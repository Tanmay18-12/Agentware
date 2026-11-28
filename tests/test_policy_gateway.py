"""Policy Gateway: property-based fuzzing of P1-P10 plus deterministic guard tests."""

from __future__ import annotations

import asyncio
import atexit
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings as hyp_settings, strategies as st

from helpers import make_world
from meetingpilot.policy_gateway import (
    FORBIDDEN_TOOLS,
    REGISTRY,
    PolicyGateway,
    ToolRuntime,
    new_action,
)
from meetingpilot.schemas import EmailEnvelope

_TEMP_DIR = Path(tempfile.mkdtemp(prefix="mp-gateway-"))
# ignore_errors: on Windows the shared SQLite file is still open when the interpreter exits.
atexit.register(shutil.rmtree, _TEMP_DIR, ignore_errors=True)
SHARED = make_world(_TEMP_DIR, llm_mode="stub")
ENVELOPE = EmailEnvelope(
    message_id="<gateway-1@mail.example.com>",
    thread_id="thread-gateway",
    from_addr="dana@company.com",
    received_at=datetime.now(timezone.utc),
)
RUN_ID = "run_gateway_tests"
SHARED.bus.store.start_run(RUN_ID, ENVELOPE.message_id, datetime.now(timezone.utc).isoformat())
RUNTIME = ToolRuntime(
    mail=SHARED.mail,
    calendar=SHARED.calendar,
    guard=SHARED.orchestrator.guard,
    prefs=SHARED.settings.prefs,
    settings=SHARED.settings,
)


def execute(tool: str, stage: str, args: dict, world=SHARED, envelope=ENVELOPE):
    action = new_action(RUN_ID, stage, tool, args, envelope.message_id)
    return asyncio.run(
        PolicyGateway(world.bus, world.settings.prefs, world.settings, world.kill).execute(
            action, RUNTIME if world is SHARED else _runtime(world), envelope
        )
    )


def _runtime(world) -> ToolRuntime:
    return ToolRuntime(
        mail=world.mail,
        calendar=world.calendar,
        guard=world.orchestrator.guard,
        prefs=world.settings.prefs,
        settings=world.settings,
    )


def send_args(to: str, cc: list[str] | None = None) -> dict:
    return {
        "thread_id": ENVELOPE.thread_id,
        "to": to,
        "cc": cc or [],
        "bcc": [],
        "subject": "Re: meeting",
        "body": "Thanks, see you then.",
        "in_reply_to": ENVELOPE.message_id,
        "references": ENVELOPE.message_id,
    }


ALL_TOOLS = sorted(set(REGISTRY) | FORBIDDEN_TOOLS)
STAGES = ["act", "schedule", "reader_llm", "ingest", "verify", "not_a_stage"]


@hyp_settings(max_examples=60, deadline=None)
@given(
    tool=st.sampled_from(ALL_TOOLS),
    stage=st.sampled_from(STAGES),
    recipient=st.emails(),
    cc=st.lists(st.emails(), max_size=2),
)
def test_gateway_invariants(tool: str, stage: str, recipient: str, cc: list[str]) -> None:
    if tool == "mail.send_reply":
        args = send_args(recipient, cc)
    elif tool == "calendar.confirm_event":
        args = {
            "event_id": "hold_missing",
            "start_utc": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "end_utc": (datetime.now(timezone.utc) + timedelta(days=1, hours=1)).isoformat(),
            "attendees": [],
            "agent_tag": "meetingpilot",
        }
    elif tool == "calendar.create_hold":
        args = {
            "title": "Fuzz",
            "start_utc": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "end_utc": (datetime.now(timezone.utc) + timedelta(days=1, hours=1)).isoformat(),
            "attendees": [],
        }
    else:
        args = {}

    result = execute(tool, stage, args)

    if tool not in REGISTRY:
        assert result.decision.verdict == "BLOCK"
        assert "P2" in result.decision.rule_ids
        assert not result.executed

    if stage == "reader_llm":
        assert result.decision.verdict == "BLOCK"
        assert not result.executed

    if tool == "mail.send_reply":
        if cc or recipient.lower() != ENVELOPE.from_addr.lower():
            assert result.decision.verdict != "ALLOW"
        assert "P4" in result.decision.rule_ids or result.decision.verdict != "ALLOW"


