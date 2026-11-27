"""Shared helpers: build an isolated mock world in a temp directory."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from meetingpilot.adapters.mock_calendar import MockCalendar  # noqa: E402
from meetingpilot.adapters.mock_mail import MockMail  # noqa: E402
from meetingpilot.config import Prefs, Settings, load_prefs  # noqa: E402
from meetingpilot.event_bus import EventBus  # noqa: E402
from meetingpilot.kill_switch import KillSwitch  # noqa: E402
from meetingpilot.orchestrator import Orchestrator  # noqa: E402
from meetingpilot.pipeline.ingest import Ingest  # noqa: E402
from meetingpilot.schemas import EmailEnvelope  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")


@dataclass
class World:
    settings: Settings
    bus: EventBus
    mail: MockMail
    calendar: MockCalendar
    kill: KillSwitch
    orchestrator: Orchestrator

    def ingest(self, payload: dict[str, Any]) -> EmailEnvelope:
        materialized = materialize(payload)
        return Ingest(self.settings, self.bus.store).from_dict(materialized)


def base_prefs(**overrides: Any) -> Prefs:
    prefs = load_prefs(REPO_ROOT / "config" / "prefs.yaml")
    if not overrides:
        return prefs
    values = {**prefs.__dict__, **overrides}
    return Prefs(**values)


def make_world(
    tmp_path: Path,
    llm_mode: str = "stub",
    prefs: Prefs | None = None,
    busy: list[tuple[str, str]] | None = None,
) -> World:
    prefs = prefs or base_prefs()
    settings = Settings(
        root=tmp_path,
        mock_mode=True,
        llm_mode=llm_mode,
        owner_email=prefs.owner_email,
        sandbox_dir=tmp_path / "sandbox",
        traces_dir=tmp_path / "traces",
        database_path=tmp_path / "sandbox" / "meetingpilot.db",
        config_path=REPO_ROOT / "config" / "prefs.yaml",
        prefs=prefs,
    )
    settings.ensure_dirs()
    bus = EventBus(settings)
    mail = MockMail(settings)
    calendar = MockCalendar(settings)
    kill = KillSwitch(tmp_path / ".kill")
    orchestrator = Orchestrator(settings, bus, mail, calendar, kill)
    world = World(settings, bus, mail, calendar, kill, orchestrator)
    seed_busy(world, busy)
    return world


def next_business_day(now: datetime | None = None, tz: ZoneInfo = TZ) -> datetime:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(tz).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def seed_busy(world: World, busy: list[tuple[str, str]] | None = None) -> None:
    if busy is not None:
        for index, (start, end) in enumerate(busy):
            world.calendar.insert_event(f"Busy {index}", start, end)
        return
    day = next_business_day(tz=ZoneInfo(world.settings.prefs.owner_tz))
    start = datetime.combine(day.date(), time(11, 0), tzinfo=day.tzinfo)
    end = datetime.combine(day.date(), time(12, 0), tzinfo=day.tzinfo)
    world.calendar.insert_event("Sprint planning", start.isoformat(), end.isoformat())


def materialize(payload: dict[str, Any], now: datetime | None = None, tz: ZoneInfo = TZ) -> dict[str, Any]:
    """Replace {{TOKENS}} with dates relative to *now* so cases stay future-proof."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    d1 = next_business_day(now, tz)
    d2 = next_business_day(d1 + timedelta(days=1), tz)
    tokens = {
        "{{NOW}}": now.isoformat(),
        "{{D1}}": d1.strftime("%Y-%m-%d"),
        "{{D2}}": d2.strftime("%Y-%m-%d"),
        "{{D1_1000}}": datetime.combine(d1.date(), time(10, 0), tzinfo=tz).isoformat(),
        "{{D1_1100}}": datetime.combine(d1.date(), time(11, 0), tzinfo=tz).isoformat(),
        "{{D2_1530}}": datetime.combine(d2.date(), time(15, 30), tzinfo=tz).isoformat(),
        "{{PAST}}": (local.date() - timedelta(days=3)).strftime("%Y-%m-%d"),
    }
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            for token, replacement in tokens.items():
                value = value.replace(token, replacement)
            out[key] = value
        elif isinstance(value, dict):
            out[key] = materialize(value, now, tz)
        else:
            out[key] = value
    out.setdefault("received_at", now.isoformat())
    out.setdefault("thread_id", f"case-{abs(hash(str(payload)))}")
    out.setdefault("message_id", f"<case-{abs(hash(str(payload)))}@mail.example.com>")
    return out
