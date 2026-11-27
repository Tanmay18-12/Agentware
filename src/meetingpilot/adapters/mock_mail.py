"""Folder-backed mailbox: ``sandbox/inbox`` in, ``sandbox/outbox`` and ``sandbox/drafts`` out."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

from ..config import Settings


class MockMail:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._followups = settings.sandbox_dir / "followups.jsonl"
        self._labels = settings.sandbox_dir / "labels.jsonl"
        settings.ensure_dirs()

    # -- read side -----------------------------------------------------
    async def list_new(self, mailbox: str = "inbox", limit: int = 10) -> list[dict[str, Any]]:
        folder = self.settings.inbox_dir if mailbox == "inbox" else self.settings.drafts_dir
        items = []
        for path in sorted(folder.glob("*")):
            if path.suffix.lower() not in {".json", ".eml", ".txt"}:
                continue
            items.append({"path": str(path), "name": path.name, "size_bytes": path.stat().st_size})
        return items[:limit]

    async def read(self, message_id: str) -> dict[str, Any] | None:
        for path in self.settings.inbox_dir.glob("*"):
            if message_id in path.name or message_id in path.read_text(errors="ignore")[:2000]:
                return {"path": str(path), "name": path.name}
        return None

    # -- write side ----------------------------------------------------
    def _write_eml(
        self,
        folder: Path,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to: str | None = None,
        references: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        message = EmailMessage()
        message["From"] = self.settings.prefs.owner_email
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
        path = folder / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.eml"
        path.write_text(message.as_string(), encoding="utf-8")
        return path

    async def draft_reply(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
        path = self._write_eml(
            self.settings.drafts_dir, to=to, subject=subject, body=body, thread_id=thread_id
        )
        return {"draft_id": path.stem, "path": str(path)}

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
        path = self._write_eml(
            self.settings.outbox_dir,
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            cc=cc,
            bcc=bcc,
        )
        return {"message_id": path.stem, "path": str(path)}

    async def label(self, message_id: str, label: str) -> dict[str, Any]:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "message_id": message_id, "label": label}
        with self._labels.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return {"ok": True, **record}

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
