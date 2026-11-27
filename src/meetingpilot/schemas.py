"""Strict data models. Every model forbids unknown fields (``extra='forbid'``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Pipeline data
# --------------------------------------------------------------------------
class EmailEnvelope(Strict):
    message_id: str = Field(min_length=1, max_length=320)
    thread_id: str = Field(min_length=1, max_length=320)
    from_addr: str = Field(min_length=3, max_length=320)
    reply_to: str | None = Field(default=None, max_length=320)
    return_path: str | None = Field(default=None, max_length=320)
    auth_results: dict[str, str] = Field(default_factory=dict)
    subject: str = Field(default="", max_length=300)
    body_text: str = Field(default="", max_length=8000)
    hidden_text_flag: bool = False
    header_flags: list[str] = Field(default_factory=list)
    received_at: datetime
    size_bytes: int = Field(default=0, ge=0)


class TimeWindow(Strict):
    start_iso: str | None = None
    end_iso: str | None = None
    raw_phrase: str = ""
    tz: str = "UTC"
    flexibility: Literal["exact", "window", "flexible"] = "window"


class MeetingIntent(Strict):
    kind: Literal[
        "meeting_request",
        "reschedule",
        "cancel",
        "slot_selection",
        "not_meeting",
        "other",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    title: str = Field(default="Meeting", max_length=120)
    duration_min: int = Field(default=30, ge=0, le=1440)
    windows: list[TimeWindow] = Field(default_factory=list)
    attendees: list[str] = Field(default_factory=list, max_length=20)
    location_pref: str | None = Field(default=None, max_length=200)
    ambiguity: list[str] = Field(default_factory=list, max_length=10)
    injection_suspected: bool = False
    rationale: str = Field(default="", max_length=2000)
    selected_slot: int | None = Field(default=None, ge=0, le=10)


class SlotCandidate(Strict):
    start_utc: datetime
    end_utc: datetime
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list, max_length=10)


class Decision(Strict):
    kind: Literal["BOOK", "PROPOSE", "CLARIFY", "ESCALATE", "FILE", "QUARANTINE"]
    reasons: list[str] = Field(default_factory=list, max_length=10)
    slots: list[SlotCandidate] = Field(default_factory=list, max_length=5)


class ActionRequest(Strict):
    id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)
    risk_tier: Literal["READ", "REVERSIBLE", "IRREVERSIBLE"] = "READ"


class PolicyDecision(Strict):
    action_id: str
    verdict: Literal["ALLOW", "GATE", "BLOCK", "LIMIT"]
    rule_ids: list[str] = Field(default_factory=list, max_length=12)
    explanation: str = Field(default="", max_length=1000)


class RunEvent(Strict):
    seq: int = Field(ge=1)
    ts: str
    run_id: str
    step_id: str
    parent_id: str | None = None
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0.0)
    prev_hash: str
    hash: str


# --------------------------------------------------------------------------
# Per-tool argument schemas (rule P3)
# --------------------------------------------------------------------------
class MailListNewArgs(Strict):
    mailbox: str = Field(default="inbox", max_length=40)
    limit: int = Field(default=10, ge=1, le=50)


class MailReadArgs(Strict):
    message_id: str = Field(min_length=1, max_length=320)


class FreeBusyArgs(Strict):
    start_utc: datetime
    end_utc: datetime


class GetOwnEventArgs(Strict):
    event_id: str = Field(min_length=1, max_length=64)


class CreateHoldArgs(Strict):
    title: str = Field(min_length=1, max_length=120)
    start_utc: datetime
    end_utc: datetime
    attendees: list[str] = Field(default_factory=list, max_length=20)
    agent_tag: str = Field(default="meetingpilot", max_length=64)


class CancelHoldArgs(Strict):
    event_id: str = Field(min_length=1, max_length=64)


class ConfirmEventArgs(Strict):
    event_id: str = Field(min_length=1, max_length=64)
    start_utc: datetime
    end_utc: datetime
    attendees: list[str] = Field(default_factory=list, max_length=20)
    agent_tag: str = Field(default="meetingpilot", max_length=64)


class DraftReplyArgs(Strict):
    thread_id: str = Field(min_length=1, max_length=320)
    to: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=4000)


class SendReplyArgs(Strict):
    thread_id: str = Field(min_length=1, max_length=320)
    to: str = Field(min_length=3, max_length=320)
    cc: list[str] = Field(default_factory=list, max_length=10)
    bcc: list[str] = Field(default_factory=list, max_length=10)
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=4000)
    in_reply_to: str | None = Field(default=None, max_length=320)
    references: str | None = Field(default=None, max_length=1000)


class LabelArgs(Strict):
    message_id: str = Field(min_length=1, max_length=320)
    label: str = Field(min_length=1, max_length=60)


class FollowupArgs(Strict):
    thread_id: str = Field(min_length=1, max_length=320)
    title: str = Field(min_length=1, max_length=200)
    due_date: str = Field(min_length=4, max_length=40)
    note: str = Field(default="", max_length=500)
