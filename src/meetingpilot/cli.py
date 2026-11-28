"""Command line interface: watch, inject, demo, kill, resume, explain, replay, verify-chain, serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.base import CalendarProvider, MailProvider
from .adapters.caldav_calendar import CalDavCalendar
from .adapters.imap_mail import ImapMail
from .adapters.mailpit_mail import MailpitMail
from .adapters.mock_calendar import MockCalendar
from .adapters.mock_mail import MockMail
from .config import Settings
from .demo_world import reset_runtime, seed
from .event_bus import EventBus
from .kill_switch import KillSwitch
from .orchestrator import Orchestrator, RunSummary
from .pipeline.ingest import DuplicateMessage, Ingest, MalformedMessage
from .pipeline.scheduler import compute_slots
from .pipeline.screen import ScreenResult
from .schemas import EmailEnvelope, MeetingIntent


@dataclass
class Services:
    settings: Settings
    bus: EventBus
    mail: MailProvider
    calendar: CalendarProvider
    kill: KillSwitch
    orchestrator: Orchestrator

    async def aclose(self) -> None:
        """Release pooled provider clients; mock providers have nothing to close."""
        for provider in (self.mail, self.calendar):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    bus = EventBus(settings)
    mail = build_mail(settings)
    calendar = build_calendar(settings)
    kill = KillSwitch(settings.kill_file)
    orchestrator = Orchestrator(settings, bus, mail, calendar, kill)
    return Services(settings, bus, mail, calendar, kill, orchestrator)


def build_mail(settings: Settings) -> MailProvider:
    provider = settings.mail_provider
    if provider == "mock":
        return MockMail(settings)
    if provider == "imap":
        return ImapMail(settings)
    if provider == "mailpit":
        return MailpitMail(settings)
    raise ValueError(f"unknown MAIL_PROVIDER {provider!r} (expected mock|imap|mailpit)")


def build_calendar(settings: Settings) -> CalendarProvider:
    provider = settings.calendar_provider
    if provider == "mock":
        return MockCalendar(settings)
    if provider == "caldav":
        return CalDavCalendar(settings)
    raise ValueError(f"unknown CALENDAR_PROVIDER {provider!r} (expected mock|caldav)")


def _require_mock_providers(settings: Settings) -> int | None:
    if settings.mail_provider != "mock" or settings.calendar_provider != "mock":
        print(
            "seed/demo build the offline mock world; unset MAIL_PROVIDER/CALENDAR_PROVIDER "
            "or use inject/watch/serve against the real providers",
            file=sys.stderr,
        )
        return 2
    return None


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_seed(args: argparse.Namespace, settings: Settings) -> int:
    if _require_mock_providers(settings) is not None:
        return 2
    print(json.dumps(seed(settings), indent=2))
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    if _require_mock_providers(settings) is not None:
        return 2
    seed(settings)
    reset_runtime(settings)
    if settings.kill_file.exists():
        settings.kill_file.unlink()
    services = build_services(settings)
    sample_dir = settings.sandbox_dir / "inbox_samples"
    order = [
        "01_valid_request",
        "02_conflict_request",
        "03_slot_selection",
        "04_injection_hidden",
        "05_external_multi",
        "06_cancel_all",
        "07_newsletter",
    ]
    runs: list[RunSummary] = []
    print(f"\nMeetingPilot demo — LLM_MODE={settings.llm_mode} MOCK_MODE={settings.mock_mode}\n")
    for name in order:
        path = sample_dir / f"{name}.json"
        if not path.is_file():
            continue
        summary = asyncio.run(services.orchestrator.handle_path(path))
        runs.append(summary)
        print(
            f"  {name:<22} → {summary.outcome:<18} decision={summary.decision:<11} "
            f"slots={len(summary.slots)} tools={summary.tool_calls}"
        )
        for reason in summary.reasons[:2]:
            print(f"      · {reason}")

    total_minutes = sum(item.minutes_saved for item in runs)
    print(
        f"\n{len(runs)} runs · {total_minutes:.1f} estimated minutes saved · "
        f"human_interventions={sum(item.human_interventions for item in runs)}"
    )

    ok = True
    for summary in runs:
        verified, detail = services.bus.verify_chain(summary.run_id)
        ok = ok and verified
        print(f"  chain {summary.run_id}: {'ok' if verified else 'FAILED'} ({detail})")
    index_ok, index_detail = services.bus.verify_index_chain()
    print(f"  chain index: {'ok' if index_ok else 'FAILED'} ({index_detail})")

    outbox = sorted(settings.outbox_dir.glob("*.eml"))
    print(f"\nReplies in sandbox/outbox: {len(outbox)}")
    for path in outbox:
        print(f"  · {path.name}")
    print(f"Approvals waiting: {len(services.bus.store.list_approvals())}")
    print(f"Traces: {settings.traces_dir}")
    return 0 if ok else 1


def cmd_inject(args: argparse.Namespace, settings: Settings) -> int:
    services = build_services(settings)
    if args.path:
        summary = asyncio.run(services.orchestrator.handle_path(Path(args.path)))
    else:
        if not args.text:
            print("inject needs a path or --text", file=sys.stderr)
            return 2
        payload = {
            "from": args.sender or "someone@example.com",
            "to": settings.prefs.owner_email,
            "subject": args.subject or "Meeting request",
            "body": args.text,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "message_id": f"<cli-{int(time.time() * 1000)}@meetingpilot.local>",
            "thread_id": f"cli-{int(time.time() * 1000)}",
        }
        from .pipeline.ingest import Ingest

        summary = asyncio.run(
            services.orchestrator.handle(Ingest(settings, services.bus.store).from_dict(payload))
        )
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def cmd_watch(args: argparse.Namespace, settings: Settings) -> int:
    services = build_services(settings)
    if settings.mail_provider != "mock":
        return _watch_provider(args, services)
    processed = settings.sandbox_dir / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    print(f"watching {settings.inbox_dir} (ctrl-c to stop)")
    try:
        while True:
            for path in sorted(settings.inbox_dir.glob("*")):
                if not path.is_file() or path.suffix.lower() not in {".json", ".eml", ".txt"}:
                    continue
                summary = asyncio.run(services.orchestrator.handle_path(path))
                print(
                    f"{path.name:<32} → {summary.outcome:<18} decision={summary.decision} "
                    f"run={summary.run_id}"
                )
                shutil.move(str(path), str(processed / path.name))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        asyncio.run(services.aclose())
    return 0


WATCH_BATCH = 50


def _watch_once(services: Services) -> int:
    """Poll the provider once and handle every message after the stored cursor.

    The cursor is a provider handle, advanced only past a message that was handled or is
    permanently unusable, so a poll that fails mid-run retries that message instead of losing it.
    """

    settings = services.settings
    store = services.bus.store
    ingest = Ingest(settings, store)
    mailbox = settings.imap_mailbox if settings.mail_provider == "imap" else "inbox"
    name = f"{settings.mail_provider}:{mailbox}"
    cursor = store.watch_cursor(name)

    def advance(handle: str) -> None:
        nonlocal cursor
        cursor = handle
        store.set_watch_cursor(name, handle)

    handled = 0
    for item in asyncio.run(services.mail.list_new(limit=WATCH_BATCH, cursor=cursor)):  # type: ignore[attr-defined]
        handle = str(item.get("id") or item.get("path") or item.get("name") or "")
        if not handle:
            continue
        try:
            record = asyncio.run(services.mail.read(handle))  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001 - a provider hiccup must not kill the loop
            print(f"retrying {handle} next poll: {error}", file=sys.stderr)
            break
        if not record:
            # Gone from the provider: there is nothing to retry.
            advance(handle)
            continue
        try:
            envelope = ingest.from_raw(str(record["raw"]).encode())
        except DuplicateMessage:
            advance(handle)
            continue
        except (MalformedMessage, ValueError) as error:
            print(f"skipping {handle}: {error}", file=sys.stderr)
            advance(handle)
            continue
        try:
            summary = asyncio.run(services.orchestrator.handle(envelope))
        except Exception as error:  # noqa: BLE001 - keep the message for the next poll
            store.forget_seen(envelope.message_id)
            print(f"retrying {handle} next poll: {error}", file=sys.stderr)
            break
        handled += 1
        print(
            f"{handle:<16} → {summary.outcome:<18} decision={summary.decision} "
            f"run={summary.run_id}"
        )
        advance(handle)
    return handled


def _watch_provider(args: argparse.Namespace, services: Services) -> int:
    settings = services.settings
    print(f"polling {settings.mail_provider} every {args.interval}s (ctrl-c to stop)")
    try:
        while True:
            try:
                _watch_once(services)
            except Exception as error:  # noqa: BLE001 - an outage is not a reason to exit
                print(f"poll failed: {error}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        asyncio.run(services.aclose())
    return 0


def cmd_kill(args: argparse.Namespace, settings: Settings) -> int:
    kill = KillSwitch(settings.kill_file)
    print(json.dumps(kill.trigger(by="cli", reason=args.reason or "meetingpilot kill"), indent=2))
    return 0


def cmd_resume(args: argparse.Namespace, settings: Settings) -> int:
    kill = KillSwitch(settings.kill_file)
    try:
        status = kill.reset(ack=args.ack)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(status, indent=2))
    return 0


def cmd_explain(args: argparse.Namespace, settings: Settings) -> int:
    bus = EventBus(settings)
    path = settings.traces_dir / f"{args.run_id}.jsonl"
    if not path.is_file():
        print(f"no trace for {args.run_id}", file=sys.stderr)
        return 1
    verified, detail = bus.verify_chain(args.run_id)
    print(f"run {args.run_id}  chain={'ok' if verified else 'BROKEN'} ({detail})\n")
    for event in bus.store.events(args.run_id):
        payload = event.get("payload", {})
        summary = _summarize(payload)
        print(f"{event['seq']:>3}  {event['ts'][11:23]}  {event['step_id']:<14} {event['type']:<18} {summary}")
    return 0 if verified else 1


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    context_path = settings.traces_dir / f"{args.run_id}.context.json"
    if not context_path.is_file():
        print(f"no recorded context for {args.run_id}", file=sys.stderr)
        return 1
    context = json.loads(context_path.read_text(encoding="utf-8"))
    services = build_services(settings)
    recorded = [
        event for event in services.bus.store.events(args.run_id) if event["type"] == "decision"
    ]
    if not recorded:
        print("no decision recorded for this run", file=sys.stderr)
        return 1
    envelope = EmailEnvelope.model_validate(context["envelope"])
    intent = MeetingIntent.model_validate(context["intent"])
    slots = compute_slots(intent, settings.prefs, context["busy"], envelope.received_at)
    screening = ScreenResult(
        injection_score=context["screen"]["injection_score"],
        flags=tuple(context["screen"]["flags"]),
        quarantine=False,
        spoof=bool(context["screen"].get("spoof", False)),
    )
    decision = services.orchestrator._decide(envelope, intent, slots, screening)
    expected = recorded[0]["payload"]["kind"]
    matches = decision.kind == expected
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "recorded_decision": expected,
                "replayed_decision": decision.kind,
                "identical": matches,
                "replayed_slots": [slot.start_utc.isoformat() for slot in decision.slots],
                "recorded_slots": [
                    slot.get("start_utc") for slot in recorded[0]["payload"].get("slots", [])
                ],
            },
            indent=2,
        )
    )
    return 0 if matches else 1


def cmd_verify_chain(args: argparse.Namespace, settings: Settings) -> int:
    bus = EventBus(settings)
    if args.run_id:
        runs = [args.run_id]
    else:
        runs = sorted(path.stem for path in settings.traces_dir.glob("run_*.jsonl"))
    failures = 0
    for run_id in runs:
        ok, detail = bus.verify_chain(run_id)
        failures += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {run_id}: {detail}")
    ok, detail = bus.verify_index_chain()
    failures += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} index: {detail}")
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    services = build_services(settings)
    print(
        json.dumps(
            {
                "kill_switch": services.kill.status(),
                "runs": len(services.bus.store.list_runs(100)),
                "pending_approvals": len(services.bus.store.list_approvals()),
                "minutes_saved": services.bus.store.total("minutes_saved"),
                "recent_runs": services.bus.store.list_runs(5),
            },
            indent=2,
        )
    )
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from .server import create_app, is_loopback_host

    token = (settings.dashboard_token or "").strip()
    if not token and not is_loopback_host(settings.host):
        print(
            f"refusing to bind the dashboard to {settings.host!r} without DASHBOARD_TOKEN: "
            "anyone who can reach it could inject mail, send replies and book events. "
            "Set DASHBOARD_TOKEN, or leave HOST at 127.0.0.1.",
            file=sys.stderr,
        )
        return 2

    services = build_services(settings)
    app = create_app(services)
    port = settings.port if settings.port and settings.port > 0 else 8000
    print(f"dashboard: http://{settings.host}:{port}", flush=True)
    if token:
        print(
            f"open http://{settings.host}:{port}/?token=<DASHBOARD_TOKEN> "
            "(requests without the token are rejected)",
            flush=True,
        )
    uvicorn.run(app, host=settings.host, port=port, log_level="warning")
    return 0


def _summarize(payload: dict[str, Any]) -> str:
    for key in ("kind", "tool", "stage", "outcome", "reason", "verdict", "message_id", "status"):
        if key in payload:
            return f"{key}={payload[key]}"
    text = json.dumps(payload, default=str)
    return text[:110]


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meetingpilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="seed the mock calendar, sample emails and cassettes")
    sub.add_parser("demo", help="run the offline end-to-end demonstration")

    watch = sub.add_parser("watch", help="poll sandbox/inbox forever")
    watch.add_argument("--interval", type=float, default=1.0)

    inject = sub.add_parser("inject", help="process one email file or inline text")
    inject.add_argument("path", nargs="?")
    inject.add_argument("--text")
    inject.add_argument("--from", dest="sender")
    inject.add_argument("--subject")

    kill = sub.add_parser("kill", help="trip the kill switch")
    kill.add_argument("--reason")

    resume = sub.add_parser("resume", help="clear HALTED state")
    resume.add_argument("--ack", action="store_true", help="acknowledge resuming after a kill")

    explain = sub.add_parser("explain", help="print a human-readable run timeline")
    explain.add_argument("run_id")

    replay = sub.add_parser("replay", help="re-run decisions from recorded inputs")
    replay.add_argument("run_id")

    verify = sub.add_parser("verify-chain", help="verify the hash-chained audit trail")
    verify.add_argument("run_id", nargs="?")

    sub.add_parser("status", help="show kill switch state and recent runs")
    sub.add_parser("serve", help="start the dashboard")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    handlers = {
        "seed": cmd_seed,
        "demo": cmd_demo,
        "watch": cmd_watch,
        "inject": cmd_inject,
        "kill": cmd_kill,
        "resume": cmd_resume,
        "explain": cmd_explain,
        "replay": cmd_replay,
        "verify-chain": cmd_verify_chain,
        "status": cmd_status,
        "serve": cmd_serve,
    }
    try:
        return handlers[args.command](args, settings)
    except ValueError as error:
        # Misconfigured providers (e.g. MAIL_PROVIDER=imapx) deserve a message, not a traceback.
        print(f"meetingpilot: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
