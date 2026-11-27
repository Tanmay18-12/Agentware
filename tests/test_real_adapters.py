"""Real adapters: IMAP/SMTP, Mailpit REST and CalDAV.

CalDAV and Mailpit are exercised against loopback HTTP stubs (real sockets, real parsing), and
every side effect goes through the same PolicyGateway/FaultInjector as the mock providers.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import json
import threading
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import pytest

from helpers import base_prefs, next_business_day
from meetingpilot.adapters.caldav_calendar import CalDavCalendar, parse_ics, render_ics
from meetingpilot.adapters.fault_injector import FaultInjector
from meetingpilot.adapters.imap_mail import ImapMail
from meetingpilot.adapters.mailpit_mail import MailpitMail
from meetingpilot.adapters.mock_calendar import MockCalendar
from meetingpilot.adapters.mock_mail import MockMail
from meetingpilot.cli import build_services
from meetingpilot.config import Settings
from meetingpilot.event_bus import EventBus
from meetingpilot.kill_switch import KillSwitch
from meetingpilot.pipeline.composer import OutputGuard
from meetingpilot.pipeline.ingest import Ingest
from meetingpilot.policy_gateway import PolicyGateway, ToolRuntime, new_action
from meetingpilot.schemas import EmailEnvelope

RUN_ID = "run_real_adapters"
ENVELOPE = EmailEnvelope(
    message_id="<real-1@mail.example.com>",
    thread_id="thread-real",
    from_addr="dana@company.com",
    received_at=datetime.now(timezone.utc),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _settings(tmp_path, **overrides: Any) -> Settings:
    settings = Settings(
        root=tmp_path,
        sandbox_dir=tmp_path / "sandbox",
        traces_dir=tmp_path / "traces",
        database_path=tmp_path / "sandbox" / "meetingpilot.db",
        config_path=tmp_path / "prefs.yaml",
        prefs=base_prefs(),
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.ensure_dirs()
    return settings


@contextlib.contextmanager
def _policy(settings: Settings, mail: Any, calendar: Any) -> Iterator[tuple[Any, Any, EventBus]]:
    bus = EventBus(settings)
    kill = KillSwitch(settings.kill_file)
    bus.store.start_run(RUN_ID, ENVELOPE.message_id, datetime.now(timezone.utc).isoformat())
    gateway = PolicyGateway(bus, settings.prefs, settings, kill)
    runtime = ToolRuntime(
        mail=mail,
        calendar=calendar,
        guard=OutputGuard(settings.prefs),
        prefs=settings.prefs,
        settings=settings,
    )
    yield gateway, runtime, bus


def _execute(gateway, runtime, tool: str, stage: str, args: dict[str, Any]):
    action = new_action(RUN_ID, stage, tool, args, ENVELOPE.message_id)
    return asyncio.run(gateway.execute(action, runtime, ENVELOPE))


def _send_args() -> dict[str, Any]:
    return {
        "thread_id": ENVELOPE.thread_id,
        "to": ENVELOPE.from_addr,
        "cc": [],
        "bcc": [],
        "subject": "Re: Roadmap sync",
        "body": "Thanks — that time works for me.",
        "in_reply_to": ENVELOPE.message_id,
        "references": ENVELOPE.message_id,
    }


def _raw_message(subject: str = "Roadmap sync", message_id: str = "<m1@dana.test>") -> bytes:
    message = EmailMessage()
    message["From"] = "Dana <dana@company.com>"
    message["To"] = "owner@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message.set_content("Can we meet tomorrow at 10:00 IST for 30 minutes?")
    return message.as_bytes()


def _next_slot() -> tuple[datetime, datetime]:
    tz = ZoneInfo("Asia/Kolkata")
    day = next_business_day(tz=tz)
    start = datetime.combine(day.date(), time(10, 0), tzinfo=tz)
    return start, start + timedelta(minutes=30)


# --------------------------------------------------------------------------
# fakes for protocol transports
# --------------------------------------------------------------------------
class FakeImap:
    """The subset of ``imaplib.IMAP4`` the adapter uses, over in-memory messages."""

    def __init__(self, messages: list[dict[str, Any]]):
        self.messages = messages
        self.selected: str | None = None
        self.copied: list[tuple[str, str]] = []
        self.appended: list[tuple[str, bytes]] = []
        self.fail_copy = False
        self.credentials: tuple[str, str] | None = None

    def _match(self, uid: str) -> dict[str, Any] | None:
        return next((item for item in self.messages if item["uid"] == uid), None)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.credentials = (user, password)
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected = folder
        return "OK", [b"1"]

    def create(self, folder: str) -> tuple[str, list[bytes]]:
        return "OK", [b"created"]

    def append(self, folder: str, flags: str, date: Any, payload: bytes) -> tuple[str, list[bytes]]:
        self.appended.append((folder, payload))
        return "OK", [b"appended"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b"bye"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        command = command.lower()
        if command == "search":
            query = str(args[-1])
            if query.strip().upper() == "ALL":
                return "OK", [" ".join(item["uid"] for item in self.messages).encode()]
            wanted = query.split('"', 2)[1] if '"' in query else query
            matches = [
                item["uid"]
                for item in self.messages
                if wanted.strip("<> ") in item["raw"].decode("utf-8", errors="ignore")
            ]
            return "OK", [" ".join(matches).encode()]
        if command == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            spec = str(args[1])
            item = self._match(uid)
            if item is None:
                return "NO", [b"not found"]
            payload = item["raw"]
            if "HEADER.FIELDS" in spec:
                payload = payload.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            header = f"{uid} (RFC822.SIZE {len(item['raw'])} BODY[] {{{len(payload)}}}".encode()
            return "OK", [(header, payload), b")"]
        if command == "copy":
            if self.fail_copy:
                raise imaplib.IMAP4.error("COPY not supported")
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            folder = args[1].decode() if isinstance(args[1], bytes) else str(args[1])
            self.copied.append((uid, folder))
            return "OK", [b"copied"]
        return "OK", [b""]


class FakeSmtp:
    def __init__(self, host: str, port: int, timeout: float | None = None):
        self.host, self.port = host, port
        self.sent: list[EmailMessage] = []
        self.logged_in: tuple[str, str] | None = None
        self.tls = False

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def quit(self) -> None:
        pass


def smtp_factory(sent: list[FakeSmtp]):
    """Build SMTP connections and record each one so tests can inspect what was delivered."""

    def factory(host: str, port: int, timeout: float | None = None) -> FakeSmtp:
        instance = FakeSmtp(host, port, timeout)
        sent.append(instance)
        return instance

    return factory


# --------------------------------------------------------------------------
# loopback HTTP stub base
# --------------------------------------------------------------------------
class _StubServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.stub = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)


# --------------------------------------------------------------------------
# CalDAV
# --------------------------------------------------------------------------
class _CalDavHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _drain(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def do_MKCALENDAR(self) -> None:  # noqa: N802
        self._drain()
        self.server.stub.requests.append("MKCALENDAR")  # type: ignore[attr-defined]
        self._reply(201)

    def do_PUT(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        stub.store[self.path] = self.rfile.read(length).decode("utf-8")
        stub.requests.append("PUT")
        self._reply(201)

    def do_GET(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        stub.requests.append("GET")
        if self.path not in stub.store:
            self._reply(404)
            return
        self._reply(200, stub.store[self.path].encode(), "text/calendar; charset=utf-8")

    def do_DELETE(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        stub.requests.append("DELETE")
        stub.store.pop(self.path, None)
        self._reply(204)

    def do_REPORT(self) -> None:  # noqa: N802
        self._drain()
        stub = self.server.stub  # type: ignore[attr-defined]
        stub.requests.append("REPORT")
        if stub.fail_reports > 0:
            stub.fail_reports -= 1
            self._reply(503)
            return
        root = ElementTree.Element("{DAV:}multistatus")
        for path, ics in stub.store.items():
            response = ElementTree.SubElement(root, "{DAV:}response")
            href = ElementTree.SubElement(response, "{DAV:}href")
            href.text = path
            propstat = ElementTree.SubElement(response, "{DAV:}propstat")
            prop = ElementTree.SubElement(propstat, "{DAV:}prop")
            data = ElementTree.SubElement(
                prop, "{urn:ietf:params:xml:ns:caldav}calendar-data"
            )
            data.text = ics
            status = ElementTree.SubElement(propstat, "{DAV:}status")
            status.text = "HTTP/1.1 200 OK"
        body = ElementTree.tostring(root, encoding="utf-8")
        self._reply(207, body, "application/xml; charset=utf-8")

    def log_message(self, *args: object) -> None:
        pass


class CalDavStub(_StubServer):
    def __init__(self) -> None:
        super().__init__(_CalDavHandler)
        self.store: dict[str, str] = {}
        self.requests: list[str] = []
        self.fail_reports = 0


@pytest.fixture
def caldav(tmp_path, no_proxy):
    stub = CalDavStub()
    stub.start()
    settings = _settings(
        tmp_path / "caldav",
        calendar_provider="caldav",
        caldav_url=f"{stub.base}/owner/",
        caldav_calendar="meetingpilot",
        caldav_user="owner",
        caldav_password="owner",
    )
    calendar = CalDavCalendar(settings)
    try:
        yield stub, settings, calendar
    finally:
        stub.stop()


def test_ics_roundtrip_and_cancel_flag():
    start = datetime(2027, 3, 1, 4, 30, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    body = render_ics("evt_1", "Roadmap, sync; Q2", start, end, ["dana@company.com"], "CONFIRMED")
    [event] = parse_ics(body)
    assert event["event_id"] == "evt_1"
    assert event["title"] == "Roadmap, sync; Q2"
    assert event["status"] == "confirmed"
    assert event["agent_tag"] == "meetingpilot"
    assert event["attendees"] == ["dana@company.com"]
    assert datetime.fromisoformat(event["start_utc"]) == start

    [cancelled] = parse_ics(render_ics("evt_1", "x", start, end, [], "CANCELLED"))
    assert cancelled["status"] == "cancelled"


def test_parse_ics_honours_tzid_duration_and_folding():
    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:foreign-1",
            "DTSTART;TZID=Asia/Kolkata:20261130T100000",
            "DURATION:PT45M",
            "SUMMARY:Quarterly plan",
            " ning with the leadership team",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    [event] = parse_ics(body)
    assert event["title"] == "Quarterly planning with the leadership team"
    assert datetime.fromisoformat(event["start_utc"]) == datetime(
        2026, 11, 30, 4, 30, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(event["end_utc"]) == datetime(
        2026, 11, 30, 5, 15, tzinfo=timezone.utc
    )


def test_parse_ics_uses_vtimezone_offset_for_unknown_tzid():
    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VTIMEZONE",
            "TZID:Custom/India",
            "BEGIN:STANDARD",
            "DTSTART:19700101T000000",
            "TZOFFSETFROM:+0530",
            "TZOFFSETTO:+0530",
            "END:STANDARD",
            "END:VTIMEZONE",
            "BEGIN:VEVENT",
            "UID:foreign-2",
            "DTSTART;TZID=Custom/India:20261201T090000",
            "DTEND;TZID=Custom/India:20261201T100000",
            "SUMMARY:Ops review",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    [event] = parse_ics(body)
    assert datetime.fromisoformat(event["start_utc"]) == datetime(
        2026, 12, 1, 3, 30, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(event["end_utc"]) == datetime(
        2026, 12, 1, 4, 30, tzinfo=timezone.utc
    )


def test_caldav_freebusy_respects_foreign_tzid_event(caldav):
    stub, _settings_obj, calendar = caldav
    stub.store["/owner/meetingpilot/foreign-tz.ics"] = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:foreign-tz",
            "DTSTART;TZID=Asia/Kolkata:20261130T100000",
            "DURATION:PT30M",
            "SUMMARY:Written by another client",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    window_start = datetime(2026, 11, 30, 4, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 11, 30, 6, 0, tzinfo=timezone.utc)
    busy = asyncio.run(calendar.freebusy(window_start.isoformat(), window_end.isoformat()))
    assert [item["event_id"] for item in busy] == ["foreign-tz"]
    assert datetime.fromisoformat(busy[0]["start_utc"]) == datetime(
        2026, 11, 30, 4, 30, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(busy[0]["end_utc"]) == datetime(
        2026, 11, 30, 5, 0, tzinfo=timezone.utc
    )


def test_caldav_hold_confirm_and_cancel(caldav):
    stub, _settings_obj, calendar = caldav
    start, end = _next_slot()

    hold = asyncio.run(
        calendar.create_hold("Roadmap sync", start.isoformat(), end.isoformat(), ["dana@company.com"])
    )
    assert hold["status"] == "tentative"
    assert hold["agent_tag"] == "meetingpilot"
    assert any(path.endswith(f"{hold['event_id']}.ics") for path in stub.store)

    busy = asyncio.run(calendar.freebusy(start.isoformat(), end.isoformat()))
    assert [item["event_id"] for item in busy] == [hold["event_id"]]

    confirmed = asyncio.run(
        calendar.confirm_event(
            hold["event_id"], start.isoformat(), end.isoformat(), ["dana@company.com"]
        )
    )
    assert confirmed["ok"] is True
    stored = asyncio.run(calendar.get_own_event(hold["event_id"]))
    assert stored is not None and stored["status"] == "confirmed"

    cancelled = asyncio.run(calendar.cancel_own_hold(hold["event_id"]))
    assert cancelled == {"ok": True, "event_id": hold["event_id"], "status": "cancelled"}
    assert asyncio.run(calendar.freebusy(start.isoformat(), end.isoformat())) == []


def test_caldav_never_touches_foreign_events(caldav):
    _stub, _settings_obj, calendar = caldav
    start, end = _next_slot()
    foreign = asyncio.run(
        calendar.insert_event(
            "Someone else's meeting",
            start.isoformat(),
            end.isoformat(),
            status="confirmed",
            agent_tag="someoneelse",
        )
    )
    assert asyncio.run(calendar.cancel_own_hold(foreign)) == {"ok": False, "reason": "not_agent_owned"}
    assert asyncio.run(calendar.confirm_event(foreign, start.isoformat(), end.isoformat(), [])) == {
        "ok": False,
        "reason": "not_agent_owned",
    }
    event = asyncio.run(calendar.get_own_event(foreign))
    assert event is not None and event["status"] == "confirmed"


def test_gateway_books_a_caldav_hold_end_to_end(caldav):
    _stub, settings, calendar = caldav
    start, end = _next_slot()
    with _policy(settings, ImapMail(settings), calendar) as (gateway, runtime, _bus):
        hold = _execute(
            gateway,
            runtime,
            "calendar.create_hold",
            "act",
            {
                "title": "Roadmap sync",
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "attendees": ["dana@company.com"],
                "agent_tag": "meetingpilot",
            },
        )
        assert hold.decision.verdict == "ALLOW", hold.decision.explanation
        event_id = hold.result["event_id"]

        confirm = _execute(
            gateway,
            runtime,
            "calendar.confirm_event",
            "act",
            {
                "event_id": event_id,
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "attendees": ["dana@company.com"],
                "agent_tag": "meetingpilot",
            },
        )
    assert confirm.decision.verdict == "ALLOW", confirm.decision.explanation
    assert confirm.result["status"] == "confirmed"


def test_gateway_blocks_confirming_a_missing_hold(caldav):
    _stub, settings, calendar = caldav
    start, end = _next_slot()
    with _policy(settings, ImapMail(settings), calendar) as (gateway, runtime, _bus):
        result = _execute(
            gateway,
            runtime,
            "calendar.confirm_event",
            "act",
            {
                "event_id": "hold_missing",
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "attendees": [],
                "agent_tag": "meetingpilot",
            },
        )
    assert result.decision.verdict == "BLOCK"
    assert "P5" in result.decision.rule_ids
    assert not result.executed


def test_gateway_retries_a_503_from_caldav(caldav):
    stub, settings, calendar = caldav
    start, end = _next_slot()
    stub.fail_reports = 1
    with _policy(settings, ImapMail(settings), calendar) as (gateway, runtime, bus):
        result = _execute(
            gateway,
            runtime,
            "calendar.freebusy",
            "schedule",
            {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
        )
        retries = [event for event in bus.store.events(RUN_ID) if event["type"] == "retry"]
    assert result.decision.verdict == "ALLOW"
    assert stub.requests.count("REPORT") == 2
    assert retries and retries[0]["payload"]["error"] == "ServerError"


def test_gateway_fails_closed_when_caldav_stays_down(caldav):
    stub, settings, calendar = caldav
    start, end = _next_slot()
    stub.fail_reports = 10
    with _policy(settings, ImapMail(settings), calendar) as (gateway, runtime, _bus):
        result = _execute(
            gateway,
            runtime,
            "calendar.freebusy",
            "schedule",
            {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
        )
    assert result.decision.verdict == "BLOCK"
    assert result.decision.rule_ids == ["PX"]
    assert not result.executed
    assert stub.requests.count("REPORT") == 3


def test_fault_injector_truncates_a_partial_freebusy(caldav):
    _stub, _settings_obj, calendar = caldav
    start, end = _next_slot()
    for index in range(2):
        asyncio.run(
            calendar.create_hold(
                f"Hold {index}",
                (start + timedelta(hours=index)).isoformat(),
                (end + timedelta(hours=index)).isoformat(),
                [],
            )
        )
    wrapped = FaultInjector(calendar, {"freebusy": ["partial"]})
    busy = asyncio.run(wrapped.freebusy(start.isoformat(), (end + timedelta(hours=2)).isoformat()))
    assert len(busy) == 1


# --------------------------------------------------------------------------
# IMAP / SMTP
# --------------------------------------------------------------------------
@pytest.fixture
def imap(tmp_path):
    settings = _settings(
        tmp_path / "imap",
        mail_provider="imap",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_user="owner",
        imap_password="secret",
    )
    fake = FakeImap(
        [
            {"uid": "1", "raw": _raw_message("First", "<first@dana.test>")},
            {"uid": "2", "raw": _raw_message("Second", "<second@dana.test>")},
        ]
    )
    sent: list[FakeSmtp] = []
    mail = ImapMail(
        settings,
        imap_factory=lambda *args, **kwargs: fake,
        smtp_factory=smtp_factory(sent),
    )
    return settings, mail, fake, sent


def test_imap_lists_and_reads_messages(imap):
    _settings_obj, mail, fake, _sent = imap
    items = asyncio.run(mail.list_new(limit=2))
    assert [item["message_id"] for item in items] == ["first@dana.test", "second@dana.test"]
    assert items[0]["subject"] == "First"
    assert items[0]["from_addr"] == "dana@company.com"
    assert items[0]["size_bytes"] > 0
    assert fake.credentials == ("owner", "secret")

    record = asyncio.run(mail.read("second@dana.test"))
    assert record is not None
    assert record["id"] == "2"
    assert "10:00 IST" in record["raw"]


def test_imap_draft_is_appended_to_drafts(imap):
    _settings_obj, mail, fake, _sent = imap
    draft = asyncio.run(mail.draft_reply("thread-real", "dana@company.com", "Re: sync", "hello"))
    assert fake.appended and fake.appended[0][0] == "Drafts"
    assert b"Auto-Submitted: auto-replied" in fake.appended[0][1]
    assert draft["draft_id"]


def test_imap_label_is_a_copy_never_a_move(imap):
    _settings_obj, mail, fake, _sent = imap
    result = asyncio.run(mail.label("<first@dana.test>", "meeting-booked"))
    assert result == {"ok": True, "label": "meeting-booked", "folder": "meeting-booked", "copied": 1}
    assert fake.copied == [("1", "meeting-booked")]
    assert not hasattr(fake, "delete")


def test_imap_label_degrades_instead_of_failing_the_run(imap):
    _settings_obj, mail, fake, _sent = imap
    fake.fail_copy = True
    result = asyncio.run(mail.label("<first@dana.test>", "meeting-booked"))
    assert result["ok"] is False
    assert "label" in result


def test_gateway_sends_through_smtp_with_loop_prevention(imap):
    settings, mail, _fake, sent = imap
    with _policy(settings, mail, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", _send_args())
    assert result.decision.verdict == "ALLOW", result.decision.explanation
    assert len(sent) == 1 and len(sent[0].sent) == 1
    delivered = sent[0].sent[0]
    assert delivered["To"] == "dana@company.com"
    mirror = list(settings.outbox_dir.glob("*.eml"))
    assert mirror, "the sent message must be mirrored for the dashboard"
    text = mirror[0].read_text(encoding="utf-8")
    assert "Auto-Submitted: auto-replied" in text
    assert "In-Reply-To: <real-1@mail.example.com>" in text
    assert "X-MeetingPilot-Thread: thread-real" in text


def test_gateway_gates_cc_before_smtp(imap):
    settings, mail, _fake, sent = imap
    args = _send_args() | {"cc": ["other@company.com"]}
    with _policy(settings, mail, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", args)
    assert result.decision.verdict == "GATE"
    assert "P4" in result.decision.rule_ids
    assert not sent
    assert not list(settings.outbox_dir.glob("*.eml"))


def test_gateway_retries_a_rate_limited_send(imap):
    settings, mail, _fake, sent = imap
    wrapped = FaultInjector(mail, {"send_reply": ["rate_limit"]})
    with _policy(settings, wrapped, CalDavCalendar(settings)) as (gateway, runtime, bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", _send_args())
        retries = [event for event in bus.store.events(RUN_ID) if event["type"] == "retry"]
    assert result.decision.verdict == "ALLOW"
    assert retries and retries[0]["payload"]["error"] == "RateLimitError"
    assert sent and sent[0].sent
    assert list(settings.outbox_dir.glob("*.eml"))


def test_gateway_blocks_when_smtp_keeps_failing(imap):
    settings, mail, _fake, _sent = imap
    wrapped = FaultInjector(mail, {"send_reply": ["server_error"] * 3})
    with _policy(settings, wrapped, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", _send_args())
    assert result.decision.verdict == "BLOCK"
    assert result.decision.rule_ids == ["PX"]
    assert not result.executed


def test_gateway_blocks_a_malformed_send_receipt(imap):
    settings, mail, _fake, _sent = imap
    wrapped = FaultInjector(mail, {"send_reply": ["malformed"]})
    with _policy(settings, wrapped, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", _send_args())
    assert result.decision.verdict == "BLOCK"
    assert result.decision.rule_ids == ["PX"]


# --------------------------------------------------------------------------
# Mailpit
# --------------------------------------------------------------------------
class _MailpitHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        stub.requests.append(self.path)
        if stub.fail_next > 0:
            stub.fail_next -= 1
            self._reply(503, b"{}")
            return
        if self.path == "/api/v1/messages":
            payload = {
                "messages": [
                    {
                        "ID": message["ID"],
                        "MessageID": message["MessageID"],
                        "Subject": message["Subject"],
                        "From": {"Name": "Dana", "Address": "dana@company.com"},
                        "Size": len(message["raw"]),
                    }
                    for message in stub.messages
                ]
            }
            self._reply(200, json.dumps(payload).encode())
            return
        if self.path.startswith("/api/v1/message/") and self.path.endswith("/raw"):
            message_id = self.path.split("/")[4]
            message = next((item for item in stub.messages if item["ID"] == message_id), None)
            if message is None:
                self._reply(404, b"")
                return
            self._reply(200, message["raw"].encode(), "text/plain; charset=utf-8")
            return
        self._reply(404, b"")

    def log_message(self, *args: object) -> None:
        pass


class MailpitStub(_StubServer):
    def __init__(self) -> None:
        super().__init__(_MailpitHandler)
        self.messages = [
            {"ID": "1", "MessageID": "first@dana.test", "Subject": "First", "raw": _raw_message("First", "<first@dana.test>").decode()},
        ]
        self.requests: list[str] = []
        self.fail_next = 0


@pytest.fixture
def mailpit(tmp_path, no_proxy):
    stub = MailpitStub()
    stub.start()
    settings = _settings(
        tmp_path / "mailpit",
        mail_provider="mailpit",
        mailpit_url=stub.base,
    )
    mail = MailpitMail(settings, smtp_factory=FakeSmtp)
    try:
        yield stub, settings, mail
    finally:
        stub.stop()


def test_mailpit_lists_and_reads_over_rest(mailpit):
    stub, settings, mail = mailpit
    items = asyncio.run(mail.list_new())
    assert items[0]["id"] == "1"
    assert items[0]["message_id"] == "first@dana.test"

    with _policy(settings, mail, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.read", "act", {"message_id": "first@dana.test"})
    assert result.decision.verdict == "ALLOW"
    assert "10:00 IST" in result.result["raw"]
    assert "/api/v1/messages" in stub.requests

    # Mailpit's own message ID is also a valid handle (list_new hands it out).
    by_id = asyncio.run(mail.read("1"))
    assert by_id is not None and "10:00 IST" in by_id["raw"]


def test_mailpit_sends_over_smtp(mailpit):
    _stub, settings, mail = mailpit
    with _policy(settings, mail, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.send_reply", "act", _send_args())
    assert result.decision.verdict == "ALLOW"
    assert result.result["transport"] == "smtp"
    assert list(settings.outbox_dir.glob("*.eml"))


def test_mailpit_5xx_fails_closed(mailpit):
    stub, settings, mail = mailpit
    stub.fail_next = 3
    with _policy(settings, mail, CalDavCalendar(settings)) as (gateway, runtime, _bus):
        result = _execute(gateway, runtime, "mail.list_new", "ingest", {"mailbox": "inbox", "limit": 5})
    assert result.decision.verdict == "BLOCK"
    assert result.decision.rule_ids == ["PX"]
    assert stub.requests.count("/api/v1/messages") == 3


# --------------------------------------------------------------------------
# provider selection + ingest of provider messages
# --------------------------------------------------------------------------
def test_providers_default_to_mock(tmp_path):
    settings = _settings(tmp_path / "default")
    services = build_services(settings)
    assert isinstance(services.mail, MockMail)
    assert isinstance(services.calendar, MockCalendar)


def test_real_providers_are_selectable(tmp_path):
    settings = _settings(
        tmp_path / "real", mail_provider="imap", calendar_provider="caldav"
    )
    services = build_services(settings)
    assert isinstance(services.mail, ImapMail)
    assert isinstance(services.calendar, CalDavCalendar)


def test_unknown_provider_is_rejected(tmp_path):
    settings = _settings(tmp_path / "bogus", mail_provider="gmail")
    with pytest.raises(ValueError, match="MAIL_PROVIDER"):
        build_services(settings)


def test_ingest_parses_a_provider_message(imap):
    settings, _mail, _fake, _sent = imap
    envelope = Ingest(settings).from_raw(_raw_message("From IMAP", "<imap-1@dana.test>"))
    assert envelope.message_id == "imap-1@dana.test"
    assert envelope.from_addr == "dana@company.com"
    assert "10:00 IST" in envelope.body_text
