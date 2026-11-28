"""``text/calendar`` attachments reach the reader as a resolvable time.

An invite attached to a message used to be dropped: the reader only saw the prose, so a calendar
invite with no time in the body became a CLARIFY.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

from helpers import base_prefs
from meetingpilot.config import Settings
from meetingpilot.pipeline.ingest import Ingest
from meetingpilot.pipeline.reader_llm import stub_extract

ICS = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MeetingPilot//test//EN",
        "BEGIN:VEVENT",
        "UID:invite-1@example.com",
        "SUMMARY:Design review",
        "DTSTART:20261201T150000Z",
        "DTEND:20261201T153000Z",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)
BODY = "Hi -- see the attached invite."


def _eml(ics: str | None, *, nested: bool = False) -> bytes:
    message = EmailMessage()
    message["From"] = "dana@company.com"
    message["To"] = "owner@example.com"
    message["Subject"] = "Design review"
    message["Message-ID"] = "<ics-1@mail.example.com>"
    message["Date"] = "Mon, 30 Nov 2026 09:00:00 +0000"
    message.set_content(BODY)
    if nested:
        message.add_alternative("<p>Hi -- see the attached invite.</p>", subtype="html")
    if ics is not None:
        message.add_attachment(ics, subtype="calendar", filename="invite.ics")
    return message.as_bytes()


def _envelope(tmp_path, ics: str | None, *, nested: bool = False):
    return Ingest(Settings(root=tmp_path, prefs=base_prefs())).from_raw(_eml(ics, nested=nested))


def test_ics_attachment_is_described_in_the_body_and_flagged(tmp_path):
    envelope = _envelope(tmp_path, ICS)

    assert (
        "Calendar invite: Design review on 2026-12-01 at 15:00 UTC for 30 minutes."
        in envelope.body_text
    )
    assert "see the attached invite" in envelope.body_text
    assert "ics_attachment" in envelope.header_flags


def test_ics_attachment_drives_the_stub_reader_to_the_invite_time(tmp_path):
    intent = stub_extract(_envelope(tmp_path, ICS), base_prefs())

    assert intent.kind == "meeting_request"
    assert intent.duration_min == 30
    assert intent.windows, intent.ambiguity
    start = datetime.fromisoformat(intent.windows[0].start_iso)
    assert start == datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)


def test_ics_attachment_inside_a_nested_multipart_is_found(tmp_path):
    envelope = _envelope(tmp_path, ICS, nested=True)

    assert "Calendar invite: Design review" in envelope.body_text
    assert "ics_attachment" in envelope.header_flags


def test_malformed_ics_attachment_is_ignored(tmp_path):
    broken = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:no-start@example.com\r\nEND:VEVENT\r\n"

    envelope = _envelope(tmp_path, broken)

    assert envelope.body_text.strip() == BODY
    assert "ics_attachment" not in envelope.header_flags
