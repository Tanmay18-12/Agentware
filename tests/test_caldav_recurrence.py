"""CalDAV recurrence: free/busy must see every occurrence of a repeating series."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from helpers import make_world
from meetingpilot.adapters.caldav_calendar import CalDavCalendar, expand_events, parse_ics

WINDOW_START = datetime(2026, 11, 25, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 12, 20, tzinfo=timezone.utc)
JAN_TO_APRIL = (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 4, 1, tzinfo=timezone.utc))


def _ics(
    start: str,
    end: str,
    rrule: str = "",
    extra: str = "",
    uid: str = "series@example.com",
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        "SUMMARY:Standup",
    ]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if extra:
        lines.append(extra)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


def _starts(
    start: str,
    end: str,
    rrule: str,
    extra: str = "",
    window: tuple[datetime, datetime] = (WINDOW_START, WINDOW_END),
) -> list[str]:
    events = expand_events(parse_ics(_ics(start, end, rrule, extra)), window[0], window[1])
    return [event["start_utc"] for event in events]


def test_daily_count_expands_into_separate_occurrences():
    starts = _starts("20261201T100000Z", "20261201T103000Z", "FREQ=DAILY;COUNT=3")

    assert starts == [
        "2026-12-01T10:00:00+00:00",
        "2026-12-02T10:00:00+00:00",
        "2026-12-03T10:00:00+00:00",
    ]


def test_interval_skips_days():
    starts = _starts("20261201T100000Z", "20261201T103000Z", "FREQ=DAILY;INTERVAL=2;COUNT=3")

    assert [start[:10] for start in starts] == ["2026-12-01", "2026-12-03", "2026-12-05"]


def test_until_stops_the_series():
    starts = _starts("20261201T100000Z", "20261201T103000Z", "FREQ=DAILY;UNTIL=20261203T100000Z")

    assert [start[:10] for start in starts] == ["2026-12-01", "2026-12-02", "2026-12-03"]


def test_exdate_removes_one_occurrence():
    starts = _starts(
        "20261201T100000Z",
        "20261201T103000Z",
        "FREQ=DAILY;COUNT=3",
        extra="EXDATE:20261202T100000Z",
    )

    assert [start[:10] for start in starts] == ["2026-12-01", "2026-12-03"]


def test_weekly_byday_lists_each_requested_weekday():
    # 2026-12-01 is a Tuesday; BYDAY=MO,WE starts on the following Wednesday.
    starts = _starts("20261201T100000Z", "20261201T103000Z", "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4")

    assert [start[:10] for start in starts] == ["2026-12-02", "2026-12-07", "2026-12-09", "2026-12-14"]


def test_monthly_clamps_short_months():
    starts = _starts(
        "20260131T100000Z",
        "20260131T103000Z",
        "FREQ=MONTHLY;COUNT=3",
        window=JAN_TO_APRIL,
    )

    assert [start[:10] for start in starts] == ["2026-01-31", "2026-02-28", "2026-03-31"]


def test_tzid_series_keeps_its_local_clock():
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:ist@example.com",
            "DTSTART;TZID=Asia/Kolkata:20261201T100000",
            "DTEND;TZID=Asia/Kolkata:20261201T103000",
            "SUMMARY:Standup",
            "RRULE:FREQ=DAILY;COUNT=2",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )

    events = expand_events(parse_ics(ics), WINDOW_START, WINDOW_END)

    assert [event["start_utc"] for event in events] == [
        "2026-12-01T04:30:00+00:00",
        "2026-12-02T04:30:00+00:00",
    ]


def test_occurrences_outside_the_window_are_dropped():
    starts = _starts("20261001T100000Z", "20261001T103000Z", "FREQ=DAILY;COUNT=2")

    assert starts == []


def test_freebusy_expands_a_weekly_series(tmp_path):
    world = make_world(tmp_path, busy=[])
    calendar = CalDavCalendar(world.settings)
    series = parse_ics(_ics("20261201T100000Z", "20261201T103000Z", "FREQ=WEEKLY;COUNT=4"))

    async def fake_report(start_utc: str, end_utc: str):
        return series

    calendar._report = fake_report  # type: ignore[method-assign]

    busy = asyncio.run(
        calendar.freebusy("2026-12-08T00:00:00+00:00", "2026-12-09T00:00:00+00:00")
    )

    assert [row["start_utc"] for row in busy] == ["2026-12-08T10:00:00+00:00"]
    assert [row["end_utc"] for row in busy] == ["2026-12-08T10:30:00+00:00"]


def test_list_events_keeps_one_row_per_series(tmp_path):
    world = make_world(tmp_path, busy=[])
    calendar = CalDavCalendar(world.settings)
    series = parse_ics(_ics("20261201T100000Z", "20261201T103000Z", "FREQ=DAILY;COUNT=30"))

    async def fake_report(start_utc: str, end_utc: str):
        return series

    calendar._report = fake_report  # type: ignore[method-assign]

    assert len(asyncio.run(calendar.list_events())) == 1
