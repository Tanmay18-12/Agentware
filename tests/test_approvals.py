"""Human approval flow: queued escalations, approve, reject, kill/resume survival."""

from __future__ import annotations

import asyncio

import pytest

from helpers import base_prefs, make_world


def _queue_vip_approval(world):
    """A VIP sender escalates with a proposed slot and a queued approval."""
    envelope = world.ingest(
        {
            "from": "ceo@company.com",
            "subject": "Board sync",
            "body": "Can we meet {{D1_1000}} for 30 minutes?",
        }
    )
    summary = asyncio.run(world.orchestrator.handle(envelope))
    approvals = world.bus.store.list_approvals("pending")
    return envelope, summary, approvals


def _outbox_files(world) -> list[str]:
    return sorted(path.name for path in world.settings.outbox_dir.glob("*.eml"))


def _agent_events(world) -> list[dict]:
    events = asyncio.run(world.calendar.list_events())
    return [event for event in events if event.get("agent_tag")]


def test_approving_a_queued_escalation_books_and_sends(tmp_path):
    world = make_world(tmp_path, prefs=base_prefs(min_notice_hours=0), busy=[])
    _envelope, summary, approvals = _queue_vip_approval(world)

    assert summary.decision == "ESCALATE"
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["payload"]["slots"], "escalation should carry a proposed slot"

    result = asyncio.run(world.orchestrator.approve(approval["id"]))

    assert result["ok"] is True
    assert result["sent"] is True
    assert result["booking"] is not None
    assert _outbox_files(world), "approval must send through the gateway"
    booked = _agent_events(world)
    assert booked and booked[0]["status"] == "confirmed"
    assert world.bus.store.list_approvals("pending") == []


def test_rejected_approval_sends_nothing_and_cannot_be_approved(tmp_path):
    world = make_world(tmp_path, prefs=base_prefs(min_notice_hours=0), busy=[])
    _envelope, _summary, [approval] = _queue_vip_approval(world)

    decided = world.bus.store.decide_approval(approval["id"], "rejected")
    assert decided is not None and decided["status"] == "rejected"
    assert world.bus.store.list_approvals("pending") == []

    result = asyncio.run(world.orchestrator.approve(approval["id"]))

    assert result == {"ok": False, "reason": "approval not found or already decided"}
    assert _outbox_files(world) == []
    assert _agent_events(world) == []


def test_approvals_survive_kill_and_resume(tmp_path):
    world = make_world(tmp_path, prefs=base_prefs(min_notice_hours=0), busy=[])
    _envelope, _summary, [approval] = _queue_vip_approval(world)

    world.kill.trigger(by="test", reason="operator break")
    assert world.kill.status()["state"] == "HALTED"
    assert [item["id"] for item in world.bus.store.list_approvals("pending")] == [approval["id"]]

    with pytest.raises(ValueError):
        world.kill.reset(ack=False)

    assert world.kill.reset(ack=True)["state"] == "RUNNING"
    assert [item["id"] for item in world.bus.store.list_approvals("pending")] == [approval["id"]]

    result = asyncio.run(world.orchestrator.approve(approval["id"]))
    assert result["ok"] is True and result["sent"] is True
    assert _outbox_files(world)
    assert world.bus.store.list_approvals("pending") == []
