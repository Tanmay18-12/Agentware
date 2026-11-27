"""Provider ports. The orchestrator never imports these directly; the gateway does."""

from __future__ import annotations

from typing import Any, Protocol


class ProviderError(RuntimeError):
    """Base class for adapter failures."""


class RateLimitError(ProviderError):
    """429 from a provider."""


class ServerError(ProviderError):
    """5xx from a provider."""


class ProviderTimeout(ProviderError):
    """Call exceeded its timeout."""


class MalformedPayload(ProviderError):
    """Provider returned something that does not match the contract."""


class MailProvider(Protocol):
    async def list_new(self, mailbox: str = "inbox", limit: int = 10) -> list[dict[str, Any]]: ...

    async def read(self, message_id: str) -> dict[str, Any] | None: ...

    async def draft_reply(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def label(self, message_id: str, label: str) -> dict[str, Any]: ...

    async def file_followup(
        self, thread_id: str, title: str, due_date: str, note: str = ""
    ) -> dict[str, Any]: ...


class CalendarProvider(Protocol):
    async def freebusy(self, start_utc: str, end_utc: str) -> list[dict[str, Any]]: ...

    async def get_own_event(self, event_id: str) -> dict[str, Any] | None: ...

    async def create_hold(
        self,
        title: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = "meetingpilot",
    ) -> dict[str, Any]: ...

    async def cancel_own_hold(self, event_id: str) -> dict[str, Any]: ...

    async def confirm_event(
        self,
        event_id: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = "meetingpilot",
    ) -> dict[str, Any]: ...

    async def list_events(self) -> list[dict[str, Any]]: ...
