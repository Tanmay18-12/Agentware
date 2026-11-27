"""Stage 1 -- Ingest: normalize a raw message into a bounded :class:`EmailEnvelope`."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email import policy as email_policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..event_bus import StateStore
from ..schemas import EmailEnvelope

MAX_BYTES = 512_000
MAX_BODY = 8000

ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
HIDDEN_STYLE_RE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0|color\s*:\s*#fff(?:fff)?)",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
HIDDEN_BLOCK_RE = re.compile(
    r"<(?P<tag>div|span|p|font)[^>]*style=\"[^\"]*(?:display\s*:\s*none|visibility\s*:\s*hidden|"
    r"font-size\s*:\s*0|opacity\s*:\s*0)[^\"]*\"[^>]*>.*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
NO_REPLY_LOCALS = {"no-reply", "noreply", "do-not-reply", "donotreply", "notifications", "mailer-daemon"}


class DuplicateMessage(RuntimeError):
    """The Message-ID has already been ingested."""


class MalformedMessage(ValueError):
    """The message could not be normalized."""


def html_to_text(html: str) -> tuple[str, bool]:
    """Strip HTML. Returns (text, saw_hidden_text)."""
    hidden = bool(HIDDEN_BLOCK_RE.search(html)) or bool(ZERO_WIDTH and any(ch in html for ch in ZERO_WIDTH))
    cleaned = HIDDEN_BLOCK_RE.sub(" ", html)
    cleaned = SCRIPT_RE.sub(" ", cleaned)
    cleaned = TAG_RE.sub(" ", cleaned)
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[ \t]+", " ", cleaned)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip(), hidden


def _sanitize(body: str) -> str:
    body = "".join(ch for ch in body if ch not in ZERO_WIDTH) if any(ch in body for ch in ZERO_WIDTH) else body
    return body[:MAX_BODY]


def _header_flags(
    headers: dict[str, str], envelope_from: str, owner_email: str, body: str, hidden: bool
) -> list[str]:
    flags: list[str] = []
    auto = headers.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        flags.append("auto_submitted")
    if "list-unsubscribe" in headers:
        flags.append("list_unsubscribe")
    if headers.get("precedence", "").strip().lower() in {"bulk", "list", "junk"}:
        flags.append("precedence_bulk")
    local = envelope_from.split("@")[0].lower() if "@" in envelope_from else ""
    if local in NO_REPLY_LOCALS:
        flags.append("no_reply")
    if envelope_from.lower() == owner_email.lower():
        flags.append("self_sent")
    if hidden:
        flags.append("hidden_text")
    if any(ch in body for ch in ZERO_WIDTH):
        flags.append("zero_width")
    return flags


class Ingest:
    def __init__(self, settings: Settings, store: StateStore | None = None):
        self.settings = settings
        self.store = store

    # -- entry points --------------------------------------------------
    def from_path(self, path: Path) -> EmailEnvelope:
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise MalformedMessage(f"message exceeds {MAX_BYTES} bytes")
        if path.suffix.lower() == ".json":
            payload = json.loads(raw.decode("utf-8"))
            envelope = self._from_dict(payload, fallback_id=path.stem)
        else:
            envelope = self._from_eml(raw)
        return self._finalize(envelope)

    def from_dict(self, payload: dict[str, Any]) -> EmailEnvelope:
        return self._finalize(self._from_dict(payload, fallback_id="inline"))

    # -- parsers -------------------------------------------------------
    def _from_dict(self, payload: dict[str, Any], fallback_id: str) -> EmailEnvelope:
        headers = {str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()}
        sender = str(payload.get("from") or payload.get("from_addr") or "unknown@example.com")
        subject = str(payload.get("subject") or "")
        body = str(payload.get("body") or "")
        html = payload.get("html")
        hidden = False
        if html:
            text, hidden = html_to_text(str(html))
            body = body or text
        auth = payload.get("auth_results") or {}
        if isinstance(auth, str):
            auth = {"raw": auth}

        received_raw = payload.get("received_at") or payload.get("date")
        received_at = _parse_date(received_raw)

        thread_id = str(payload.get("thread_id") or fallback_id)
        message_id = str(payload.get("message_id") or hashlib.sha256(
            f"{sender}|{subject}|{body}".encode()
        ).hexdigest()[:24])

        envelope = EmailEnvelope(
            message_id=message_id,
            thread_id=thread_id,
            from_addr=sender,
            reply_to=(str(payload["reply_to"]) if payload.get("reply_to") else None),
            return_path=(str(payload["return_path"]) if payload.get("return_path") else None),
            auth_results={str(k): str(v) for k, v in auth.items()},
            subject=subject[:300],
            body_text=_sanitize(body),
            hidden_text_flag=hidden,
            header_flags=[],
            received_at=received_at,
            size_bytes=len((subject + body).encode()),
        )
        envelope.header_flags = _header_flags(
            headers, envelope.from_addr, self.settings.prefs.owner_email, body, hidden
        )
        for key in ("auto_submitted", "list_unsubscribe", "precedence_bulk"):
            if key in (payload.get("header_flags") or []):
                envelope.header_flags.append(key)
        return envelope

    def _from_eml(self, raw: bytes) -> EmailEnvelope:
        message = message_from_bytes(raw, policy=email_policy.default)
        headers = {key.lower(): str(value) for key, value in message.items()}
        sender = headers.get("from", "unknown@example.com")
        if "<" in sender:
            sender = sender.split("<")[-1].strip("> ")
        reply_to = headers.get("reply-to")
        if reply_to and "<" in reply_to:
            reply_to = reply_to.split("<")[-1].strip("> ")
        return_path = headers.get("return-path", "").strip("<> ") or None

        body = ""
        html = ""
        hidden = False
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not body:
                    body = part.get_content()
                elif content_type == "text/html" and not html:
                    html = part.get_content()
        else:
            if message.get_content_type() == "text/html":
                html = message.get_content()
            else:
                body = message.get_content()
        if html:
            text, hidden = html_to_text(html)
            body = body or text

        message_id = headers.get("message-id", "").strip("<> ") or hashlib.sha256(raw).hexdigest()[:24]
        references = headers.get("references", "")
        thread_id = (
            headers.get("x-thread-id")
            or (references.split()[-1] if references.strip() else "")
            or message_id
        ).strip("<> ")

        auth_results = headers.get("authentication-results", "")
        auth = {"raw": auth_results} if auth_results else {}

        received_at = _parse_date(headers.get("date"))
        envelope = EmailEnvelope(
            message_id=message_id,
            thread_id=thread_id,
            from_addr=sender,
            reply_to=reply_to or None,
            return_path=return_path,
            auth_results=auth,
            subject=headers.get("subject", "")[:300],
            body_text=_sanitize(body),
            hidden_text_flag=hidden,
            header_flags=[],
            received_at=received_at,
            size_bytes=len(raw),
        )
        envelope.header_flags = _header_flags(
            headers, envelope.from_addr, self.settings.prefs.owner_email, body, hidden
        )
        return envelope

    def _finalize(self, envelope: EmailEnvelope) -> EmailEnvelope:
        if self.store is not None and not self.store.mark_seen(envelope.message_id):
            raise DuplicateMessage(envelope.message_id)
        return envelope


def _parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
