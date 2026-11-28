"""Concurrent emits on one run must not collide on (run_id, seq) or fork the hash chain."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from helpers import make_world

WORKERS = 40


def test_concurrent_emits_keep_one_contiguous_chain(tmp_path):
    world = make_world(tmp_path, busy=[])
    run_id = "run_concurrent"
    world.bus.store.start_run(run_id, "<concurrent@mail.example.com>", datetime.now(timezone.utc).isoformat())

    errors: list[BaseException] = []
    barrier = threading.Barrier(WORKERS)

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            world.bus.emit(run_id, "act", "tool_call", {"worker": index})
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors

    verified, detail = world.bus.verify_chain(run_id)
    assert verified, detail

    stored = [event["seq"] for event in world.bus.store.events(run_id)]
    assert stored == list(range(1, WORKERS + 1))

    lines = [
        json.loads(line)
        for line in (world.settings.traces_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The JSONL is the audit source of truth: seq order must match the chain, not thread order.
    assert [line["seq"] for line in lines] == list(range(1, WORKERS + 1))
    assert len({line["hash"] for line in lines}) == WORKERS
