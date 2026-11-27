"""Scheduler unit tests: DST transitions, cross-timezone, phrases, buffers, races."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from helpers import base_prefs, make_world
from meetingpilot.pipeline.scheduler import (
    Interval,
    compute_slots,
    overlaps,
    parse_phrase,
    subtract,
    work_block,
)
from meetingpilot.pipeline.validator import local_is_ambiguous, local_is_nonexistent, resolve_tz
from meetingpilot.policy_gateway import new_action, ToolRuntime
from meetingpilot.schemas import EmailEnvelope, MeetingIntent, TimeWindow

from zoneinfo import ZoneInfo


def intent_with(start_iso: str | None, duration: int = 60, tz: str = "UTC", **kwargs) -> MeetingIntent:
    window = TimeWindow(start_iso=start_iso, tz=tz, flexibility="exact")
    return MeetingIntent(
        kind="meeting_request", confidence=0.95, title="Test", duration_min=duration,
        windows=[window], **kwargs
    )


def test_spring_forward_work_window_is_utc_correct():
    prefs = base_prefs(
        owner_tz="America/New_York", work_start="09:00", work_end="17:00",
        buffer_before=0, buffer_after=0, min_notice_hours=0, protected=(),
        pref_start="09:00", pref_end="17:00", grid_min=30,
    )
    tz = ZoneInfo("America/New_York")
    # 2027-03-15 is the Monday after the US spring-forward (2027-03-14)
    block = work_block(datetime(2027, 3, 15).date(), prefs, tz)
    assert block is not None
    assert block.start == datetime(2027, 3, 15, 13, 0, tzinfo=timezone.utc)  # 09:00 EDT


def test_spring_forward_gap_is_nonexistent():
    tz = resolve_tz("America/New_York", "UTC")
    assert local_is_nonexistent(datetime(2027, 3, 14, 2, 30, tzinfo=tz))
    assert not local_is_nonexistent(datetime(2027, 3, 15, 2, 30, tzinfo=tz))


def test_fall_back_hour_is_ambiguous():
    tz = resolve_tz("America/New_York", "UTC")
    assert local_is_ambiguous(datetime(2026, 11, 1, 1, 30, tzinfo=tz))
    assert not local_is_ambiguous(datetime(2026, 11, 2, 1, 30, tzinfo=tz))


def test_fall_back_work_window_is_utc_correct():
    prefs = base_prefs(
        owner_tz="America/New_York", work_start="09:00", work_end="17:00",
        buffer_before=0, buffer_after=0, min_notice_hours=0, protected=(),
    )
    tz = ZoneInfo("America/New_York")
    block = work_block(datetime(2026, 11, 2).date(), prefs, tz)
    assert block is not None
    assert block.start == datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # 09:00 EST


def test_cross_timezone_request_lands_inside_owner_hours():
    prefs = base_prefs(
        owner_tz="America/New_York", work_start="09:00", work_end="17:00",
        buffer_before=0, buffer_after=0, min_notice_hours=0, protected=(),
    )
    # requester in India asks for 09:00 IST (03:30 UTC), outside NY hours
    intent = intent_with("2026-12-02T09:00:00+05:30", duration=30, tz="Asia/Kolkata")
    slots = compute_slots(intent, prefs, [], now_utc=datetime(2026, 11, 26, tzinfo=timezone.utc), limit=5)
    assert slots, "expected alternatives outside the requested region"
    tz = ZoneInfo("America/New_York")
    for slot in slots:
        local = slot.start_utc.astimezone(tz)
        assert 9 <= local.hour < 17


def test_next_friday_resolves_from_received_time():
    tz = ZoneInfo("Asia/Kolkata")
    now_local = datetime(2026, 11, 25, 12, 0, tzinfo=tz)  # Wednesday
    start, end = parse_phrase("next friday at 14:00", tz, now_local, timedelta(minutes=45), "exact")
    assert start is not None and end is not None
    assert (start.year, start.month, start.day, start.hour) == (2026, 11, 27, 14)
    assert end - start == timedelta(minutes=45)


def test_past_request_never_produces_a_past_slot():
    prefs = base_prefs(min_notice_hours=4)
    now = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
    intent = intent_with("2026-11-24T10:00:00+00:00", duration=30)
    slots = compute_slots(intent, prefs, [], now_utc=now)
    assert all(slot.start_utc >= now + timedelta(hours=4) for slot in slots)


def test_midnight_boundary_window():
    prefs = base_prefs(
        owner_tz="UTC", work_start="00:00", work_end="03:00", work_days=(0, 1, 2, 3, 4, 5, 6),
        buffer_before=0, buffer_after=0, min_notice_hours=0, protected=(),
        pref_start="00:00", pref_end="03:00",
    )
    intent = intent_with("2026-12-02T00:30:00+00:00", duration=30)
    slots = compute_slots(intent, prefs, [], now_utc=datetime(2026, 12, 1, 12, tzinfo=timezone.utc))
    assert any(slot.start_utc == datetime(2026, 12, 2, 0, 30, tzinfo=timezone.utc) for slot in slots)


def test_invalid_clock_is_unresolvable():
    tz = ZoneInfo("Asia/Kolkata")
    now_local = datetime(2026, 11, 25, 12, 0, tzinfo=tz)
    start, end = parse_phrase("2026-12-02 at 25:00", tz, now_local, timedelta(minutes=30), "exact")
    assert start is None and end is None


def test_leap_day_is_schedulable():
    prefs = base_prefs(
        owner_tz="UTC", work_start="09:00", work_end="17:00", buffer_before=0, buffer_after=0,
        min_notice_hours=0, protected=(), horizon_days=900, pref_start="09:00", pref_end="17:00",
    )
    intent = intent_with("2028-02-29T10:00:00+00:00", duration=30)
    slots = compute_slots(intent, prefs, [], now_utc=datetime(2026, 11, 26, tzinfo=timezone.utc))
    assert any(slot.start_utc.date().isoformat() == "2028-02-29" for slot in slots)


def wide_intent(start_iso: str, end_iso: str, duration: int = 30) -> MeetingIntent:
    return MeetingIntent(
        kind="meeting_request", confidence=0.95, title="Test", duration_min=duration,
        windows=[TimeWindow(start_iso=start_iso, end_iso=end_iso, flexibility="window")],
    )


def test_buffer_blocks_adjacent_slot():
    prefs = base_prefs(min_notice_hours=0, buffer_before=10, buffer_after=10, grid_min=15, protected=())
    busy = [{"start_utc": "2026-12-02T10:00:00+00:00", "end_utc": "2026-12-02T11:00:00+00:00"}]
    intent = wide_intent("2026-12-02T00:00:00+00:00", "2026-12-03T00:00:00+00:00")
    slots = compute_slots(intent, prefs, busy, now_utc=datetime(2026, 12, 1, tzinfo=timezone.utc), limit=50)
    starts = {slot.start_utc for slot in slots}
    assert datetime(2026, 12, 2, 11, 0, tzinfo=timezone.utc) not in starts
    assert datetime(2026, 12, 2, 11, 15, tzinfo=timezone.utc) in starts


def test_protected_block_is_never_offered():
    from meetingpilot.config import ProtectedBlock

    prefs = base_prefs(
        min_notice_hours=0,
        protected=(ProtectedBlock(days=(0, 1, 2, 3, 4, 5, 6), start="13:00", end="14:00", label="Lunch"),),
    )
    intent = wide_intent("2026-12-02T00:00:00+00:00", "2026-12-03T00:00:00+00:00")
    slots = compute_slots(intent, prefs, [], now_utc=datetime(2026, 12, 1, tzinfo=timezone.utc), limit=200)
    assert slots
    tz = ZoneInfo(prefs.owner_tz)
    for slot in slots:
        local = slot.start_utc.astimezone(tz)
        assert not (local.hour == 13 and local.minute < 60)


def test_two_phase_hold_recheck_gates_a_race(tmp_path):
    world = make_world(tmp_path / "race", llm_mode="stub")
    envelope = world.ingest(
        {"from": "dana@company.com", "subject": "Race", "body": "Meet {{D1}} at 10:00 IST for 30 min?"}
    )
    run_id = "run_race_test"
    world.bus.store.start_run(run_id, envelope.message_id, datetime.now(timezone.utc).isoformat())
    runtime = ToolRuntime(
        mail=world.mail,
        calendar=world.calendar,
        guard=world.orchestrator.guard,
        prefs=world.settings.prefs,
        settings=world.settings,
    )
    hold = asyncio.run(
        world.orchestrator.gateway.execute(
            new_action(
                run_id, "act", "calendar.create_hold",
                {"title": "Race", "start_utc": "2026-12-02T04:30:00+00:00",
                 "end_utc": "2026-12-02T05:00:00+00:00", "attendees": ["dana@company.com"]},
                envelope.message_id,
            ),
            runtime,
            envelope,
        )
    )
    assert hold.allowed, hold.decision
    hold_id = hold.result["event_id"]
    world.calendar.insert_event(
        "Other booking", "2026-12-02T04:45:00+00:00", "2026-12-02T05:15:00+00:00"
    )
    confirm = asyncio.run(
        world.orchestrator.gateway.execute(
            new_action(
                run_id, "act", "calendar.confirm_event",
                {"event_id": hold_id, "start_utc": "2026-12-02T04:30:00+00:00",
                 "end_utc": "2026-12-02T05:00:00+00:00",
                 "attendees": ["dana@company.com"], "agent_tag": "meetingpilot"},
                envelope.message_id,
            ),
            runtime,
            envelope,
        )
    )
    assert confirm.decision.verdict == "GATE"
    assert "P5" in confirm.decision.rule_ids


def test_interval_math():
    a = Interval(datetime(2026, 1, 1, 9, tzinfo=timezone.utc), datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    b = Interval(datetime(2026, 1, 1, 11, tzinfo=timezone.utc), datetime(2026, 1, 1, 13, tzinfo=timezone.utc))
    assert overlaps(a, b)
    pieces = subtract([a], [b])
    assert len(pieces) == 1 and pieces[0].end.hour == 11
