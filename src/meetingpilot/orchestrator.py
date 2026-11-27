"""The 10-stage deterministic orchestrator. One run per email."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .adapters.base import ProviderError
from .config import Prefs, Settings
from .event_bus import EventBus
from .kill_switch import KilledError, KillSwitch
from .pipeline.composer import (
    OutputGuard,
    render_clarification,
    render_confirmation,
    render_proposal,
)
from .pipeline.ingest import DuplicateMessage, Ingest, MalformedMessage
from .pipeline.prefilter import prefilter
from .pipeline.reader_llm import ReaderLLM, ReaderUnavailable
from .pipeline.scheduler import SlotCandidate, compute_slots
from .pipeline.screen import ScreenResult, risk_flags, screen
from .pipeline.validator import validate_intent
from .policy_gateway import AGENT_TAG, GatewayResult, PolicyGateway, ToolRuntime, new_action
from .schemas import Decision, EmailEnvelope, MeetingIntent

MINUTES_SAVED = {
    "BOOKED": 7.5,
    "PROPOSED": 4.0,
    "NEEDS_CLARIFICATION": 1.5,
    "ESCALATED": 3.0,
    "FILED": 0.5,
    "QUARANTINED": 0.0,
    "SKIPPED": 0.0,
    "HALTED": 0.0,
    "DUPLICATE": 0.0,
    "COMPENSATED": 0.0,
    "FAILED": 0.0,
}

# Identity, money and stranger risk stop the run before any clarification.
IMMEDIATE_ESCALATION_RISKS = frozenset(
    {
        "unknown_external_domain",
        "vip_sender",
        "external_attendees",
        "money_or_legal_wording",
        "too_many_attendees",
    }
)
# A window the reader could not pin down is a question for the sender, not a guess.
AMBIGUITY_REASONS = frozenset(
    {"time_unresolved", "no_date_time", "invalid_time", "timezone_missing"}
)


@dataclass
class RunSummary:
    run_id: str
    message_id: str
    outcome: str
    decision: str = "FILE"
    reasons: list[str] = field(default_factory=list)
    slots: list[dict[str, Any]] = field(default_factory=list)
    booking: dict[str, Any] | None = None
    human_interventions: int = 0
    minutes_saved: float = 0.0
    cost_usd: float = 0.0
    tool_calls: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "message_id": self.message_id,
            "outcome": self.outcome,
            "decision": self.decision,
            "reasons": self.reasons,
            "slots": self.slots,
            "booking": self.booking,
            "human_interventions": self.human_interventions,
            "minutes_saved": self.minutes_saved,
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "errors": self.errors,
        }


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        mail: Any,
        calendar: Any,
        kill: KillSwitch,
        gateway: PolicyGateway | None = None,
        reader: ReaderLLM | None = None,
    ):
        self.settings = settings
        self.bus = bus
        self.store = bus.store
        self.prefs: Prefs = settings.prefs
        self.mail = mail
        self.calendar = calendar
        self.kill = kill
        self.guard = OutputGuard(self.prefs)
        self.gateway = gateway or PolicyGateway(bus, self.prefs, settings, kill)
        self.reader = reader or ReaderLLM(settings, self.prefs)
        self.runtime = ToolRuntime(
            mail=mail,
            calendar=calendar,
            guard=self.guard,
            prefs=self.prefs,
            settings=settings,
        )
        self._contexts: dict[str, dict[str, Any]] = {}

    # ==================================================================
    # public entry points
    # ==================================================================
    async def handle_path(self, path: Path) -> RunSummary:
        try:
            envelope = Ingest(self.settings, self.store).from_path(path)
        except DuplicateMessage as error:
            return self._finish_duplicate(str(error))
        except (MalformedMessage, ValueError) as error:
            return self._finish_malformed(str(error))
        return await self.handle(envelope)

    async def handle(self, envelope: EmailEnvelope) -> RunSummary:
        """Run with the kill switch able to cancel work mid-flight."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.store.start_run(run_id, envelope.message_id, _now())
        summary = RunSummary(run_id=run_id, message_id=envelope.message_id, outcome="FAILED")
        self.bus.emit(
            run_id,
            "init",
            "run_start",
            {
                "message_id": envelope.message_id,
                "from": envelope.from_addr,
                "subject": envelope.subject,
                "thread_id": envelope.thread_id,
                "llm_mode": self.settings.llm_mode,
                "mock_mode": self.settings.mock_mode,
                "human_interventions": 0,
            },
        )

        task = asyncio.create_task(self._run_stages(run_id, envelope, summary))
        watcher = asyncio.create_task(self.kill.watch(task))
        try:
            await task
        except asyncio.CancelledError:
            summary.outcome = "HALTED"
            summary.decision = "ESCALATE"
            summary.reasons.append("kill switch cancelled the run")
            self.bus.emit(
                run_id,
                "kill",
                "kill",
                {
                    "state": "HALTED",
                    "triggered_at": self.kill.triggered_at,
                    "triggered_by": self.kill.triggered_by,
                    "reason": self.kill.reason,
                    "snapshot": {"outcome": summary.outcome, "decision": summary.decision},
                },
            )
        except KilledError as error:
            summary.outcome = "HALTED"
            summary.decision = "ESCALATE"
            summary.reasons.append(str(error))
            self.bus.emit(
                run_id,
                "kill",
                "kill",
                {
                    "state": "HALTED",
                    "triggered_at": self.kill.triggered_at,
                    "triggered_by": self.kill.triggered_by,
                    "reason": self.kill.reason,
                    "snapshot": {"outcome": "HALTED", "detail": str(error)},
                },
            )
        except Exception as error:  # fail closed: never let a stage crash the worker
            summary.errors.append(f"{type(error).__name__}: {error}")
            summary.outcome = "ESCALATED"
            summary.decision = "ESCALATE"
            summary.reasons.append(f"internal failure, failing closed ({type(error).__name__})")
            self.bus.emit(
                run_id,
                "record",
                "run_error",
                {"error": type(error).__name__, "detail": str(error)[:400]},
            )
        finally:
            watcher.cancel()
            self._finalize(summary)
        return summary

    # ==================================================================
    # stages
    # ==================================================================
    async def _run_stages(self, run_id: str, envelope: EmailEnvelope, summary: RunSummary) -> None:
        self._enter(run_id, 1, "ingest")
        self.bus.emit(
            run_id,
            "ingest",
            "ingested",
            {
                "message_id": envelope.message_id,
                "bytes": envelope.size_bytes,
                "hidden_text": envelope.hidden_text_flag,
                "header_flags": envelope.header_flags,
            },
            parent_id="1_ingest",
        )
        self._leave(run_id, 1, "ingest")

        # ---- 2 prefilter ---------------------------------------------
        self._enter(run_id, 2, "prefilter")
        self.kill.check()
        pre = prefilter(envelope, self.prefs, self.store)
        if pre.skip:
            self.bus.emit(run_id, "prefilter", "skipped", {"reason": pre.reason})
            summary.outcome = "SKIPPED"
            summary.decision = "FILE"
            summary.reasons.append(pre.reason)
            self._leave(run_id, 2, "prefilter", "skipped")
            return
        self._leave(run_id, 2, "prefilter")

        # ---- 3 screen ------------------------------------------------
        self._enter(run_id, 3, "screen")
        self.kill.check()
        screening = screen(envelope)
        self.bus.emit(
            run_id,
            "screen",
            "screened",
            {
                "injection_score": screening.injection_score,
                "flags": list(screening.flags),
                "spoof": screening.spoof,
            },
        )
        if screening.quarantine:
            summary.outcome = "QUARANTINED"
            summary.decision = "QUARANTINE"
            summary.reasons.extend(screening.flags or ["injection score above threshold"])
            self._record_decision(run_id, summary, Decision(kind="QUARANTINE", reasons=summary.reasons))
            self._leave(run_id, 3, "screen", "quarantined")
            return
        self._leave(run_id, 3, "screen")

        # ---- 4 reader LLM (zero tools) -------------------------------
        self._enter(run_id, 4, "reader_llm")
        self.kill.check()
        try:
            reading = await self.reader.read(envelope)
        except (ReaderUnavailable, ProviderError) as error:
            summary.errors.append(f"reader: {error}")
            summary.outcome = "ESCALATED"
            summary.decision = "ESCALATE"
            summary.reasons.append("reader LLM unavailable; failing closed")
            self.bus.emit(run_id, "reader_llm", "reader_failed", {"error": str(error)})
            await self._escalate(run_id, envelope, summary, MeetingIntent(
                kind="other", confidence=0.0, title=envelope.subject[:120] or "Meeting"
            ), [], ["reader unavailable"])
            self._leave(run_id, 4, "reader_llm", "failed")
            return
        summary.cost_usd += reading.cost_usd
        self.store.bump_total(f"cost:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}", reading.cost_usd)
        self.store.finish_run(run_id, cost_usd=self.store.total(f"cost:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"))
        self.bus.emit(
            run_id,
            "reader_llm",
            "llm_call",
            {
                "mode": reading.mode,
                "model": reading.model,
                "tokens": reading.tokens,
                "cost_usd": reading.cost_usd,
                "tools": [],
                "cassette_hit": reading.cassette_hit,
                "intent": reading.intent.model_dump(mode="json"),
            },
            cost_usd=reading.cost_usd,
        )
        intent = reading.intent
        self._leave(run_id, 4, "reader_llm")

        # ---- 5 validate ----------------------------------------------
        self._enter(run_id, 5, "validate")
        self.kill.check()
        validation = validate_intent(intent, self.prefs, envelope.received_at)
        self.bus.emit(
            run_id,
            "validate",
            "validated",
            {"ok": validation.ok, "issues": validation.issues, "duration_min": validation.duration_min},
        )
        if validation.hard_fail:
            summary.outcome = "NEEDS_CLARIFICATION"
            summary.decision = "CLARIFY"
            summary.reasons.extend(validation.issues)
            await self._clarify(run_id, envelope, intent, summary, validation.issues[0] if validation.issues else None)
            self._leave(run_id, 5, "validate", "invalid")
            return
        self._leave(run_id, 5, "validate")

        # ---- 6 schedule ----------------------------------------------
        self._enter(run_id, 6, "schedule")
        self.kill.check()
        busy = await self._read_busy(run_id, envelope)
        if busy is None:
            summary.outcome = "ESCALATED"
            summary.decision = "ESCALATE"
            summary.reasons.append("calendar free/busy unavailable")
            await self._escalate(run_id, envelope, summary, intent, [], ["calendar unavailable"])
            self._leave(run_id, 6, "schedule", "failed")
            return
        resolution_issues: list[str] = []
        slots = compute_slots(
            intent, self.prefs, busy, envelope.received_at, issues=resolution_issues
        )
        ambiguous = [issue for issue in resolution_issues if "ambiguous" in issue]
        if ambiguous:
            # Never guess which side of a fall-back hour the requester meant.
            summary.outcome = "NEEDS_CLARIFICATION"
            summary.decision = "CLARIFY"
            summary.reasons.extend(ambiguous)
            self.bus.emit(run_id, "schedule", "ambiguous_time", {"issues": ambiguous})
            await self._clarify(
                run_id,
                envelope,
                intent,
                summary,
                "That time occurs twice because the clocks change. Which one did you mean? "
                "Please reply with the time including an explicit UTC offset (e.g. 09:00+05:30).",
            )
            self._leave(run_id, 6, "schedule", "ambiguous")
            return
        self._contexts[run_id] = {
            "envelope": envelope.model_dump(mode="json"),
            "intent": intent.model_dump(mode="json"),
            "busy": [dict(item) for item in busy],
            "screen": {
                "injection_score": screening.injection_score,
                "flags": list(screening.flags),
                "spoof": screening.spoof,
            },
            "slots": [slot.model_dump(mode="json") for slot in slots],
        }
        self.bus.emit(
            run_id,
            "schedule",
            "scheduled",
            {"busy_intervals": len(busy), "candidates": [s.model_dump(mode="json") for s in slots]},
        )
        self._leave(run_id, 6, "schedule")

        # ---- 7 decide ------------------------------------------------
        self._enter(run_id, 7, "decide")
        decision = self._decide(envelope, intent, slots, screening)
        self._record_decision(run_id, summary, decision)
        summary.slots = [slot.model_dump(mode="json") for slot in decision.slots]
        self._leave(run_id, 7, "decide", decision.kind.lower())

        # ---- 8 act ---------------------------------------------------
        self._enter(run_id, 8, "act")
        self.kill.check()
        if decision.kind == "BOOK":
            await self._act_book(run_id, envelope, intent, decision, summary)
        elif decision.kind == "PROPOSE":
            await self._act_propose(run_id, envelope, intent, decision, summary)
        elif decision.kind == "CLARIFY":
            summary.outcome = "NEEDS_CLARIFICATION"
            await self._clarify(run_id, envelope, intent, summary, None)
        elif decision.kind == "ESCALATE":
            await self._escalate(run_id, envelope, summary, intent, decision.slots, decision.reasons)
            summary.outcome = "ESCALATED"
        elif decision.kind == "FILE":
            await self._act_file(run_id, envelope, summary)
        self._leave(run_id, 8, "act", summary.outcome.lower())

        # ---- 9 verify ------------------------------------------------
        if decision.kind == "BOOK" and summary.booking:
            self._enter(run_id, 9, "verify")
            await self._verify_booking(run_id, envelope, summary)
            self._leave(run_id, 9, "verify", summary.outcome.lower())

    # ==================================================================
    # decisions
    # ==================================================================
    def _decide(
        self,
        envelope: EmailEnvelope,
        intent: MeetingIntent,
        slots: list[SlotCandidate],
        screening: ScreenResult,
    ) -> Decision:
        if screening.spoof and screening.injection_score < 0.5:
            return Decision(
                kind="ESCALATE", reasons=["header identity mismatch (From / Reply-To / auth)"]
            )

        if intent.injection_suspected or screening.injection_score >= 0.5:
            return Decision(kind="QUARANTINE", reasons=["injection suspected"])

        thread = self.store.get_thread_state(envelope.thread_id)

        if intent.kind == "not_meeting":
            return Decision(kind="FILE", reasons=["not a meeting request"])

        if intent.kind == "cancel":
            return Decision(
                kind="ESCALATE",
                reasons=["cancellation requested: the agent never deletes or edits events"],
            )

        if intent.kind == "reschedule" and not (thread and thread["status"] == "booked"):
            return Decision(kind="ESCALATE", reasons=["rescheduling requires human confirmation"])

        if intent.kind == "slot_selection":
            if thread and thread["status"] == "proposed":
                chosen = intent.selected_slot
                pending = thread["payload"].get("slots", [])
                if chosen is not None and 0 <= chosen < len(pending):
                    slot = SlotCandidate.model_validate(pending[chosen])
                    return Decision(
                        kind="BOOK",
                        reasons=[f"thread owner picked option {chosen + 1}"],
                        slots=[slot],
                    )
                return Decision(kind="CLARIFY", reasons=["selected option not understood"])
            return Decision(kind="ESCALATE", reasons=["slot selection without a pending proposal"])

        if thread and thread["status"] == "proposed" and intent.kind == "meeting_request":
            # A reply in a proposed thread that is not a selection: keep negotiating, never double-book.
            return Decision(
                kind="PROPOSE",
                reasons=["thread already has a pending proposal"],
                slots=slots[: self.prefs.max_proposals],
            )

        risks = risk_flags(envelope, intent, self.prefs)
        blocking = [risk for risk in risks if risk in IMMEDIATE_ESCALATION_RISKS]
        if blocking:
            return Decision(kind="ESCALATE", reasons=blocking, slots=slots[:1])

        if not intent.windows or AMBIGUITY_REASONS & set(intent.ambiguity):
            return Decision(
                kind="CLARIFY", reasons=intent.ambiguity or ["no time window in the request"]
            )

        soft = [risk for risk in risks if risk not in IMMEDIATE_ESCALATION_RISKS]
        if soft:
            return Decision(kind="ESCALATE", reasons=soft, slots=slots[:1])

        if not slots:
            return Decision(kind="ESCALATE", reasons=["no conflict-free slot inside the policy"])

        exact = self._exact_requested_start(intent)
        if exact is not None:
            if slots[0].start_utc == exact:
                return Decision(kind="BOOK", reasons=["requested slot is free"], slots=[slots[0]])
            return Decision(
                kind="PROPOSE",
                reasons=["requested slot is unavailable"],
                slots=slots[: self.prefs.max_proposals],
            )

        return Decision(kind="BOOK", reasons=["conflict-free slot found"], slots=[slots[0]])

    def _exact_requested_start(self, intent: MeetingIntent) -> datetime | None:
        for window in intent.windows:
            if window.flexibility == "exact" and window.start_iso:
                try:
                    return datetime.fromisoformat(window.start_iso.replace("Z", "+00:00")).astimezone(
                        timezone.utc
                    )
                except ValueError:
                    return None
        return None

    # ==================================================================
    # actions
    # ==================================================================
    async def _act_book(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        intent: MeetingIntent,
        decision: Decision,
        summary: RunSummary,
    ) -> None:
        candidates = list(decision.slots)
        for attempt, slot in enumerate(candidates[:3]):
            hold = await self._act(
                run_id,
                "act",
                "calendar.create_hold",
                {
                    "title": intent.title,
                    "start_utc": slot.start_utc.isoformat(),
                    "end_utc": slot.end_utc.isoformat(),
                    "attendees": [envelope.from_addr],
                    "agent_tag": AGENT_TAG,
                },
                envelope,
            )
            if not hold.allowed:
                continue
            event_id = (hold.result or {}).get("event_id")
            if not event_id:
                continue

            # Two-phase commit: re-check free/busy immediately before confirming.
            recheck = await self._act(
                run_id,
                "verify",
                "calendar.freebusy",
                {"start_utc": slot.start_utc.isoformat(), "end_utc": slot.end_utc.isoformat()},
                envelope,
                fresh=True,
            )
            conflict = False
            if recheck.allowed:
                for interval in recheck.result or []:
                    if interval.get("event_id") == event_id:
                        continue
                    if interval["start_utc"] < slot.end_utc.isoformat() and interval[
                        "end_utc"
                    ] > slot.start_utc.isoformat():
                        conflict = True
                        break
            if conflict:
                await self._act(
                    run_id,
                    "verify",
                    "calendar.cancel_own_hold",
                    {"event_id": event_id},
                    envelope,
                )
                self.bus.emit(
                    run_id, "act", "race_detected", {"event_id": event_id, "attempt": attempt + 1}
                )
                continue

            confirm = await self._act(
                run_id,
                "act",
                "calendar.confirm_event",
                {
                    "event_id": event_id,
                    "start_utc": slot.start_utc.isoformat(),
                    "end_utc": slot.end_utc.isoformat(),
                    "attendees": [envelope.from_addr],
                    "agent_tag": AGENT_TAG,
                },
                envelope,
            )
            if not confirm.allowed:
                await self._act(
                    run_id, "verify", "calendar.cancel_own_hold", {"event_id": event_id}, envelope
                )
                await self._gate_downgrade(run_id, envelope, intent, summary, confirm, [slot])
                return

            meeting_url = f"{self.prefs.meeting_url_prefix.rstrip('/')}/{run_id}"
            subject, body = render_confirmation(intent, slot, self.prefs, meeting_url)
            summary.booking = {
                "event_id": event_id,
                "start_utc": slot.start_utc.isoformat(),
                "end_utc": slot.end_utc.isoformat(),
                "title": intent.title,
                "meeting_url": meeting_url,
            }
            sent = await self._send(run_id, envelope, summary, intent, subject, body, [slot])
            if sent is None:
                return
            await self._act(
                run_id,
                "act",
                "followup.file",
                {
                    "thread_id": envelope.thread_id,
                    "title": f"Follow up on {intent.title}",
                    "due_date": (slot.start_utc + timedelta(days=1)).date().isoformat(),
                    "note": "Confirm the meeting happened and capture notes.",
                },
                envelope,
            )
            await self._label(run_id, envelope, "meeting-booked")
            self.store.set_thread_state(
                envelope.thread_id,
                run_id,
                "booked",
                {"slots": [slot.model_dump(mode="json")], "event_id": event_id},
            )
            summary.outcome = "BOOKED"
            return

        summary.outcome = "ESCALATED"
        summary.reasons.append("no hold could be confirmed after re-checks")
        await self._escalate(run_id, envelope, summary, intent, candidates[:1], summary.reasons)

    async def _act_propose(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        intent: MeetingIntent,
        decision: Decision,
        summary: RunSummary,
    ) -> None:
        slots = decision.slots[: self.prefs.max_proposals]
        subject, body = render_proposal(intent, slots, self.prefs)
        sent = await self._send(run_id, envelope, summary, intent, subject, body, slots)
        if sent is None:
            return
        self.store.set_thread_state(
            envelope.thread_id,
            run_id,
            "proposed",
            {
                "slots": [slot.model_dump(mode="json") for slot in slots],
                "event_title": intent.title,
                "duration_min": intent.duration_min,
            },
        )
        await self._label(run_id, envelope, "meeting-proposed")
        summary.outcome = "PROPOSED"

    async def _clarify(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        intent: MeetingIntent,
        summary: RunSummary,
        question: str | None,
    ) -> None:
        subject, body = render_clarification(intent, self.prefs, question)
        sent = await self._send(run_id, envelope, summary, intent, subject, body, [])
        if sent is None:
            return
        summary.outcome = "NEEDS_CLARIFICATION"
        summary.decision = "CLARIFY"
        self.store.set_thread_state(
            envelope.thread_id, run_id, "clarifying", {"question": question or "time/duration"}
        )

    async def _act_file(self, run_id: str, envelope: EmailEnvelope, summary: RunSummary) -> None:
        await self._label(run_id, envelope, "not-a-meeting")
        self.bus.emit(run_id, "act", "digest_entry", {"subject": envelope.subject, "from": envelope.from_addr})
        summary.outcome = "FILED"
        summary.decision = "FILE"

    async def _escalate(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        summary: RunSummary,
        intent: MeetingIntent,
        slots: list[SlotCandidate],
        reasons: list[str],
    ) -> None:
        subject, body = render_proposal(intent, slots, self.prefs) if slots else render_clarification(
            intent, self.prefs
        )
        draft = await self._act(
            run_id,
            "act",
            "mail.draft_reply",
            {
                "thread_id": envelope.thread_id,
                "to": envelope.from_addr,
                "subject": subject,
                "body": body,
            },
            envelope,
        )
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        self.store.add_approval(
            approval_id,
            run_id,
            "escalation",
            {
                "to": envelope.from_addr,
                "thread_id": envelope.thread_id,
                "subject": subject,
                "body": body,
                "slots": [slot.model_dump(mode="json") for slot in slots],
                "reasons": reasons,
                "draft": (draft.result or {}) if draft.allowed else None,
            },
        )
        self.bus.emit(
            run_id,
            "act",
            "approval_queued",
            {"approval_id": approval_id, "reasons": reasons, "to": envelope.from_addr},
        )
        summary.outcome = "ESCALATED"
        summary.decision = "ESCALATE"

    async def _gate_downgrade(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        intent: MeetingIntent,
        summary: RunSummary,
        gate: GatewayResult,
        slots: list[SlotCandidate],
    ) -> None:
        await self._escalate(
            run_id,
            envelope,
            summary,
            intent,
            slots,
            [f"{gate.decision.verdict}: {gate.decision.explanation}"] + gate.decision.rule_ids,
        )

    async def _send(
        self,
        run_id: str,
        envelope: EmailEnvelope,
        summary: RunSummary,
        intent: MeetingIntent,
        subject: str,
        body: str,
        slots: list[SlotCandidate],
    ) -> GatewayResult | None:
        result = await self._act(
            run_id,
            "act",
            "mail.send_reply",
            {
                "thread_id": envelope.thread_id,
                "to": envelope.from_addr,
                "cc": [],
                "bcc": [],
                "subject": subject,
                "body": body,
                "in_reply_to": envelope.message_id,
                "references": envelope.message_id,
            },
            envelope,
        )
        if result.allowed:
            return result
        await self._gate_downgrade(run_id, envelope, intent, summary, result, slots)
        return None

    async def _label(self, run_id: str, envelope: EmailEnvelope, label: str) -> None:
        await self._act(
            run_id,
            "act",
            "mail.label",
            {"message_id": envelope.message_id, "label": label},
            envelope,
        )

    async def _read_busy(
        self, run_id: str, envelope: EmailEnvelope
    ) -> list[dict[str, Any]] | None:
        now = envelope.received_at
        horizon = now + timedelta(days=self.prefs.horizon_days)
        action = new_action(
            run_id,
            "schedule",
            "calendar.freebusy",
            {"start_utc": now.isoformat(), "end_utc": horizon.isoformat()},
            envelope.message_id,
        )
        result = await self.gateway.execute(action, self.runtime, envelope)
        if not result.allowed:
            return None
        return list(result.result or [])

    async def _verify_booking(
        self, run_id: str, envelope: EmailEnvelope, summary: RunSummary
    ) -> None:
        booking = summary.booking or {}
        event_id = booking.get("event_id")
        read_back = await self._act(
            run_id, "verify", "calendar.get_own_event", {"event_id": event_id}, envelope, fresh=True
        )
        event = read_back.result if read_back.allowed else None
        problems: list[str] = []
        if not event or event.get("status") != "confirmed":
            problems.append("confirmed event not found on read-back")
        else:
            if event.get("start_utc") != booking.get("start_utc"):
                problems.append("event start does not match the confirmed slot")
            if event.get("agent_tag") != AGENT_TAG:
                problems.append("event is not agent-tagged")

        if read_back.allowed and event:
            busy = await self._act(
                run_id,
                "verify",
                "calendar.freebusy",
                {"start_utc": booking["start_utc"], "end_utc": booking["end_utc"]},
                envelope,
                fresh=True,
            )
            for interval in busy.result or []:
                if interval.get("event_id") == event_id:
                    continue
                if interval["start_utc"] < booking["end_utc"] and interval["end_utc"] > booking["start_utc"]:
                    problems.append("overlap detected on post-commit verification")
                    break

        if problems:
            summary.errors.extend(problems)
            summary.outcome = "COMPENSATED"
            await self._act(
                run_id, "verify", "calendar.cancel_own_hold", {"event_id": event_id}, envelope
            )
            self.bus.emit(run_id, "verify", "verification_failed", {"problems": problems})
            self.store.set_thread_state(
                envelope.thread_id, run_id, "needs_human", {"problems": problems}
            )
        else:
            self.bus.emit(run_id, "verify", "verified", {"event_id": event_id})

    async def approve(self, approval_id: str) -> dict[str, Any]:
        """Human approval path: re-validated by the gateway, never bypassed."""
        approval = next(
            (item for item in self.store.list_approvals("pending") if item["id"] == approval_id),
            None,
        )
        if approval is None:
            return {"ok": False, "reason": "approval not found or already decided"}
        payload = approval["payload"]
        run_id = approval["run_id"]
        envelope = EmailEnvelope(
            message_id=str(payload.get("message_id") or f"approval-{approval_id}"),
            thread_id=str(payload["thread_id"]),
            from_addr=str(payload["to"]),
            received_at=datetime.now(timezone.utc),
        )
        booked: dict[str, Any] | None = None
        for slot_data in payload.get("slots") or []:
            slot = SlotCandidate.model_validate(slot_data)
            hold = await self._act(
                run_id,
                "act",
                "calendar.create_hold",
                {
                    "title": str(payload.get("subject") or "Meeting")[:120],
                    "start_utc": slot.start_utc.isoformat(),
                    "end_utc": slot.end_utc.isoformat(),
                    "attendees": [envelope.from_addr],
                    "agent_tag": AGENT_TAG,
                },
                envelope,
            )
            if not hold.allowed:
                continue
            event_id = (hold.result or {}).get("event_id")
            confirm = await self._act(
                run_id,
                "act",
                "calendar.confirm_event",
                {
                    "event_id": event_id,
                    "start_utc": slot.start_utc.isoformat(),
                    "end_utc": slot.end_utc.isoformat(),
                    "attendees": [envelope.from_addr],
                    "agent_tag": AGENT_TAG,
                },
                envelope,
            )
            if confirm.allowed:
                booked = {"event_id": event_id, "start_utc": slot.start_utc.isoformat()}
                break
            await self._act(run_id, "verify", "calendar.cancel_own_hold", {"event_id": event_id}, envelope)

        sent = await self._act(
            run_id,
            "act",
            "mail.send_reply",
            {
                "thread_id": envelope.thread_id,
                "to": envelope.from_addr,
                "cc": [],
                "bcc": [],
                "subject": str(payload.get("subject") or "Re: meeting"),
                "body": str(payload.get("body") or ""),
                "in_reply_to": envelope.message_id,
                "references": envelope.message_id,
            },
            envelope,
        )
        self.store.decide_approval(approval_id, "approved")
        self.bus.emit(
            run_id,
            "act",
            "approval_decided",
            {
                "approval_id": approval_id,
                "decision": "approved",
                "sent": sent.allowed,
                "booking": booked,
                "rule_ids": sent.decision.rule_ids,
            },
        )
        return {"ok": True, "sent": sent.allowed, "booking": booked, "approval_id": approval_id}

    # ==================================================================
    # helpers
    # ==================================================================
    async def _act(
        self,
        run_id: str,
        stage: str,
        tool: str,
        args: dict[str, Any],
        envelope: EmailEnvelope,
        fresh: bool = False,
    ) -> GatewayResult:
        action = new_action(run_id, stage, tool, args, envelope.message_id)
        return await self.gateway.execute(action, self.runtime, envelope, fresh=fresh)

    def _enter(self, run_id: str, number: int, name: str) -> None:
        self.bus.emit(
            run_id,
            f"{number}_{name}",
            "stage_start",
            {"stage": number, "name": name, "status": "running"},
        )

    def _leave(self, run_id: str, number: int, name: str, status: str = "ok") -> None:
        self.bus.emit(
            run_id,
            f"{number}_{name}",
            "stage_end",
            {"stage": number, "name": name, "status": status},
        )

    def _record_decision(self, run_id: str, summary: RunSummary, decision: Decision) -> None:
        summary.decision = decision.kind
        self.bus.emit(
            run_id,
            "decide",
            "decision",
            {
                "kind": decision.kind,
                "reasons": decision.reasons,
                "slots": [slot.model_dump(mode="json") for slot in decision.slots],
            },
        )

    def _finalize(self, summary: RunSummary) -> None:
        context = self._contexts.pop(summary.run_id, None)
        if context is not None:
            import json

            path = self.settings.traces_dir / f"{summary.run_id}.context.json"
            path.write_text(json.dumps(context, indent=2, default=str), encoding="utf-8")
        summary.human_interventions = 0
        summary.minutes_saved = MINUTES_SAVED.get(summary.outcome, 0.0)
        summary.tool_calls = self.store.run_tool_calls(summary.run_id)
        self.bus.emit(
            summary.run_id, "record", "run_summary", summary.as_dict()
        )
        self.store.finish_run(
            summary.run_id,
            outcome=summary.outcome,
            decision=summary.decision,
            ended_at=_now(),
            tool_calls=summary.tool_calls,
            cost_usd=summary.cost_usd,
            human_interventions=0,
            minutes_saved=summary.minutes_saved,
            summary=summary.as_dict(),
        )
        self.bus.append_run_summary(summary.as_dict())
        self.store.bump_total("runs", 1)
        self.store.bump_total("minutes_saved", summary.minutes_saved)

    def _finish_duplicate(self, message_id: str) -> RunSummary:
        summary = RunSummary(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            message_id=message_id,
            outcome="DUPLICATE",
            decision="FILE",
            reasons=["message already ingested (at-least-once delivery, exactly-once effects)"],
        )
        self.bus.emit(summary.run_id, "ingest", "duplicate_skipped", {"message_id": message_id})
        self._finalize(summary)
        return summary

    def _finish_malformed(self, detail: str) -> RunSummary:
        summary = RunSummary(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            message_id="unknown",
            outcome="QUARANTINED",
            decision="QUARANTINE",
            reasons=[f"malformed message: {detail}"],
        )
        self.bus.emit(summary.run_id, "ingest", "malformed", {"detail": detail})
        self._finalize(summary)
        return summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
