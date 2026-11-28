"""Adapter regressions: Mailpit Message-ID parity and a CalDAV collection that vanished."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from helpers import make_world
from meetingpilot.adapters.caldav_calendar import CalDavCalendar
from meetingpilot.adapters.mailpit_mail import MailpitMail

RAW = (
    "From: dana@company.com\r\n"
    "To: owner@example.com\r\n"
    "Subject: Meeting request\r\n"
    "Message-ID: <abc123@company.com>\r\n"
    "\r\n"
    "Can we meet tomorrow?\r\n"
)


def _mailpit(tmp_path) -> MailpitMail:
    world = make_world(tmp_path, busy=[])
    return MailpitMail(world.settings)


def test_mailpit_read_reports_the_rfc_message_id(tmp_path):
    mail = _mailpit(tmp_path)

    async def fake_get(path: str):
        assert path.endswith("/raw")
        return SimpleNamespace(status_code=200, text=RAW)

    mail._get = fake_get  # type: ignore[method-assign]

    record = asyncio.run(mail.read("42"))

    assert record["id"] == "42"
    assert record["message_id"] == "abc123@company.com"
    assert record["raw"] == RAW


def test_mailpit_read_falls_back_to_the_handle_without_a_header(tmp_path):
    mail = _mailpit(tmp_path)

    async def fake_get(path: str):
        return SimpleNamespace(status_code=200, text="From: x@y.com\r\n\r\nno message id\r\n")

    mail._get = fake_get  # type: ignore[method-assign]

    assert asyncio.run(mail.read("42"))["message_id"] == "42"


def test_a_vanished_collection_is_re_probed(tmp_path):
    world = make_world(tmp_path, busy=[])
    calendar = CalDavCalendar(world.settings)
    calendar._ready = True

    calendar._note_response(SimpleNamespace(status_code=409))

    assert calendar._ready is False


def test_a_missing_event_keeps_the_collection_ready(tmp_path):
    world = make_world(tmp_path, busy=[])
    calendar = CalDavCalendar(world.settings)
    calendar._ready = True

    calendar._note_response(SimpleNamespace(status_code=404))
    calendar._note_response(SimpleNamespace(status_code=207))

    assert calendar._ready is True
