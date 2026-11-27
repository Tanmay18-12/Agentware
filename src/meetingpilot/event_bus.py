"""Event bus: one stream feeds the JSONL audit trail, SQLite state and the dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .schemas import RunEvent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_hash(prev_hash: str, record: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical(record))


class StateStore:
    """SQLite: run state, events, idempotency keys, budget counters, approvals."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL,
                    step_id TEXT, type TEXT NOT NULL, payload TEXT, cost_usd REAL DEFAULT 0,
                    hash TEXT NOT NULL, prev_hash TEXT NOT NULL, record TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, message_id TEXT, outcome TEXT,
                    started_at TEXT, ended_at TEXT, decision TEXT,
                    tool_calls INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
                    human_interventions INTEGER DEFAULT 0, minutes_saved REAL DEFAULT 0,
                    summary TEXT
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY, tool TEXT, result TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS seen_messages (
                    message_id TEXT PRIMARY KEY, first_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS window_counters (
                    name TEXT NOT NULL, ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS totals (name TEXT PRIMARY KEY, value REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, payload TEXT,
                    status TEXT DEFAULT 'pending', created_at TEXT, decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS thread_state (
                    thread_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, payload TEXT, updated_at TEXT
                );
                """
            )

    # -- events --------------------------------------------------------
    def next_seq(self, run_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["s"]) + 1

    def last_hash(self, run_id: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        return row["hash"] if row else "0" * 64

    def insert_event(self, event: RunEvent) -> None:
        record = event.model_dump(mode="json")
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO events (run_id, seq, ts, step_id, type, payload, cost_usd,"
                " hash, prev_hash, record) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event.run_id,
                    event.seq,
                    event.ts,
                    event.step_id,
                    event.type,
                    canonical(event.payload),
                    event.cost_usd,
                    event.hash,
                    event.prev_hash,
                    canonical(record),
                ),
            )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT record FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    # -- runs ----------------------------------------------------------
    def start_run(self, run_id: str, message_id: str, started_at: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, message_id, started_at, tool_calls, cost_usd,"
                " human_interventions) VALUES (?,?,?,0,0,0)",
                (run_id, message_id, started_at),
            )

    def finish_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "outcome",
            "ended_at",
            "decision",
            "tool_calls",
            "cost_usd",
            "human_interventions",
            "minutes_saved",
            "summary",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [
            value if isinstance(value, (int, float, str)) or value is None else canonical(value)
            for value in updates.values()
        ]
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*values, run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def bump_run_tool_calls(self, run_id: str) -> int:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE runs SET tool_calls = COALESCE(tool_calls, 0) + 1 WHERE run_id = ?",
                (run_id,),
            )
            row = self.conn.execute(
                "SELECT tool_calls FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["tool_calls"]) if row else 0

    def run_tool_calls(self, run_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(tool_calls, 0) AS c FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["c"]) if row else 0

    # -- idempotency ---------------------------------------------------
    def get_idempotent(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT result FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def put_idempotent(self, key: str, tool: str, result: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO idempotency (key, tool, result, created_at) VALUES (?,?,?,?)",
                (key, tool, canonical(result), now_iso()),
            )

    # -- dedupe --------------------------------------------------------
    def mark_seen(self, message_id: str) -> bool:
        """Return True the first time a Message-ID is seen."""
        with self._lock, self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO seen_messages (message_id, first_seen) VALUES (?,?)",
                    (message_id, now_iso()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # -- counters ------------------------------------------------------
    def hit(self, name: str, at: float | None = None) -> None:
        import time as _time

        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO window_counters (name, ts) VALUES (?,?)", (name, at or _time.time())
            )

    def hits_since(self, name: str, seconds: float) -> int:
        import time as _time

        cutoff = _time.time() - seconds
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM window_counters WHERE name = ? AND ts >= ?",
                (name, cutoff),
            ).fetchone()
        return int(row["c"])

    def bump_total(self, name: str, amount: float = 1.0) -> float:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO totals (name, value) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET value = value + ?",
                (name, amount, amount),
            )
            row = self.conn.execute("SELECT value FROM totals WHERE name = ?", (name,)).fetchone()
        return float(row["value"]) if row else amount

    def total(self, name: str) -> float:
        with self._lock:
            row = self.conn.execute("SELECT value FROM totals WHERE name = ?", (name,)).fetchone()
        return float(row["value"]) if row else 0.0

    # -- approvals -----------------------------------------------------
    def add_approval(self, approval_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO approvals (id, run_id, kind, payload, status, created_at)"
                " VALUES (?,?,?,?, 'pending', ?)",
                (approval_id, run_id, kind, canonical(payload), now_iso()),
            )

    def list_approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def decide_approval(self, approval_id: str, status: str) -> dict[str, Any] | None:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                # A decided approval is final; deciding twice must not change it.
                return None
            self.conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
                (status, now_iso(), approval_id),
            )
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["status"] = status
        return item

    # -- thread state --------------------------------------------------
    def set_thread_state(self, thread_id: str, run_id: str, status: str, payload: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO thread_state (thread_id, run_id, status, payload, updated_at)"
                " VALUES (?,?,?,?,?)",
                (thread_id, run_id, status, canonical(payload), now_iso()),
            )

    def get_thread_state(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM thread_state WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item


class EventBus:
    """Append-only, hash-chained event stream with fan-out to dashboard subscribers."""

    def __init__(self, settings: Settings, store: StateStore | None = None):
        self.settings = settings
        self.store = store or StateStore(settings.database_path)
        settings.traces_dir.mkdir(parents=True, exist_ok=True)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._index_hash = self._load_index_hash()

    # -- audit trail ---------------------------------------------------
    def _run_path(self, run_id: str) -> Path:
        return self.settings.traces_dir / f"{run_id}.jsonl"

    @property
    def index_path(self) -> Path:
        return self.settings.traces_dir / "index.jsonl"

    def _load_index_hash(self) -> str:
        path = self.index_path
        if not path.is_file():
            return "0" * 64
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return "0" * 64
        return json.loads(lines[-1]).get("hash", "0" * 64)

    def emit(
        self,
        run_id: str,
        step_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        parent_id: str | None = None,
    ) -> RunEvent:
        seq = self.store.next_seq(run_id)
        prev_hash = self.store.last_hash(run_id)
        record = {
            "seq": seq,
            "ts": now_iso(),
            "run_id": run_id,
            "step_id": step_id,
            "parent_id": parent_id,
            "type": type,
            "payload": payload or {},
            "cost_usd": round(float(cost_usd), 6),
        }
        digest = compute_hash(prev_hash, record)
        event = RunEvent(**record, prev_hash=prev_hash, hash=digest)
        self.store.insert_event(event)
        with self._run_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(canonical(event.model_dump(mode="json")) + "\n")
        self._publish(event.model_dump(mode="json"))
        return event

    def append_run_summary(self, summary: dict[str, Any]) -> None:
        prev = self._index_hash
        record = {"ts": now_iso(), **summary}
        digest = compute_hash(prev, record)
        line = {**record, "prev_hash": prev, "hash": digest}
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(line) + "\n")
        self._index_hash = digest
        self._publish({"type": "run_summary", "payload": summary, "run_id": summary.get("run_id", "")})

    # -- dashboard fan-out --------------------------------------------
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except (asyncio.QueueFull, RuntimeError):
                self._subscribers.discard(queue)

    # -- verification --------------------------------------------------
    def verify_chain(self, run_id: str) -> tuple[bool, str]:
        path = self._run_path(run_id)
        if not path.is_file():
            return False, f"no trace file for {run_id}"
        prev = "0" * 64
        expected_seq = 1
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {number} is not valid JSON"
            stored_hash = record.pop("hash", None)
            stored_prev = record.pop("prev_hash", None)
            if record.get("seq") != expected_seq:
                return False, f"line {number} has seq {record.get('seq')}, expected {expected_seq}"
            if stored_prev != prev:
                return False, f"line {number} prev_hash mismatch"
            if compute_hash(stored_prev, record) != stored_hash:
                return False, f"line {number} hash mismatch (tampered)"
            prev = stored_hash
            expected_seq += 1
        return True, f"{expected_seq - 1} events verified"

    def verify_index_chain(self) -> tuple[bool, str]:
        path = self.index_path
        if not path.is_file():
            return True, "no index yet"
        prev = "0" * 64
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            record = json.loads(line)
            stored_hash = record.pop("hash", None)
            stored_prev = record.pop("prev_hash", None)
            if stored_prev != prev or compute_hash(stored_prev, record) != stored_hash:
                return False, f"index line {number} failed verification"
            prev = stored_hash
        return True, "index verified"
