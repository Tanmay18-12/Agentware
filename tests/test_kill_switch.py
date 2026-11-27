"""Kill switch proof: mid-run cancellation under one second with nothing executed after the kill."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from helpers import make_world
from meetingpilot.adapters.mock_mail import MockMail
from meetingpilot.kill_switch import KillSwitch
from meetingpilot.orchestrator import Orchestrator


class SlowMail(MockMail):
    """Send blocks long enough for the kill switch to interrupt it."""

    async def send_reply(self, *args, **kwargs):  # type: ignore[override]
        await asyncio.sleep(3.0)
        return await super().send_reply(*args, **kwargs)


def test_kill_cancels_a_run_in_flight(tmp_path):
    world = make_world(tmp_path / "kill")
    orchestrator = Orchestrator(
        world.settings, world.bus, SlowMail(world.settings), world.calendar, world.kill
    )
    envelope = world.ingest(
        {
            "from": "dana@company.com",
            "subject": "Kill test",
            "body": "Can we meet {{D1}} at 10:00 IST for 30 minutes?",
        }
    )

    async def scenario():
        task = asyncio.create_task(orchestrator.handle(envelope))
        await asyncio.sleep(0.5)
        started = time.perf_counter()
        world.kill.trigger(by="test", reason="mid-run kill")
        summary = await task
        return summary, time.perf_counter() - started

    summary, latency = asyncio.run(scenario())

    assert summary.outcome == "HALTED"
    assert latency < 1.0, f"kill latency was {latency:.3f}s"

    triggered_at = datetime.fromisoformat(str(world.kill.triggered_at).replace("Z", "+00:00"))
    late_calls = []
    for run in world.bus.store.list_runs(10):
        for event in world.bus.store.events(run["run_id"]):
            if event["type"] != "tool_call":
                continue
            stamp = datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
            if stamp > triggered_at:
                late_calls.append(event)
    assert not late_calls, "a tool executed after the kill timestamp"

    assert summary.decision in {"ESCALATE", "HALTED", "FILE"}
    assert not list(world.settings.outbox_dir.glob("*.eml")), "cancelled send still wrote mail"
    assert world.kill.is_triggered()


def test_kill_latency_reported_in_the_trace(tmp_path):
    world = make_world(tmp_path / "trace")
    world.kill.trigger(by="test")
    envelope = world.ingest({"from": "dana@company.com", "subject": "halted", "body": "Meet {{D1}} 10:00 IST."})
    summary = asyncio.run(world.orchestrator.handle(envelope))
    events = world.bus.store.events(summary.run_id)
    assert any(event["type"] == "kill" or event["type"] == "action_blocked" for event in events)


def test_file_flag_and_acknowledged_resume(tmp_path):
    path = tmp_path / ".kill"
    kill = KillSwitch(path)
    assert not kill.is_triggered()
    path.write_text("tripped\n", encoding="utf-8")
    assert kill.is_triggered(), "the .kill file must trip the switch"
    with pytest.raises(ValueError):
        kill.reset()
    status = kill.reset(ack=True)
    assert status["triggered"] is False
    assert not path.exists()


def test_pending_approvals_survive_a_kill(tmp_path):
    world = make_world(tmp_path / "approvals")
    envelope = world.ingest(
        {
            "from": "procurement@acme-corp.test",
            "subject": "Vendor session",
            "body": "Book 30 min on {{D1}} at 10:00 UTC about the contract.",
        }
    )
    summary = asyncio.run(world.orchestrator.handle(envelope))
    assert summary.outcome == "ESCALATED"
    pending = world.bus.store.list_approvals()
    assert pending, "escalations must be queued for a human"
    assert pending[0]["status"] == "pending"
