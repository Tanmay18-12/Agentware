"""``watch`` resumes from a stored provider cursor.

The loop used to ask for the newest ten messages every poll, so a backlog drained nothing and a
restart re-read the same window. It now advances a persisted cursor and re-reads only what follows.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from helpers import base_prefs
from meetingpilot.adapters.mock_calendar import MockCalendar
from meetingpilot.cli import Services, _watch_once
from meetingpilot.config import Settings
from meetingpilot.event_bus import EventBus
from meetingpilot.kill_switch import KillSwitch
from meetingpilot.orchestrator import Orchestrator


class FakeMailbox:
    """Mail in the provider contract: numeric ids, ``cursor`` is exclusive, oldest first."""

    def __init__(self, count: int = 2) -> None:
        self.messages: dict[str, bytes] = {}
        self.read_handles: list[str] = []
        for index in range(1, count + 1):
            self.add(index)

    def add(self, index: int) -> None:
        message = EmailMessage()
        message["From"] = "dana@company.com"
        message["To"] = "owner@example.com"
        message["Subject"] = f"Meeting {index}"
        message["Message-ID"] = f"<watch-{index}@mail.example.com>"
        message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        message.set_content("Can we meet tomorrow at 10:00 UTC for 30 minutes?")
        self.messages[str(index)] = message.as_bytes()

    async def list_new(
        self, mailbox: str = "inbox", limit: int = 10, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        floor = int(cursor) if cursor else 0
        handles = [handle for handle in sorted(self.messages, key=int) if int(handle) > floor]
        return [{"id": handle} for handle in handles[:limit]]

    async def read(self, message_id: str) -> dict[str, Any] | None:
        self.read_handles.append(message_id)
        raw = self.messages.get(message_id)
        return None if raw is None else {"id": message_id, "raw": raw.decode()}

    async def aclose(self) -> None:
        pass


class VanishingMailbox(FakeMailbox):
    """Lists a handle that is already gone when it is read."""

    async def read(self, message_id: str) -> dict[str, Any] | None:
        self.read_handles.append(message_id)
        return None


class FakeOrchestrator:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.handled: list[str] = []

    async def handle(self, envelope: Any) -> Any:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("provider exploded")
        self.handled.append(envelope.message_id)
        return type("Summary", (), {"outcome": "BOOKED", "decision": "BOOK", "run_id": "run-watch"})()


def _services(tmp_path, mail: FakeMailbox) -> Services:
    settings = Settings(
        root=tmp_path,
        prefs=base_prefs(),
        database_path=tmp_path / "sandbox/meetingpilot.db",
    )
    settings.ensure_dirs()
    bus = EventBus(settings)
    calendar = MockCalendar(settings)
    kill = KillSwitch(settings.kill_file)
    return Services(
        settings, bus, mail, calendar, kill, Orchestrator(settings, bus, mail, calendar, kill)
    )


def test_watch_handles_the_whole_backlog_then_resumes_from_the_cursor(tmp_path):
    mail = FakeMailbox(3)
    services = _services(tmp_path, mail)
    orchestrator = FakeOrchestrator()
    services.orchestrator = orchestrator

    assert _watch_once(services) == 3
    assert services.bus.store.watch_cursor("mock:inbox") == "3"
    assert mail.read_handles == ["1", "2", "3"]

    assert _watch_once(services) == 0
    assert mail.read_handles == ["1", "2", "3"]

    mail.add(4)
    assert _watch_once(services) == 1
    assert services.bus.store.watch_cursor("mock:inbox") == "4"
    assert len(orchestrator.handled) == 4


def test_a_failed_run_is_retried_from_the_same_cursor(tmp_path):
    mail = FakeMailbox(1)
    services = _services(tmp_path, mail)
    orchestrator = FakeOrchestrator(fail_times=1)
    services.orchestrator = orchestrator

    assert _watch_once(services) == 0
    assert services.bus.store.watch_cursor("mock:inbox") is None

    assert _watch_once(services) == 1
    assert orchestrator.handled == ["watch-1@mail.example.com"]
    assert services.bus.store.watch_cursor("mock:inbox") == "1"


def test_a_message_deleted_at_the_provider_does_not_block_the_cursor(tmp_path):
    mail = VanishingMailbox(1)
    services = _services(tmp_path, mail)

    assert _watch_once(services) == 0
    assert services.bus.store.watch_cursor("mock:inbox") == "1"


def test_mock_adapter_cursor_returns_only_later_files(tmp_path):
    from meetingpilot.adapters.mock_mail import MockMail

    settings = Settings(root=tmp_path, prefs=base_prefs())
    settings.ensure_dirs()
    mail = MockMail(settings)
    first = settings.inbox_dir / "01.eml"
    first.write_text("first", encoding="utf-8")
    (settings.inbox_dir / "02.eml").write_text("second", encoding="utf-8")

    later = asyncio.run(mail.list_new(limit=10, cursor=str(first)))

    assert [item["name"] for item in later] == ["02.eml"]
