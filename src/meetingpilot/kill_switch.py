"""Kill switch: in-process event mirrored to a file flag, with TaskGroup-friendly cancellation."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class KilledError(RuntimeError):
    """Raised when work is attempted after the kill switch has been triggered."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class KillSwitch:
    def __init__(self, path: Path):
        self.path = path
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self.triggered_at: str | None = None
        self.triggered_by: str | None = None
        self.reason: str | None = None

    # -- state ---------------------------------------------------------
    def is_triggered(self) -> bool:
        if self._event.is_set():
            return True
        if self.path.exists():
            self._set("file", ".kill flag present")
            return True
        return False

    def _set(self, by: str, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.triggered_at = _now()
            self.triggered_by = by
            self.reason = reason
            self._event.set()

    def trigger(self, by: str = "operator", reason: str = "manual") -> dict[str, Any]:
        self._set(by, reason)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(f"{self.triggered_at} {by} {reason}\n", encoding="utf-8")
        except OSError:
            pass
        return self.status()

    def reset(self, ack: bool = False) -> dict[str, Any]:
        if not ack:
            raise ValueError("resume requires explicit acknowledgement (resume --ack)")
        with self._lock:
            self.triggered_at = None
            self.triggered_by = None
            self.reason = None
            self._event.clear()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return self.status()

    def status(self) -> dict[str, Any]:
        triggered = self.is_triggered()
        return {
            "triggered": triggered,
            "state": "HALTED" if triggered else "RUNNING",
            "triggered_at": self.triggered_at,
            "triggered_by": self.triggered_by,
            "reason": self.reason,
        }

    def check(self) -> None:
        if self.is_triggered():
            raise KilledError(f"kill switch active ({self.reason or 'flag'})")

    # -- async integration --------------------------------------------
    async def watch(self, *tasks: asyncio.Task[Any], poll_s: float = 0.05) -> None:
        """Cancel ``tasks`` as soon as the switch trips (event or file)."""
        while not self.is_triggered():
            await asyncio.sleep(poll_s)
        for task in tasks:
            task.cancel()
