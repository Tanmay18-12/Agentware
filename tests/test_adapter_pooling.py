"""BUG-10: the HTTP adapters keep one pooled ``httpx.AsyncClient`` instead of one per request."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from helpers import make_world
from meetingpilot.adapters.caldav_calendar import CalDavCalendar
from meetingpilot.adapters.mailpit_mail import MailpitMail
from meetingpilot.cli import Services, build_services


class FakeClient:
    """Stands in for ``httpx.AsyncClient`` and records every construction."""

    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_closed = False
        self.calls: list[tuple[str, str]] = []
        FakeClient.instances.append(self)

    # Used by the old code (one client per request inside ``async with``), so that a missing
    # pooling fix shows up as a client count, not as a protocol error.
    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def get(self, url: str) -> SimpleNamespace:
        self.calls.append(("GET", url))
        return SimpleNamespace(status_code=200, json=lambda: {"messages": []}, text="")

    async def request(self, method: str, url: str, **kwargs) -> SimpleNamespace:
        self.calls.append((method, url))
        return SimpleNamespace(status_code=207, text="")

    async def aclose(self) -> None:
        self.is_closed = True


def _install(monkeypatch) -> None:
    FakeClient.instances = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def test_mailpit_reuses_one_client_across_requests(tmp_path, monkeypatch):
    _install(monkeypatch)
    mail = MailpitMail(make_world(tmp_path, busy=[]).settings)

    async def run() -> None:
        await mail._get("/api/v1/messages")
        await mail._get("/api/v1/messages")

    asyncio.run(run())

    assert len(FakeClient.instances) == 1
    assert len(FakeClient.instances[0].calls) == 2
    asyncio.run(mail.aclose())
    assert FakeClient.instances[0].is_closed is True


def test_mailpit_rebuilds_the_client_after_close(tmp_path, monkeypatch):
    _install(monkeypatch)
    mail = MailpitMail(make_world(tmp_path, busy=[]).settings)

    async def run() -> None:
        await mail._get("/api/v1/messages")
        await mail.aclose()
        await mail._get("/api/v1/messages")

    asyncio.run(run())

    assert [client.is_closed for client in FakeClient.instances] == [True, False]


def test_caldav_reuses_one_client_across_requests(tmp_path, monkeypatch):
    _install(monkeypatch)
    calendar = CalDavCalendar(make_world(tmp_path, busy=[]).settings)
    calendar._ready = True

    async def run() -> None:
        await calendar._request("GET", "http://caldav.local/collection/")
        await calendar._request("GET", "http://caldav.local/collection/")

    asyncio.run(run())

    assert len(FakeClient.instances) == 1
    assert len(FakeClient.instances[0].calls) == 2
    asyncio.run(calendar.aclose())
    assert FakeClient.instances[0].is_closed is True


def test_services_close_tolerates_mock_providers(tmp_path):
    """The mock providers expose no ``aclose``; closing services must not raise."""
    services = build_services(make_world(tmp_path, busy=[]).settings)

    asyncio.run(services.aclose())

    assert isinstance(services, Services)
