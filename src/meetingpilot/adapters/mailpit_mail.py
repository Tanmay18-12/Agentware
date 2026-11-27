"""Mailpit mailbox: read over its REST API, reply over SMTP.

Mailpit deliberately has no IMAP server (only SMTP, POP3 and HTTP), so this adapter uses the
HTTP API for the read side. Drafts, labels and follow-ups still land in the sandbox, which is
also what the dashboard shows.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from .base import ProviderError, ProviderTimeout, RateLimitError, ServerError
from .mock_mail import MockMail
from .smtp_mail import SmtpSender


class MailpitMail:
    def __init__(self, settings: Settings, *, smtp_factory: Any | None = None):
        self.settings = settings
        self.sender = SmtpSender(settings, smtp_factory)
        self._local = MockMail(settings)
        self._base = settings.mailpit_url.rstrip("/")

    # -- HTTP ----------------------------------------------------------
    def _fail(self, response: httpx.Response, action: str) -> None:
        detail = f"mailpit {action} failed: HTTP {response.status_code}"
        if response.status_code == 429:
            raise RateLimitError(detail)
        if response.status_code >= 500:
            raise ServerError(detail)
        raise ProviderError(detail)

    async def _get(self, path: str) -> httpx.Response:
        kwargs: dict[str, Any] = {"timeout": self.settings.mailpit_timeout_s}
        if self.settings.mailpit_user:
            kwargs["auth"] = (self.settings.mailpit_user, self.settings.mailpit_password)
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                return await client.get(f"{self._base}{path}")
        except httpx.TimeoutException as error:
            raise ProviderTimeout(f"mailpit request timed out: {path}") from error
        except httpx.HTTPError as error:
            raise ProviderError(f"mailpit request failed: {error}") from error

    # -- read side -----------------------------------------------------
    async def list_new(self, mailbox: str = "inbox", limit: int = 10) -> list[dict[str, Any]]:
        if mailbox != "inbox":
            # Drafts live locally: Mailpit has no drafts concept.
            return await self._local.list_new(mailbox, limit)
        response = await self._get("/api/v1/messages")
        if response.status_code != 200:
            self._fail(response, "list messages")
        items: list[dict[str, Any]] = []
        for message in response.json().get("messages", [])[:limit]:
            sender = message.get("From") or {}
            items.append(
                {
                    "id": str(message["ID"]),
                    "message_id": str(message.get("MessageID") or ""),
                    "subject": str(message.get("Subject") or ""),
                    "from_addr": str(sender.get("Address") or "")
                    if isinstance(sender, dict)
                    else str(sender),
                    "size_bytes": int(message.get("Size") or 0),
                }
            )
        return items

    async def read(self, message_id: str) -> dict[str, Any] | None:
        """Accepts either Mailpit's own message ID or the RFC 5322 Message-ID."""
        handle = message_id.strip("<> ")
        response = await self._get(f"/api/v1/message/{handle}/raw")
        if response.status_code == 404 and "@" in handle:
            for item in await self.list_new(limit=200):
                if item["message_id"] == handle:
                    handle = item["id"]
                    response = await self._get(f"/api/v1/message/{handle}/raw")
                    break
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._fail(response, "read message")
        return {"id": handle, "message_id": handle, "raw": response.text}

    # -- write side ----------------------------------------------------
    async def draft_reply(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
        return await self._local.draft_reply(thread_id, to, subject, body)

    async def label(self, message_id: str, label: str) -> dict[str, Any]:
        return await self._local.label(message_id, label)

    async def file_followup(
        self, thread_id: str, title: str, due_date: str, note: str = ""
    ) -> dict[str, Any]:
        return await self._local.file_followup(thread_id, title, due_date, note)

    async def send_reply(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        return await self.sender.send(
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            cc=cc,
            bcc=bcc,
        )
