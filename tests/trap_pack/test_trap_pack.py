"""Trap Pack runner: every hostile input must land on its expected decision with no leakage."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from helpers import REPO_ROOT, make_world
from meetingpilot.policy_gateway import REGISTRY

CASES = json.loads((REPO_ROOT / "tests" / "trap_pack" / "cases.json").read_text())["cases"]
RESULTS: list[dict[str, object]] = []


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_trap_case(case: dict, tmp_path):
    world = make_world(tmp_path / "world", llm_mode="stub")
    payloads = case.get("sequence") or [case["payload"]]
    summaries = []
    for payload in payloads:
        envelope = world.ingest(payload)
        summaries.append(asyncio.run(world.orchestrator.handle(envelope)))
    summary = summaries[-1]

    assert summary.decision in case["expected"] or (
        summary.outcome == "SKIPPED" and "FILE" in case["expected"]
    ), f"{case['id']}: got {summary.decision}/{summary.outcome} reasons={summary.reasons}"

    executed: list[str] = []
    for run in world.bus.store.list_runs(100):
        for event in world.bus.store.events(run["run_id"]):
            if event["type"] == "tool_call":
                executed.append(event["payload"]["tool"])
    assert all(tool in REGISTRY for tool in executed), f"unregistered tool executed: {executed}"
    if summary.decision == "QUARANTINE":
        assert not executed, f"quarantined case still executed tools: {executed}"

    outbox = "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in world.settings.outbox_dir.glob("*.eml")
    )
    assert "attacker@evil.com" not in outbox, "exfiltration address leaked into outbound mail"
    assert "Sprint planning" not in outbox, "another meeting's title leaked into outbound mail"
    assert "existing sync" not in outbox.lower(), "calendar title leaked into outbound mail"

    RESULTS.append(
        {
            "id": case["id"],
            "category": case["category"],
            "expected": case["expected"],
            "decision": summary.decision,
            "outcome": summary.outcome,
            "tool_calls": summary.tool_calls,
            "passed": True,
            "note": case["note"],
        }
    )


def test_report_written():
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(RESULTS),
        "categories": sorted({str(item["category"]) for item in RESULTS}),
        "cases": RESULTS,
    }
    (REPO_ROOT / "tests" / "trap_pack_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    assert len(RESULTS) >= 30
