"""SMTP delivery shared by the real mail adapters, plus a local ``.eml`` mirror.

The mirror is what the dashboard's Outbox tab shows: the agent's own copy of what it sent.
"""

from __future__ import annotations

import asyncio
import smtplib
import socket
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

from ..config import Settings
from .base import ProviderError, ProviderTimeout, RateLimitError, ServerError


def build_message(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.mail_from or settings.prefs.owner_email
    message["To"] = to
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain="meetingpilot.local")
    message["Thread-Id"] = thread_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    # Loop prevention for every outbound message.
    message["Auto-Submitted"] = "auto-replied"
    message["X-MeetingPilot-Thread"] = thread_id
    message.set_content(body)
    return message


class SmtpSender:
    def __init__(self, settings: Settings, smtp_factory: Any | None = None):
        self.settings = settings
        self._factory = smtp_factory or (
            smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        )

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to: str | None = None,
        references: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict[str, Any]:
        message = build_message(
            self.settings,
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            cc=cc,
            bcc=bcc,
        )
        try:
            await asyncio.to_thread(self._send_sync, message)
        except (TimeoutError, socket.timeout) as error:
            raise ProviderTimeout(f"smtp send timed out: {error}") from error
        mirror = self._mirror(message)
        return {
            "message_id": str(message["Message-ID"]).strip("<>"),
            "path": str(mirror),
            "transport": "smtp",
        }

    def _send_sync(self, message: EmailMessage) -> None:
        client = self._factory(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.smtp_timeout_s,
        )
        try:
            if self.settings.smtp_starttls:
                client.starttls()
            if self.settings.smtp_user:
                client.login(self.settings.smtp_user, self.settings.smtp_password)
            client.send_message(message)
        except smtplib.SMTPAuthenticationError as error:
            # Subclass of SMTPResponseException: check it first or it becomes a 5xx retry loop.
            raise ProviderError(f"smtp authentication failed: {error}") from error
        except smtplib.SMTPResponseException as error:
            code = int(getattr(error, "smtp_code", 0) or 0)
            detail = f"smtp {code}: {getattr(error, 'smtp_error', b'')!r}"
            if 400 <= code < 500:
                raise RateLimitError(detail) from error
            raise ServerError(detail) from error
        except (smtplib.SMTPException, OSError) as error:
            raise ServerError(f"smtp error: {error}") from error
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - closing a broken session must not mask the send
                pass

    def _mirror(self, message: EmailMessage) -> Path:
        folder = self.settings.outbox_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.eml"
        )
        path.write_text(message.as_string(), encoding="utf-8")
        return path
