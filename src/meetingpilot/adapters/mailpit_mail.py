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


def _header_message_id(raw: str) -> str:
    """Extract the RFC 5322 ``Message-ID`` from a raw message, without the angle brackets."""
    head = raw.replace("\r\n", "\n").split("\n\n", 1)[0]
    for line in head.split("\n"):
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip().strip("<>")
    return ""


_PAGE_SIZE = 200
_MAX_SCAN = 2000


def _handle_number(value: Any) -> int:
    """Mailpit ids are increasing integers; a non-numeric handle sorts first."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


class MailpitMail:
    def __init__(self, settings: Settings, *, smtp_factory: Any | None = None):
        self.settings = settings
        self.sender = SmtpSender(settings, smtp_factory)
        self._local = MockMail(settings)
        self._base = settings.mailpit_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None

    # -- HTTP ----------------------------------------------------------
    def _fail(self, response: httpx.Response, action: str) -> None:
        detail = f"mailpit {action} failed: HTTP {response.status_code}"
        if response.status_code == 429:
            raise RateLimitError(detail)
        if response.status_code >= 500:
            raise ServerError(detail)
        raise ProviderError(detail)

    def _http_client(self) -> httpx.AsyncClient:
        """One pooled client per adapter, so TCP connections survive between polls."""
        if self._http is None or self._http.is_closed:
            kwargs: dict[str, Any] = {"timeout": self.settings.mailpit_timeout_s}
            if self.settings.mailpit_user:
                kwargs["auth"] = (self.settings.mailpit_user, self.settings.mailpit_password)
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def aclose(self) -> None:
        client, self._http = self._http, None
        if client is not None:
            await client.aclose()

    async def _get(self, path: str) -> httpx.Response:
        try:
            return await self._http_client().get(f"{self._base}{path}")
        except httpx.TimeoutException as error:
            raise ProviderTimeout(f"mailpit request timed out: {path}") from error
        except httpx.HTTPError as error:
            raise ProviderError(f"mailpit request failed: {error}") from error

    # -- read side -----------------------------------------------------
    async def list_new(
        self, mailbox: str = "inbox", limit: int = 10, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        if mailbox != "inbox":
            # Drafts live locally: Mailpit has no drafts concept.
            return await self._local.list_new(mailbox, limit, cursor)
        if cursor is None:
            messages = await self._messages()
        else:
            messages = (await self._messages_after(_handle_number(cursor)))[:limit]
        items: list[dict[str, Any]] = []
        for message in messages:
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

    async def _messages(self) -> list[dict[str, Any]]:
        response = await self._get("/api/v1/messages")
        if response.status_code != 200:
            self._fail(response, "list messages")
        return list(response.json().get("messages", []))

    async def _messages_after(self, floor: int) -> list[dict[str, Any]]:
        """Every message whose numeric id is above ``floor``, oldest first.

        Mailpit lists newest first, so pages are walked from the newest message and the
        matches are sorted; the caller takes the oldest ``limit`` and the next poll
        resumes from there.
        """
        found: list[dict[str, Any]] = []
        start = 0
        while start < _MAX_SCAN:
            response = await self._get(f"/api/v1/messages?start={start}&limit={_PAGE_SIZE}")
            if response.status_code != 200:
                self._fail(response, "list messages")
            batch = list(response.json().get("messages", []))
            if not batch:
                break
            found.extend(item for item in batch if _handle_number(item.get("ID")) > floor)
            start += len(batch)
            if len(batch) < _PAGE_SIZE:
                break
        found.sort(key=lambda item: _handle_number(item.get("ID")))
        return found

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
        # Report the RFC 5322 Message-ID like the IMAP adapter: dedupe keys on it, not on the
        # provider's internal handle.
        rfc_id = _header_message_id(response.text)
        return {"id": handle, "message_id": rfc_id or handle, "raw": response.text}

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
