"""Deterministic reply rendering + the Output Guard that gates every outbound message."""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..config import Prefs
from ..schemas import MeetingIntent, SlotCandidate

URL_RE = re.compile(r"https?://[^\s)>\"']+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
)
INJECTION_ECHO = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "disregard prior",
    "disregard all previous",
    "you are now",
    "forward all",
)

FOOTER = "\n\n---\nSent automatically by MeetingPilot on behalf of the calendar owner."


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


class OutputGuard:
    """Deterministic checks: recipients, data minimization, URLs, secrets, injection echo."""

    def __init__(self, prefs: Prefs, max_len: int = 4000):
        self.prefs = prefs
        self.max_len = max_len

    def check(
        self,
        body: str,
        *,
        allowed_recipients: list[str] | None = None,
        forbidden_titles: list[str] | None = None,
    ) -> GuardResult:
        problems: list[str] = []
        allowed = {addr.lower() for addr in (allowed_recipients or []) if addr}
        allowed.add(self.prefs.owner_email.lower())

        if not body or len(body) > self.max_len:
            problems.append("body length out of bounds")

        for match in EMAIL_RE.findall(body):
            if match.lower() not in allowed:
                problems.append(f"foreign email address in body: {match}")

        for url in URL_RE.findall(body):
            if not url.startswith(self.prefs.meeting_url_prefix.rstrip("/") + "/") and url.rstrip(
                "/"
            ) != self.prefs.meeting_url_prefix.rstrip("/"):
                problems.append(f"url outside allowlist: {url}")

        for pattern in SECRET_PATTERNS:
            if pattern.search(body):
                problems.append(f"secret-like token matched {pattern.pattern}")

        lowered = body.lower()
        for marker in INJECTION_ECHO:
            if marker in lowered:
                problems.append(f"injection echo: {marker}")

        for title in forbidden_titles or []:
            cleaned = (title or "").strip()
            if len(cleaned) >= 4 and cleaned.lower() in lowered:
                problems.append(f"leaks another event's title: {cleaned}")

        return GuardResult(ok=not problems, reasons=tuple(problems))


def _fmt(slot: SlotCandidate, tz: ZoneInfo) -> str:
    start = slot.start_utc.astimezone(tz)
    end = slot.end_utc.astimezone(tz)
    return f"{start:%a %d %b %Y, %H:%M}–{end:%H:%M} {start.tzname() or ''}".strip()


def _greeting(intent: MeetingIntent) -> str:
    return "Hi,"


def render_confirmation(
    intent: MeetingIntent, slot: SlotCandidate, prefs: Prefs, meeting_url: str
) -> tuple[str, str]:
    tz = ZoneInfo(prefs.owner_tz)
    subject = f"Confirmed: {intent.title} — {slot.start_utc.astimezone(tz):%a %d %b, %H:%M}"
    body = (
        f"{_greeting(intent)}\n\n"
        f"Thanks for the note. I've booked “{intent.title}” for {intent.duration_min} minutes:\n\n"
        f"  {_fmt(slot, tz)}\n\n"
        f"Join link: {meeting_url}\n\n"
        "If that no longer works, reply to this thread and I'll propose alternatives.\n"
        f"{FOOTER}"
    )
    return subject, body


def render_proposal(
    intent: MeetingIntent, slots: list[SlotCandidate], prefs: Prefs
) -> tuple[str, str]:
    tz = ZoneInfo(prefs.owner_tz)
    subject = f"Re: {intent.title} — three times that work"
    lines = [f"{index + 1}. {_fmt(slot, tz)}" for index, slot in enumerate(slots)]
    body = (
        f"{_greeting(intent)}\n\n"
        "Thanks for reaching out. The requested slot isn't available, but these work on my side:\n\n"
        + "\n".join(lines)
        + "\n\nReply with the number you prefer and I'll send the invitation.\n"
        f"{FOOTER}"
    )
    return subject, body


def render_clarification(intent: MeetingIntent, prefs: Prefs, question: str | None = None) -> tuple[str, str]:
    subject = "Re: your meeting request — one quick question"
    detail = question or "Could you confirm the date, start time and timezone you have in mind?"
    body = f"{_greeting(intent)}\n\nThanks — I can schedule this. {detail}\n{FOOTER}"
    return subject, body
