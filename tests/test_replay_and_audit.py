"""Audit trail: deterministic replay, tamper detection and at-least-once ingest safety."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from helpers import make_world
from meetingpilot.cli import cmd_explain, cmd_replay, cmd_verify_chain
from meetingpilot.pipeline.ingest import DuplicateMessage, Ingest


def _run_booked(world):
    payload = {
        "from": "dana@company.com",
        "subject": "Replay me",
        "body": "Can we meet {{D1}} at 10:00 IST for 30 minutes?",
        "message_id": "<replay-1@mail.example.com>",
        "thread_id": "thread-replay",
    }
    envelope = world.ingest(payload)
    summary = asyncio.run(world.orchestrator.handle(envelope))
    return payload, summary


def test_decision_replays_identically(tmp_path, capsys):
    world = make_world(tmp_path / "replay")
    payload, summary = _run_booked(world)
    assert summary.decision == "BOOK", summary.reasons

    code = cmd_replay(SimpleNamespace(run_id=summary.run_id), world.settings)
    output = capsys.readouterr().out
    assert code == 0, output
    assert json.loads(output)["identical"] is True


def test_chain_verifies_then_detects_tampering(tmp_path):
    world = make_world(tmp_path / "tamper")
    _payload, summary = _run_booked(world)

    ok, detail = world.bus.verify_chain(summary.run_id)
    assert ok, detail

    path = world.settings.traces_dir / f"{summary.run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[3])
    record["payload"]["verdict"] = "ALLOW"
    lines[3] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, detail = world.bus.verify_chain(summary.run_id)
    assert not ok
    assert "hash mismatch" in detail

    code = cmd_verify_chain(SimpleNamespace(run_id=summary.run_id), world.settings)
    assert code == 1


def test_explain_prints_the_timeline(tmp_path, capsys):
    world = make_world(tmp_path / "explain")
    _payload, summary = _run_booked(world)
    code = cmd_explain(SimpleNamespace(run_id=summary.run_id), world.settings)
    output = capsys.readouterr().out
    assert code == 0
    assert "policy_decision" in output
    assert "run_start" in output


def test_human_interventions_is_zero_in_the_summary(tmp_path):
    world = make_world(tmp_path / "hi")
    _payload, summary = _run_booked(world)
    run = world.bus.store.get_run(summary.run_id)
    assert run is not None
    assert run["human_interventions"] == 0
    summary_events = [
        event
        for event in world.bus.store.events(summary.run_id)
        if event["type"] == "run_summary"
    ]
    assert summary_events[0]["payload"]["human_interventions"] == 0


def test_duplicate_message_is_not_processed_twice(tmp_path):
    import pytest

    world = make_world(tmp_path / "dupe")
    payload = {
        "from": "dana@company.com",
        "subject": "Only once",
        "body": "Meet {{D1}} 10:00 IST for 30 minutes.",
        "message_id": "<dupe-1@mail.example.com>",
        "thread_id": "thread-dupe",
    }
    first = world.ingest(payload)
    summary = asyncio.run(world.orchestrator.handle(first))
    assert summary.decision in {"BOOK", "PROPOSE"}

    with pytest.raises(DuplicateMessage):
        Ingest(world.settings, world.bus.store).from_dict(payload)


def test_index_chain_verifies(tmp_path):
    world = make_world(tmp_path / "index")
    _payload, summary = _run_booked(world)
    ok, detail = world.bus.verify_index_chain()
    assert ok, detail
    assert world.bus.store.get_run(summary.run_id)["minutes_saved"] > 0
