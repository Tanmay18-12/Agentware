"""SQLite calendar: free/busy, agent-tagged tentative holds, confirm and cancel-own."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class MockCalendar:
    def __init__(self, settings: Settings, path: Path | None = None):
        self.settings = settings
        self.path = path or settings.calendar_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, title TEXT, start_utc TEXT, end_utc TEXT,
                    attendees TEXT, status TEXT, agent_tag TEXT, created_at TEXT
                )
                """
            )

    def _rows(self, start: str | None = None, end: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM events WHERE status != 'cancelled'"
        params: list[Any] = []
        if start and end:
            query += " AND start_utc < ? AND end_utc > ?"
            params = [end, start]
        with self._lock:
            return self.conn.execute(query, params).fetchall()

    # -- reads ---------------------------------------------------------
    async def freebusy(self, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        """Busy intervals only: never titles or attendees (data minimization)."""
        start, end = _iso(_parse(start_utc)), _iso(_parse(end_utc))
        busy = []
        for row in self._rows(start, end):
            busy.append(
                {
                    "event_id": row["id"],
                    "start_utc": row["start_utc"],
                    "end_utc": row["end_utc"],
                    "status": row["status"],
                    "agent_tag": row["agent_tag"],
                }
            )
        return busy

    async def get_own_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["attendees"] = json.loads(item["attendees"] or "[]")
        return item

    async def list_events(self) -> list[dict[str, Any]]:
        out = []
        for row in self._rows():
            item = dict(row)
            item["attendees"] = json.loads(item["attendees"] or "[]")
            out.append(item)
        return sorted(out, key=lambda event: event["start_utc"])

    # -- writes --------------------------------------------------------
    async def create_hold(
        self,
        title: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = "meetingpilot",
    ) -> dict[str, Any]:
        event_id = f"hold_{uuid.uuid4().hex[:10]}"
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO events (id, title, start_utc, end_utc, attendees, status, agent_tag,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    title,
                    _iso(_parse(start_utc)),
                    _iso(_parse(end_utc)),
                    json.dumps(attendees),
                    "tentative",
                    agent_tag,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return {
            "event_id": event_id,
            "title": title,
            "start_utc": _iso(_parse(start_utc)),
            "end_utc": _iso(_parse(end_utc)),
            "status": "tentative",
            "agent_tag": agent_tag,
        }

    async def cancel_own_hold(self, event_id: str) -> dict[str, Any]:
        event = await self.get_own_event(event_id)
        if event is None:
            return {"ok": False, "reason": "not_found"}
        if event["agent_tag"] != "meetingpilot":
            return {"ok": False, "reason": "not_agent_owned"}
        with self._lock, self.conn:
            self.conn.execute("UPDATE events SET status = 'cancelled' WHERE id = ?", (event_id,))
        return {"ok": True, "event_id": event_id, "status": "cancelled"}

    async def confirm_event(
        self,
        event_id: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = "meetingpilot",
    ) -> dict[str, Any]:
        event = await self.get_own_event(event_id)
        if event is None:
            return {"ok": False, "reason": "not_found"}
        if event["agent_tag"] != "meetingpilot":
            return {"ok": False, "reason": "not_agent_owned"}
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE events SET start_utc = ?, end_utc = ?, attendees = ?, status = 'confirmed',"
                " agent_tag = ? WHERE id = ?",
                (
                    _iso(_parse(start_utc)),
                    _iso(_parse(end_utc)),
                    json.dumps(attendees),
                    agent_tag,
                    event_id,
                ),
            )
        return {
            "ok": True,
            "event_id": event_id,
            "status": "confirmed",
            "start_utc": _iso(_parse(start_utc)),
            "end_utc": _iso(_parse(end_utc)),
        }

    # -- seeding helper ------------------------------------------------
    def insert_event(
        self,
        title: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str] | None = None,
        status: str = "confirmed",
        agent_tag: str = "",
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or f"evt_{uuid.uuid4().hex[:10]}"
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO events (id, title, start_utc, end_utc, attendees, status,"
                " agent_tag, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    title,
                    _iso(_parse(start_utc)),
                    _iso(_parse(end_utc)),
                    json.dumps(attendees or []),
                    status,
                    agent_tag,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return event_id
