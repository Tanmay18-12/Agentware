"""Stage 5 -- Validate: bounds, futures, attendee caps and DST-aware local times."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Prefs
from ..schemas import MeetingIntent

OFFSET_RE = re.compile(r"^(?:UTC)?([+-])(\d{2}):?(\d{2})$")


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    duration_min: int = 30
    hard_fail: bool = False

    @property
    def actionable(self) -> bool:
        return self.ok


def resolve_tz(name: str | None, fallback: str) -> ZoneInfo:
    candidate = (name or fallback or "UTC").strip()
    match = OFFSET_RE.match(candidate)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3))
        return timezone(timedelta(hours=sign * hours, minutes=sign * minutes))  # type: ignore[return-value]
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        aliases = {
            "ist": "Asia/Kolkata",
            "pst": "America/Los_Angeles",
            "pdt": "America/Los_Angeles",
            "est": "America/New_York",
            "edt": "America/New_York",
            "gmt": "Etc/GMT",
            "bst": "Europe/London",
            "cet": "Europe/Paris",
            "cest": "Europe/Paris",
            "jst": "Asia/Tokyo",
            "sgt": "Asia/Singapore",
        }
        try:
            return ZoneInfo(aliases.get(candidate.lower(), fallback))
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


def local_is_nonexistent(local_dt: datetime) -> bool:
    """True for times skipped by a spring-forward transition."""
    if local_dt.tzinfo is None:
        return False
    round_trip = local_dt.astimezone(timezone.utc).astimezone(local_dt.tzinfo)
    return round_trip.replace(tzinfo=None) != local_dt.replace(tzinfo=None)


def local_is_ambiguous(local_dt: datetime) -> bool:
    """True for times repeated by a fall-back transition."""
    if local_dt.tzinfo is None:
        return False
    first = local_dt.replace(fold=0).astimezone(timezone.utc)
    second = local_dt.replace(fold=1).astimezone(timezone.utc)
    return first != second


def parse_hhmm(value: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours, minutes


def validate_intent(
    intent: MeetingIntent, prefs: Prefs, now: datetime | None = None
) -> ValidationResult:
    now = now or datetime.now(timezone.utc)
    issues: list[str] = []
    hard_fail = False

    duration = intent.duration_min or 30
    if duration < 10 or duration > 180:
        issues.append(f"duration {duration} min is outside the 10-180 min range")
        hard_fail = True
    elif prefs.allowed_durations and duration not in prefs.allowed_durations:
        issues.append(f"duration {duration} min is not an allowed length")

    if len(intent.attendees) > prefs.max_attendees:
        # Not a hard failure: the decision stage escalates this to a human instead.
        issues.append(f"{len(intent.attendees)} attendees exceeds the cap of {prefs.max_attendees}")

    if intent.kind in {"not_meeting", "other"}:
        return ValidationResult(ok=True, issues=issues, duration_min=duration)

    if intent.kind == "cancel":
        return ValidationResult(ok=True, issues=issues, duration_min=duration)

    for window in intent.windows:
        tz = resolve_tz(window.tz, prefs.owner_tz)
        for raw in (window.start_iso, window.end_iso):
            if not raw:
                continue
            parsed = _parse_iso(raw, tz)
            if parsed is None:
                issues.append(f"unparseable timestamp {raw!r}")
                hard_fail = True
                continue
            if local_is_nonexistent(parsed):
                issues.append(f"{raw} does not exist in {tz} (DST spring-forward gap)")
                hard_fail = True
            if local_is_ambiguous(parsed):
                issues.append(f"{raw} is ambiguous in {tz} (DST fall-back)")
            if parsed < now - timedelta(minutes=1):
                issues.append(f"{raw} is in the past")
                hard_fail = True
            if parsed > now + timedelta(days=prefs.horizon_days + 1):
                issues.append(f"{raw} is beyond the {prefs.horizon_days}-day horizon")
        if window.start_iso is None and window.end_iso is None and not window.raw_phrase.strip():
            issues.append("window has neither an ISO timestamp nor a phrase")

    if intent.kind in {"meeting_request", "reschedule"} and not intent.windows:
        issues.append("no time window was extracted")

    ambiguity = list(intent.ambiguity)
    if ambiguity:
        issues.extend(f"ambiguous: {item}" for item in ambiguity)

    return ValidationResult(
        ok=not issues or not hard_fail, issues=issues, duration_min=duration, hard_fail=hard_fail
    )


def _parse_iso(raw: str, tz: Any) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=tz)
        except (TypeError, ValueError):
            return None
    return parsed
