"""Configuration: environment switches plus ``config/prefs.yaml``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def project_root() -> Path:
    """Repository root: ``MEETINGPILOT_ROOT`` or the checkout that holds this package."""
    override = os.environ.get("MEETINGPILOT_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def parse_hhmm(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes or 0))


@dataclass(frozen=True)
class ProtectedBlock:
    days: tuple[int, ...]
    start: str
    end: str
    label: str = "protected"


@dataclass(frozen=True)
class Prefs:
    owner_name: str = "Owner"
    owner_email: str = "owner@example.com"
    owner_tz: str = "UTC"
    work_start: str = "09:00"
    work_end: str = "18:00"
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    pref_start: str = "10:00"
    pref_end: str = "16:00"
    buffer_before: int = 10
    buffer_after: int = 10
    min_notice_hours: int = 4
    max_meetings_per_day: int = 4
    max_attendees: int = 4
    horizon_days: int = 30
    grid_min: int = 15
    max_proposals: int = 3
    no_meeting_days: tuple[int, ...] = ()
    allowed_durations: tuple[int, ...] = (15, 30, 45, 60, 90)
    protected: tuple[ProtectedBlock, ...] = ()
    allowed_domains: tuple[str, ...] = ("example.com",)
    blocked_domains: tuple[str, ...] = ()
    vip_senders: tuple[str, ...] = ()
    auto_approve_domains: tuple[str, ...] = ("example.com",)
    tool_calls_per_run: int = 25
    cost_per_run_usd: float = 0.25
    cost_per_day_usd: float = 5.0
    wall_clock_per_run_s: int = 120
    sends_per_hour: int = 10
    bookings_per_day: int = 10
    sender_emails_per_hour: int = 3
    meeting_url_prefix: str = "https://meet.example.com/"

    @property
    def work_start_t(self) -> time:
        return parse_hhmm(self.work_start)

    @property
    def work_end_t(self) -> time:
        return parse_hhmm(self.work_end)

    @property
    def pref_start_t(self) -> time:
        return parse_hhmm(self.pref_start)

    @property
    def pref_end_t(self) -> time:
        return parse_hhmm(self.pref_end)

    def domain_allowed(self, domain: str) -> bool:
        domain = domain.lower()
        if domain in {d.lower() for d in self.blocked_domains}:
            return False
        return domain in {d.lower() for d in self.allowed_domains}

    def domain_auto_approve(self, domain: str) -> bool:
        return domain.lower() in {d.lower() for d in self.auto_approve_domains}


def load_prefs(path: Path) -> Prefs:
    if not path.is_file():
        return Prefs()
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    owner = raw.get("owner", {}) or {}
    working = raw.get("working_hours", {}) or {}
    preferred = raw.get("preferred_hours", {}) or {}
    buffers = raw.get("buffers", {}) or {}
    senders = raw.get("senders", {}) or {}
    limits = raw.get("limits", {}) or {}
    protected = tuple(
        ProtectedBlock(
            days=tuple(int(d) for d in block.get("days", [])),
            start=str(block.get("start", "00:00")),
            end=str(block.get("end", "00:00")),
            label=str(block.get("label", "protected")),
        )
        for block in (raw.get("protected_blocks") or [])
    )
    return Prefs(
        owner_name=str(owner.get("name", "Owner")),
        owner_email=str(owner.get("email", "owner@example.com")),
        owner_tz=str(owner.get("timezone", "UTC")),
        work_start=str(working.get("start", "09:00")),
        work_end=str(working.get("end", "18:00")),
        work_days=tuple(int(d) for d in working.get("days", [0, 1, 2, 3, 4])),
        pref_start=str(preferred.get("start", "10:00")),
        pref_end=str(preferred.get("end", "16:00")),
        buffer_before=int(buffers.get("before_min", 10)),
        buffer_after=int(buffers.get("after_min", 10)),
        min_notice_hours=int(raw.get("min_notice_hours", 4)),
        max_meetings_per_day=int(raw.get("max_meetings_per_day", 4)),
        max_attendees=int(raw.get("max_attendees", 4)),
        horizon_days=int(raw.get("booking_horizon_days", 30)),
        grid_min=int(raw.get("grid_min", 15)),
        max_proposals=int(raw.get("max_proposals", 3)),
        no_meeting_days=tuple(int(d) for d in raw.get("no_meeting_days", [])),
        allowed_durations=tuple(int(d) for d in raw.get("allowed_durations_min", [15, 30, 45, 60, 90])),
        protected=protected,
        allowed_domains=tuple(str(d) for d in senders.get("allowed_domains", ["example.com"])),
        blocked_domains=tuple(str(d) for d in senders.get("blocked_domains", [])),
        vip_senders=tuple(str(d) for d in senders.get("vip_senders", [])),
        auto_approve_domains=tuple(str(d) for d in senders.get("auto_approve_domains", ["example.com"])),
        tool_calls_per_run=int(limits.get("tool_calls_per_run", 25)),
        cost_per_run_usd=float(limits.get("cost_per_run_usd", 0.25)),
        cost_per_day_usd=float(limits.get("cost_per_day_usd", 5.0)),
        wall_clock_per_run_s=int(limits.get("wall_clock_per_run_s", 120)),
        sends_per_hour=int(limits.get("sends_per_hour", 10)),
        bookings_per_day=int(limits.get("bookings_per_day", 10)),
        sender_emails_per_hour=int(limits.get("sender_emails_per_hour", 3)),
        meeting_url_prefix=str(raw.get("meeting_url_prefix", "https://meet.example.com/")),
    )


@dataclass
class Settings:
    root: Path
    mock_mode: bool = True
    llm_mode: str = "cassette"
    dry_run: bool = False
    owner_email: str = "owner@example.com"
    owner_name: str = "Tanmay"
    sandbox_dir: Path = field(default_factory=lambda: Path("sandbox"))
    traces_dir: Path = field(default_factory=lambda: Path("traces"))
    database_path: Path = field(default_factory=lambda: Path("sandbox/meetingpilot.db"))
    config_path: Path = field(default_factory=lambda: Path("config/prefs.yaml"))
    host: str = "127.0.0.1"
    port: int = 8000
    prefs: Prefs = field(default_factory=Prefs)

    @classmethod
    def from_env(cls, root: Path | None = None, **overrides: Any) -> "Settings":
        root = (root or project_root()).resolve()
        dotenv = _parse_dotenv(root / ".env")
        merged = {**dotenv, **{k: v for k, v in os.environ.items()}}

        def flag(name: str, default: str) -> bool:
            return str(merged.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}

        config_path = root / Path(merged.get("CONFIG_PATH", "config/prefs.yaml"))
        sandbox = root / Path(merged.get("SANDBOX_DIR", "sandbox"))
        settings = cls(
            root=root,
            mock_mode=flag("MOCK_MODE", "1"),
            llm_mode=str(merged.get("LLM_MODE", "cassette")).strip().lower(),
            dry_run=flag("DRY_RUN", "0"),
            owner_email=str(merged.get("OWNER_EMAIL", "owner@example.com")),
            owner_name=str(merged.get("OWNER_NAME", "Tanmay")),
            sandbox_dir=sandbox,
            traces_dir=root / Path(merged.get("TRACES_DIR", "traces")),
            database_path=root / Path(merged.get("DATABASE_PATH", "sandbox/meetingpilot.db")),
            config_path=config_path,
            host=str(merged.get("HOST", "127.0.0.1")),
            port=int(merged.get("PORT", "8000")),
            prefs=load_prefs(config_path),
        )
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings

    # ---- paths -------------------------------------------------------
    @property
    def inbox_dir(self) -> Path:
        return self.sandbox_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self.sandbox_dir / "outbox"

    @property
    def drafts_dir(self) -> Path:
        return self.sandbox_dir / "drafts"

    @property
    def calendar_db(self) -> Path:
        return self.sandbox_dir / "calendar.db"

    @property
    def kill_file(self) -> Path:
        return self.root / ".kill"

    @property
    def cassettes_dir(self) -> Path:
        return self.root / "tests" / "cassettes"

    def ensure_dirs(self) -> None:
        for path in (
            self.sandbox_dir,
            self.inbox_dir,
            self.outbox_dir,
            self.drafts_dir,
            self.traces_dir,
            self.database_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
