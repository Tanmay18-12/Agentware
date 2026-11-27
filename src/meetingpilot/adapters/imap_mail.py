"""IMAP + SMTP mailbox for real mail servers (Dovecot, GreenMail, any RFC 3501 server).

Only the tools the registry exposes are implemented: list/read, draft, label (a folder copy),
reply via SMTP and a local follow-up file. Nothing ever deletes or moves a message.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import json
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import getaddresses
from typing import Any, Iterator

from ..config import Settings
from .base import ProviderError, ProviderTimeout, ServerError
from .smtp_mail import SmtpSender, build_message


class ImapMail:
    def __init__(self, settings: Settings, *, imap_factory: Any | None = None, smtp_factory: Any | None = None):
        self.settings = settings
        self.sender = SmtpSender(settings, smtp_factory)
        self._factory = imap_factory or (
            imaplib.IMAP4_SSL if settings.imap_ssl else imaplib.IMAP4
        )
        self._followups = settings.sandbox_dir / "followups.jsonl"
        settings.ensure_dirs()

    # -- connection ----------------------------------------------------
    @contextlib.contextmanager
    def _imap(self) -> Iterator[Any]:
        try:
            client = self._factory(
                self.settings.imap_host, self.settings.imap_port, timeout=10
            )
        except OSError as error:
            raise ProviderTimeout(f"cannot reach IMAP at {self.settings.imap_host}: {error}") from error
        try:
            if self.settings.imap_user:
                client.login(self.settings.imap_user, self.settings.imap_password)
            yield client
        except imaplib.IMAP4.error as error:
            raise ProviderError(f"imap error: {error}") from error
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - closing a broken session is best effort
                pass

    def _select(self, client: Any, folder: str) -> None:
        status, _ = client.select(folder, readonly=False)
        if status != "OK":
            raise ProviderError(f"cannot select IMAP folder {folder!r}")

    def _create(self, client: Any, folder: str) -> None:
        try:
            client.create(folder)
        except Exception:  # noqa: BLE001 - several servers reject CREATE for existing folders
            pass

    def _search(self, client: Any, query: str) -> list[bytes]:
        status, data = client.uid("search", None, query)
        if status != "OK":
            raise ProviderError(f"imap search failed: {query}")
        return list((data[0] or b"").split())

    # -- read side -----------------------------------------------------
    async def list_new(self, mailbox: str = "inbox", limit: int = 10) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_new_sync, mailbox, limit)

    def _list_new_sync(self, mailbox: str, limit: int) -> list[dict[str, Any]]:
        folder = self.settings.imap_mailbox if mailbox == "inbox" else self.settings.imap_drafts_mailbox
        with self._imap() as client:
            self._select(client, folder)
            uids = self._search(client, "ALL")[-limit:]
            items: list[dict[str, Any]] = []
            for uid in uids:
                status, data = client.uid(
                    "fetch", uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
                )
                if status != "OK" or not isinstance(data, list):
                    continue
                header = next((part[1] for part in data if isinstance(part, tuple)), b"")
                parsed = message_from_bytes(header)
                items.append(
                    {
                        "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                        "message_id": str(parsed.get("Message-ID", "")).strip("<> "),
                        "subject": str(parsed.get("Subject", "")),
                        "from_addr": _first_address(str(parsed.get("From", ""))),
                        "size_bytes": _size_from(data),
                    }
                )
            return items

    async def read(self, message_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_sync, message_id)

    def _read_sync(self, message_id: str) -> dict[str, Any] | None:
        handle = message_id.strip("<> ")
        with self._imap() as client:
            self._select(client, self.settings.imap_mailbox)
            uid = self._resolve_uid(client, handle)
            if uid is None:
                return None
            status, data = client.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not isinstance(data, list):
                raise ProviderError("imap fetch returned no message")
            raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
            parsed = message_from_bytes(raw)
            return {
                "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                "message_id": str(parsed.get("Message-ID", "")).strip("<> ") or handle,
                "raw": raw.decode("utf-8", errors="replace"),
            }

    def _resolve_uid(self, client: Any, handle: str) -> bytes | None:
        if handle.isdigit():
            return handle.encode()
        uids = self._search(client, f'HEADER Message-ID "{handle}"')
        return uids[-1] if uids else None

    # -- write side ----------------------------------------------------
    async def draft_reply(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._draft_sync, thread_id, to, subject, body)

    def _draft_sync(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
        message = build_message(
            self.settings, to=to, subject=subject, body=body, thread_id=thread_id
        )
        folder = self.settings.imap_drafts_mailbox
        with self._imap() as client:
            self._create(client, folder)
            status, _ = client.append(folder, r"(\Draft)", None, message.as_string().encode())
        if status != "OK":
            raise ProviderError(f"imap append to {folder!r} failed")
        return {
            "draft_id": str(message["Message-ID"]).strip("<>"),
            "path": f"imap://{folder}/{message['Message-ID']}",
        }

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

    async def label(self, message_id: str, label: str) -> dict[str, Any]:
        """Copy into a label folder. Never move or delete: IMAP servers disagree on folders."""
        try:
            return await asyncio.to_thread(self._label_sync, message_id, label)
        except (ProviderError, ServerError, ProviderTimeout) as error:
            # Labelling is cosmetic; a server without folder support must not fail the run.
            return {"ok": False, "label": label, "reason": str(error)}

    def _label_sync(self, message_id: str, label: str) -> dict[str, Any]:
        folder = "".join(ch for ch in label if ch.isalnum() or ch in " ._-").strip() or "meetingpilot"
        with self._imap() as client:
            self._select(client, self.settings.imap_mailbox)
            uids = self._search(client, f'HEADER Message-ID "{message_id.strip("<> ")}"')
            if not uids:
                return {"ok": False, "label": label, "reason": "message not found"}
            self._create(client, folder)
            copied = 0
            for uid in uids:
                status, _ = client.uid("copy", uid, folder)
                copied += 1 if status == "OK" else 0
        if not copied:
            return {"ok": False, "label": label, "reason": "server rejected the copy"}
        return {"ok": True, "label": label, "folder": folder, "copied": copied}

    async def file_followup(
        self, thread_id: str, title: str, due_date: str, note: str = ""
    ) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "title": title,
            "due_date": due_date,
            "note": note,
        }
        with self._followups.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return {"followup_id": uuid.uuid4().hex[:12], **record}


def _first_address(value: str) -> str:
    pairs = getaddresses([value])
    return pairs[0][1] if pairs and pairs[0][1] else value


def _size_from(data: list[Any]) -> int:
    for part in data:
        if isinstance(part, tuple) and isinstance(part[0], bytes) and b"RFC822.SIZE" in part[0].upper():
            rest = part[0].upper().split(b"RFC822.SIZE", 1)[1].split(b")", 1)[0].split()
            try:
                return int(rest[0])
            except (IndexError, ValueError):
                return 0
    return 0