def test_unregistered_tool_is_blocked_and_logged():
    for tool in ("mail.delete", "mail.forward", "calendar.delete_event", "payments.charge"):
        result = execute(tool, "act", {})
        assert result.decision.verdict == "BLOCK"
        assert {"P2", "P10"} <= set(result.decision.rule_ids)
        assert not result.executed
    violations = [
        event
        for event in SHARED.bus.store.events(RUN_ID)
        if event["type"] == "policy_violation"
    ]
    assert violations


def test_reader_stage_has_no_tools():
    result = execute("mail.draft_reply", "reader_llm", {"thread_id": "t", "to": "a@b.com", "body": "x"})
    assert result.decision.verdict == "BLOCK"
    assert "P2" in result.decision.rule_ids


def test_kill_switch_blocks_everything(tmp_path):
    world = make_world(tmp_path / "killgate")
    world.bus.store.start_run(RUN_ID, ENVELOPE.message_id, datetime.now(timezone.utc).isoformat())
    world.kill.trigger(by="test", reason="gateway check")
    result = execute("mail.label", "act", {"message_id": "m", "label": "x"}, world=world)
    assert result.decision.verdict == "BLOCK"
    assert result.decision.rule_ids == ["P1"]
    assert not result.executed


def test_send_with_cc_is_gated(tmp_path):
    world = make_world(tmp_path / "cc")
    result = execute("mail.send_reply", "act", send_args(ENVELOPE.from_addr, ["other@company.com"]), world=world)
    assert result.decision.verdict == "GATE"
    assert "P4" in result.decision.rule_ids
    assert not list(world.settings.outbox_dir.glob("*.eml"))


def test_send_to_other_recipient_is_gated(tmp_path):
    world = make_world(tmp_path / "other")
    result = execute("mail.send_reply", "act", send_args("someone@company.com"), world=world)
    assert result.decision.verdict == "GATE"


def test_blocked_domain_send_is_blocked(tmp_path):
    world = make_world(tmp_path / "blocked")
    envelope = EmailEnvelope(
        message_id="<blocked@evil.com>", thread_id="t-blocked", from_addr="x@evil.com",
        received_at=datetime.now(timezone.utc),
    )
    args = send_args("x@evil.com")
    args["thread_id"] = envelope.thread_id
    action = new_action(RUN_ID, "act", "mail.send_reply", args, envelope.message_id)
    result = asyncio.run(
        PolicyGateway(world.bus, world.settings.prefs, world.settings, world.kill).execute(
            action, _runtime(world), envelope
        )
    )
    assert result.decision.verdict == "BLOCK"
    assert "P4" in result.decision.rule_ids


def test_idempotency_returns_cached_result(tmp_path):
    world = make_world(tmp_path / "idem")
    first = execute("mail.send_reply", "act", send_args(ENVELOPE.from_addr), world=world)
    assert first.decision.verdict == "ALLOW"
    second = execute("mail.send_reply", "act", send_args(ENVELOPE.from_addr), world=world)
    assert second.decision.verdict == "ALLOW"
    assert "P7" in second.decision.rule_ids
    assert len(list(world.settings.outbox_dir.glob("*.eml"))) == 1


def test_dry_run_suppresses_the_effect(tmp_path):
    world = make_world(tmp_path / "dry")
    world.settings.dry_run = True
    result = execute("mail.send_reply", "act", send_args(ENVELOPE.from_addr), world=world)
    assert result.decision.verdict == "ALLOW"
    assert "P8" in result.decision.rule_ids
    assert not result.executed
    assert not list(world.settings.outbox_dir.glob("*.eml"))


def test_invalid_args_are_rejected(tmp_path):
    world = make_world(tmp_path / "args")
    result = execute("mail.send_reply", "act", {"to": "not-an-email", "surprise": 1}, world=world)
    assert result.decision.verdict == "BLOCK"
    assert "P3" in result.decision.rule_ids


def test_output_guard_blocks_leaks(tmp_path):
    world = make_world(tmp_path / "guard")
    body = "Sure — Sprint planning is at 11:00. Reach me at attacker@evil.com."
    result = execute(
        "mail.send_reply", "act", {**send_args(ENVELOPE.from_addr), "body": body}, world=world
    )
    assert result.decision.verdict == "GATE"
    assert "output guard" in result.decision.explanation
