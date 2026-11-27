"""The single choke point. Every side effect passes through :meth:`PolicyGateway.execute`."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from .adapters.base import ProviderError, ProviderTimeout, RateLimitError, ServerError
from .config import Prefs, Settings
from .event_bus import EventBus, canonical, sha256_hex
from .kill_switch import KillSwitch
from .pipeline.composer import OutputGuard
from .schemas import (
    ActionRequest,
    CancelHoldArgs,
    ConfirmEventArgs,
    CreateHoldArgs,
    DraftReplyArgs,
    EmailEnvelope,
    FollowupArgs,
    FreeBusyArgs,
    GetOwnEventArgs,
    LabelArgs,
    MailListNewArgs,
    MailReadArgs,
    PolicyDecision,
    SendReplyArgs,
)

AGENT_TAG = "meetingpilot"


# --------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk_tier: str
    args_model: type[BaseModel]
    stages: tuple[str, ...]


READ_STAGES = ("act", "schedule", "verify")
REGISTRY: dict[str, ToolSpec] = {
    "mail.list_new": ToolSpec("mail.list_new", "READ", MailListNewArgs, ("act", "ingest")),
    "mail.read": ToolSpec("mail.read", "READ", MailReadArgs, ("act", "ingest")),
    "calendar.freebusy": ToolSpec("calendar.freebusy", "READ", FreeBusyArgs, ("schedule", "act", "verify")),
    "calendar.get_own_event": ToolSpec(
        "calendar.get_own_event", "READ", GetOwnEventArgs, ("act", "verify")
    ),
    "calendar.create_hold": ToolSpec(
        "calendar.create_hold", "REVERSIBLE", CreateHoldArgs, ("act",)
    ),
    "calendar.cancel_own_hold": ToolSpec(
        "calendar.cancel_own_hold", "REVERSIBLE", CancelHoldArgs, ("act", "verify")
    ),
    "mail.draft_reply": ToolSpec("mail.draft_reply", "REVERSIBLE", DraftReplyArgs, ("act",)),
    "mail.label": ToolSpec("mail.label", "REVERSIBLE", LabelArgs, ("act",)),
    "followup.file": ToolSpec("followup.file", "REVERSIBLE", FollowupArgs, ("act",)),
    "calendar.confirm_event": ToolSpec(
        "calendar.confirm_event", "IRREVERSIBLE", ConfirmEventArgs, ("act",)
    ),
    "mail.send_reply": ToolSpec("mail.send_reply", "IRREVERSIBLE", SendReplyArgs, ("act",)),
}

# Deliberately absent from the registry: the tool does not exist, so it cannot run.
FORBIDDEN_TOOLS = frozenset(
    {
        "mail.delete",
        "mail.forward",
        "mail.archive_others",
        "calendar.delete_event",
        "calendar.cancel_others",
        "calendar.update_others",
        "files.delete",
        "payments.charge",
        "shell.exec",
    }
)

STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "ingest": ("mail.list_new", "mail.read"),
    "prefilter": (),
    "screen": (),
    "reader_llm": (),
    "validate": (),
    "schedule": ("calendar.freebusy",),
    "act": tuple(REGISTRY),
    "verify": ("calendar.get_own_event", "calendar.cancel_own_hold", "calendar.freebusy"),
    "record": (),
}


def idempotency_key(message_id: str, tool: str, args: dict[str, Any]) -> str:
    return sha256_hex(canonical({"message_id": message_id, "tool": tool, "args": args}))[:64]


# --------------------------------------------------------------------------
# Runtime + result
# --------------------------------------------------------------------------
@dataclass
class ToolRuntime:
    mail: Any
    calendar: Any
    guard: OutputGuard
    prefs: Prefs
    settings: Settings
    known_titles: tuple[str, ...] = ()


@dataclass
class GatewayResult:
    decision: PolicyDecision
    executed: bool = False
    result: Any = None
    latency_ms: float = 0.0
    approval_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision.verdict == "ALLOW"


class PolicyGateway:
    def __init__(self, bus: EventBus, prefs: Prefs, settings: Settings, kill: KillSwitch):
        self.bus = bus
        self.store = bus.store
        self.prefs = prefs
        self.settings = settings
        self.kill = kill

    # -- public API ----------------------------------------------------
    async def execute(
        self,
        action: ActionRequest,
        runtime: ToolRuntime,
        envelope: EmailEnvelope | None = None,
        fresh: bool = False,
    ) -> GatewayResult:
        started = time.perf_counter()

        # P1 -- kill switch is checked before anything else.
        if self.kill.is_triggered():
            return self._finish(
                action, "BLOCK", ["P1"], "kill switch active: all actions blocked", started
            )

        # P2 -- registered tool + stage scope.
        spec = REGISTRY.get(action.tool)
        if spec is None:
            self._violation(action, f"unregistered tool {action.tool!r}")
            return self._finish(
                action,
                "BLOCK",
                ["P2", "P10"],
                f"tool {action.tool!r} is not registered and cannot be called",
                started,
            )
        if action.tool not in STAGE_TOOLS.get(action.stage, ()):
            self._violation(action, f"tool {action.tool!r} not permitted in stage {action.stage!r}")
            return self._finish(
                action,
                "BLOCK",
                ["P2"],
                f"tool {action.tool!r} is not permitted in stage {action.stage!r}",
                started,
            )

        # P3 -- strict argument validation.
        try:
            args = spec.args_model.model_validate(action.args)
        except ValidationError as error:
            return self._finish(
                action,
                "BLOCK",
                ["P3"],
                f"invalid arguments for {action.tool!r}: {error.errors()[:3]}",
                started,
            )

        # P7 -- idempotency (skipped for deliberate verification re-reads).
        cached = None if fresh else self.store.get_idempotent(action.idempotency_key)
        if cached is not None:
            unwrapped = cached.get("result") if isinstance(cached, dict) and "ok" in cached else cached
            return self._finish(
                action,
                "ALLOW",
                ["P7"],
                "duplicate action: returning recorded result",
                started,
                result=unwrapped,
            )

        # P6 -- budgets.
        budget_block = self._check_budgets(action)
        if budget_block is not None:
            return self._finish(action, "LIMIT", ["P6"], budget_block[0], started)

        # P4 / P5 -- irreversible action guards.
        guard_verdict: tuple[str, list[str], str] | None = None
        if action.tool == "mail.send_reply":
            guard_verdict = self._guard_send(args, envelope, action, runtime)
        elif action.tool == "calendar.confirm_event":
            guard_verdict = await self._guard_book(args, runtime, action, started)
        if guard_verdict is not None:
            verdict, rules, explanation = guard_verdict
            return self._finish(action, verdict, rules, explanation, started)  # type: ignore[arg-type]

        # P8 -- dry run: log the verdict, perform no effect.
        if self.settings.dry_run:
            return self._finish(
                action,
                "ALLOW",
                ["P8"],
                "dry-run: effect suppressed",
                started,
                result={"dry_run": True},
            )

        # Execute with bounded retries for transient provider failures.
        last_error: Exception | None = None
        for attempt in range(3):
            if self.kill.is_triggered():
                return self._finish(
                    action, "BLOCK", ["P1"], "kill switch activated during retries", started
                )
            try:
                result = await self._dispatch(action.tool, args, runtime)
                latency = (time.perf_counter() - started) * 1000
                if not fresh:
                    self.store.put_idempotent(
                        action.idempotency_key,
                        action.tool,
                        {"ok": True, "result": result if isinstance(result, (dict, list)) else str(result)},
                    )
                if action.tool == "mail.send_reply":
                    self.store.hit("send")
                    self.store.bump_total("sends", 1)
                if action.tool == "calendar.confirm_event":
                    self.store.hit("booking")
                    self.store.bump_total("bookings", 1)
                self.store.bump_run_tool_calls(action.run_id)
                self.bus.emit(
                    action.run_id,
                    action.stage,
                    "tool_call",
                    {
                        "tool": action.tool,
                        "args": self._redact_args(action.tool, action.args),
                        "ok": True,
                        "latency_ms": round(latency, 1),
                        "risk_tier": spec.risk_tier,
                        "idempotency_key": action.idempotency_key,
                    },
                )
                self._emit_decision(action, "ALLOW", self._rules_for(action), "allowed", latency)
                return GatewayResult(
                    decision=PolicyDecision(
                        action_id=action.id,
                        verdict="ALLOW",
                        rule_ids=self._rules_for(action),
                        explanation="allowed",
                    ),
                    executed=True,
                    result=result,
                    latency_ms=latency,
                )
            except (RateLimitError, ServerError, ProviderTimeout) as error:
                last_error = error
                self.bus.emit(
                    action.run_id,
                    action.stage,
                    "retry",
                    {"tool": action.tool, "attempt": attempt + 1, "error": type(error).__name__},
                )
                await asyncio.sleep(0.05 * (attempt + 1))
            except Exception as error:  # fail closed on anything unexpected
                last_error = error
                break

        latency = (time.perf_counter() - started) * 1000
        self._violation(action, f"provider failure: {type(last_error).__name__}: {last_error}")
        return self._finish(
            action,
            "BLOCK",
            ["PX"],
            f"provider failure after retries, failing closed ({type(last_error).__name__})",
            started,
            latency=latency,
        )

    # -- guards --------------------------------------------------------
    def _guard_send(
        self,
        args: SendReplyArgs,
        envelope: EmailEnvelope | None,
        action: ActionRequest,
        runtime: ToolRuntime,
    ) -> tuple[str, list[str], str] | None:
        if envelope is None:
            return "GATE", ["P4"], "no verified sender envelope in scope"
        if args.to.strip().lower() != envelope.from_addr.strip().lower():
            return "GATE", ["P4"], "recipient is not the verified original sender"
        if args.cc or args.bcc:
            return "GATE", ["P4"], "cc/bcc are not permitted on autonomous replies"
        if args.thread_id != envelope.thread_id:
            return "GATE", ["P4"], "reply is not bound to the original thread"
        domain = envelope.from_addr.rsplit("@", 1)[-1].lower()
        if domain in {d.lower() for d in self.prefs.blocked_domains}:
            return "BLOCK", ["P4"], f"sender domain {domain!r} is blocked"
        if not self.settings.mock_mode and not self.prefs.domain_allowed(domain):
            return "GATE", ["P4"], f"sender domain {domain!r} is not on the allowlist"

        prior_sends = sum(
            1
            for event in self.store.events(action.run_id)
            if event.get("type") == "tool_call"
            and event.get("payload", {}).get("tool") == "mail.send_reply"
        )
        if prior_sends >= 1:
            return "GATE", ["P4", "P6"], "at most one autonomous send per run"

        guard = runtime.guard.check(
            args.body,
            allowed_recipients=[envelope.from_addr],
            forbidden_titles=list(runtime.known_titles),
        )
        if not guard.ok:
            return "GATE", ["P4"], f"output guard rejected the draft: {'; '.join(guard.reasons)}"

        if self.store.hits_since("send", 3600) >= self.prefs.sends_per_hour:
            return "LIMIT", ["P6"], "sends-per-hour cap reached"
        return None

    async def _guard_book(
        self, args: ConfirmEventArgs, runtime: ToolRuntime, action: ActionRequest, started: float
    ) -> tuple[str, list[str], str] | None:
        if args.agent_tag != AGENT_TAG:
            return "BLOCK", ["P5"], "refusing to confirm an event the agent does not own"
        if len(args.attendees) > self.prefs.max_attendees:
            return "GATE", ["P5"], f"{len(args.attendees)} attendees exceeds the cap"
        now = datetime.now(timezone.utc)
        if args.start_utc <= now:
            return "BLOCK", ["P5"], "event start is not in the future"
        if args.start_utc > now + timedelta(days=self.prefs.horizon_days):
            return "BLOCK", ["P5"], "event start is beyond the booking horizon"

        local = args.start_utc.astimezone(ZoneInfo(self.prefs.owner_tz))
        if local.weekday() not in self.prefs.work_days:
            return "GATE", ["P5"], "event start is outside working days"
        if not (self.prefs.work_start_t <= local.time() < self.prefs.work_end_t):
            return "GATE", ["P5"], "event start is outside working hours"

        event = await runtime.calendar.get_own_event(args.event_id)
        if not event or event.get("agent_tag") != AGENT_TAG:
            return "BLOCK", ["P5"], "hold does not exist or is not agent-tagged"

        busy = await runtime.calendar.freebusy(
            args.start_utc.isoformat(), args.end_utc.isoformat()
        )
        for interval in busy:
            if interval.get("event_id") == args.event_id:
                continue
            if interval.get("start_utc") < args.end_utc.isoformat() and interval.get(
                "end_utc"
            ) > args.start_utc.isoformat():
                return "GATE", ["P5"], "re-check found an overlapping event"

        if self.store.hits_since("booking", 86400) >= self.prefs.bookings_per_day:
            return "LIMIT", ["P6"], "bookings-per-day cap reached"
        return None

    def _check_budgets(self, action: ActionRequest) -> tuple[str] | None:
        run = self.store.get_run(action.run_id) or {}
        if self.store.run_tool_calls(action.run_id) >= self.prefs.tool_calls_per_run:
            return (f"tool-call budget of {self.prefs.tool_calls_per_run} per run exhausted",)
        spent = float(run.get("cost_usd") or 0.0)
        if spent > self.prefs.cost_per_run_usd:
            return ("per-run LLM cost budget exhausted",)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.store.total(f"cost:{today}") > self.prefs.cost_per_day_usd:
            return ("daily LLM cost budget exhausted",)
        started_at = run.get("started_at")
        if started_at:
            try:
                began = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - began).total_seconds()
                if elapsed > self.prefs.wall_clock_per_run_s:
                    return ("wall-clock budget for this run exhausted",)
            except ValueError:
                pass
        return None

    # -- dispatch ------------------------------------------------------
    async def _dispatch(self, tool: str, args: BaseModel, runtime: ToolRuntime) -> Any:
        from .adapters.base import MalformedPayload

        if tool == "mail.list_new":
            return await runtime.mail.list_new(args.mailbox, args.limit)  # type: ignore[attr-defined]
        if tool == "mail.read":
            return await runtime.mail.read(args.message_id)  # type: ignore[attr-defined]
        if tool == "calendar.freebusy":
            return await runtime.calendar.freebusy(  # type: ignore[attr-defined]
                args.start_utc.isoformat(), args.end_utc.isoformat()  # type: ignore[attr-defined]
            )
        if tool == "calendar.get_own_event":
            return await runtime.calendar.get_own_event(args.event_id)  # type: ignore[attr-defined]
        if tool == "calendar.create_hold":
            return await runtime.calendar.create_hold(  # type: ignore[attr-defined]
                args.title,  # type: ignore[attr-defined]
                args.start_utc.isoformat(),  # type: ignore[attr-defined]
                args.end_utc.isoformat(),  # type: ignore[attr-defined]
                args.attendees,  # type: ignore[attr-defined]
                args.agent_tag,  # type: ignore[attr-defined]
            )
        if tool == "calendar.cancel_own_hold":
            return await runtime.calendar.cancel_own_hold(args.event_id)  # type: ignore[attr-defined]
        if tool == "calendar.confirm_event":
            return await runtime.calendar.confirm_event(  # type: ignore[attr-defined]
                args.event_id,  # type: ignore[attr-defined]
                args.start_utc.isoformat(),  # type: ignore[attr-defined]
                args.end_utc.isoformat(),  # type: ignore[attr-defined]
                args.attendees,  # type: ignore[attr-defined]
                args.agent_tag,  # type: ignore[attr-defined]
            )
        if tool == "mail.draft_reply":
            return await runtime.mail.draft_reply(  # type: ignore[attr-defined]
                args.thread_id, args.to, args.subject, args.body  # type: ignore[attr-defined]
            )
        if tool == "mail.send_reply":
            result = await runtime.mail.send_reply(  # type: ignore[attr-defined]
                args.thread_id,  # type: ignore[attr-defined]
                args.to,  # type: ignore[attr-defined]
                args.subject,  # type: ignore[attr-defined]
                args.body,  # type: ignore[attr-defined]
                args.cc,  # type: ignore[attr-defined]
                args.bcc,  # type: ignore[attr-defined]
                args.in_reply_to,  # type: ignore[attr-defined]
                args.references,  # type: ignore[attr-defined]
            )
            if isinstance(result, dict) and result.get("malformed"):
                raise MalformedPayload("mail provider returned a malformed send receipt")
            return result
        if tool == "mail.label":
            return await runtime.mail.label(args.message_id, args.label)  # type: ignore[attr-defined]
        if tool == "followup.file":
            return await runtime.mail.file_followup(  # type: ignore[attr-defined]
                args.thread_id, args.title, args.due_date, args.note  # type: ignore[attr-defined]
            )
        raise ProviderError(f"no handler for registered tool {tool!r}")

    # -- events --------------------------------------------------------
    def _rules_for(self, action: ActionRequest) -> list[str]:
        if action.tool == "mail.send_reply":
            return ["P4"]
        if action.tool == "calendar.confirm_event":
            return ["P5"]
        return []

    def _redact_args(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(args)
        if "body" in redacted and isinstance(redacted["body"], str):
            redacted["body"] = f"<{len(redacted['body'])} chars>"
        if "attendees" in redacted and isinstance(redacted["attendees"], list):
            redacted["attendees"] = [f"<{len(redacted['attendees'])} attendees>"]
        return redacted

    def _emit_decision(
        self, action: ActionRequest, verdict: str, rules: list[str], explanation: str, latency: float
    ) -> None:
        self.bus.emit(
            action.run_id,
            action.stage,
            "policy_decision",
            {
                "action_id": action.id,
                "tool": action.tool,
                "verdict": verdict,
                "rule_ids": rules,
                "explanation": explanation,
                "latency_ms": round(latency, 1),
            },
        )

    def _finish(
        self,
        action: ActionRequest,
        verdict: str,
        rules: list[str],
        explanation: str,
        started: float,
        result: Any = None,
        latency: float | None = None,
    ) -> GatewayResult:
        latency = latency if latency is not None else (time.perf_counter() - started) * 1000
        self._emit_decision(action, verdict, rules, explanation, latency)
        if verdict == "GATE":
            self.bus.emit(
                action.run_id,
                action.stage,
                "action_gated",
                {"action_id": action.id, "tool": action.tool, "args": self._redact_args(action.tool, action.args)},
            )
        elif verdict == "BLOCK":
            self.bus.emit(
                action.run_id,
                action.stage,
                "action_blocked",
                {"action_id": action.id, "tool": action.tool, "explanation": explanation},
            )
        return GatewayResult(
            decision=PolicyDecision(
                action_id=action.id,
                verdict=verdict,  # type: ignore[arg-type]
                rule_ids=rules,
                explanation=explanation,
            ),
            executed=False,
            result=result,
            latency_ms=latency,
        )

    def _violation(self, action: ActionRequest, detail: str) -> None:
        self.bus.emit(
            action.run_id,
            action.stage,
            "policy_violation",
            {"action_id": action.id, "tool": action.tool, "detail": detail},
        )
        self.store.bump_total("violations", 1)


def new_action(
    run_id: str,
    stage: str,
    tool: str,
    args: dict[str, Any],
    message_id: str,
    risk_tier: str | None = None,
) -> ActionRequest:
    spec = REGISTRY.get(tool)
    return ActionRequest(
        id=f"act_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        stage=stage,
        tool=tool,
        args=args,
        idempotency_key=idempotency_key(message_id, tool, args),
        risk_tier=(risk_tier or (spec.risk_tier if spec else "READ")),  # type: ignore[arg-type]
    )
