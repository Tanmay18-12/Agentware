"""Stage 6 -- Scheduler: pure-code free/busy math, 15-minute grid and slot scoring.

No model is involved. All datetimes are handled as timezone-aware UTC values and every local
conversion goes through :mod:`zoneinfo`, so DST gaps and overlaps are explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..config import Prefs
from ..schemas import MeetingIntent, SlotCandidate
from .validator import local_is_ambiguous, local_is_nonexistent, parse_hhmm, resolve_tz

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", to_utc(self.start))
        object.__setattr__(self, "end", to_utc(self.end))

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def as_interval(item: Any) -> Interval:
    if isinstance(item, Interval):
        return item
    if isinstance(item, dict):
        return Interval(_parse_dt(item["start_utc"]), _parse_dt(item["end_utc"]))
    raise TypeError(f"cannot interpret {item!r} as an interval")


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return to_utc(value)
    return to_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def overlaps(first: Interval, second: Interval) -> bool:
    return first.start < second.end and first.end > second.start


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted((item for item in intervals if item.end > item.start), key=lambda i: i.start)
    merged: list[Interval] = []
    for item in ordered:
        if not merged or item.start > merged[-1].end:
            merged.append(item)
        else:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, item.end))
    return merged


def subtract(base: Iterable[Interval], cutters: Iterable[Interval]) -> list[Interval]:
    result = merge(base)
    for cutter in merge(cutters):
        next_result: list[Interval] = []
        for interval in result:
            if not overlaps(interval, cutter):
                next_result.append(interval)
                continue
            if cutter.start > interval.start:
                next_result.append(Interval(interval.start, min(cutter.start, interval.end)))
            if cutter.end < interval.end:
                next_result.append(Interval(max(cutter.end, interval.start), interval.end))
        result = next_result
    return [item for item in result if item.end > item.start]


def intersect(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a in merge(left):
        for b in merge(right):
            start = max(a.start, b.start)
            end = min(a.end, b.end)
            if end > start:
                out.append(Interval(start, end))
    return merge(out)


# --------------------------------------------------------------------------
# Local-time helpers
# --------------------------------------------------------------------------
def _resolve_local(day: date, clock: time, tz: ZoneInfo) -> datetime:
    """Build an aware local datetime, shifting out of a DST spring-forward gap."""
    local = datetime.combine(day, clock, tzinfo=tz)
    if local_is_nonexistent(local):
        local = local.astimezone(timezone.utc).astimezone(tz)
    return local


def work_block(day: date, prefs: Prefs, tz: ZoneInfo) -> Interval | None:
    if day.weekday() not in prefs.work_days or day.weekday() in prefs.no_meeting_days:
        return None
    start = _resolve_local(day, prefs.work_start_t, tz)
    end = _resolve_local(day, prefs.work_end_t, tz)
    if end <= start:
        return None
    return Interval(start, end)


def protected_intervals(day: date, prefs: Prefs, tz: ZoneInfo) -> list[Interval]:
    blocks: list[Interval] = []
    for block in prefs.protected:
        if block.days and day.weekday() not in block.days:
            continue
        start_clocks = parse_hhmm(block.start)
        end_clocks = parse_hhmm(block.end)
        if not start_clocks or not end_clocks:
            continue
        start = _resolve_local(day, time(*start_clocks), tz)
        end = _resolve_local(day, time(*end_clocks), tz)
        blocks.append(Interval(start, end))
    return blocks


# --------------------------------------------------------------------------
# Phrase resolution (raw text -> window), still code not model
# --------------------------------------------------------------------------
def resolve_windows(
    intent: MeetingIntent, prefs: Prefs, now_utc: datetime | None = None
) -> tuple[list[Interval], list[str]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    duration = timedelta(minutes=intent.duration_min or 30)
    regions: list[Interval] = []
    issues: list[str] = []

    for window in intent.windows:
        tz = resolve_tz(window.tz, prefs.owner_tz)
        start = _parse_iso(window.start_iso, tz)
        end = _parse_iso(window.end_iso, tz)
        if start and not end:
            span = duration if window.flexibility == "exact" else max(duration, timedelta(minutes=60))
            end = start + span
        elif end and not start:
            start = end - duration
        elif start is None and window.raw_phrase.strip():
            local_now = now_utc.astimezone(tz)
            start, end = parse_phrase(
                window.raw_phrase, tz, local_now, duration, window.flexibility
            )
        if start is None or end is None:
            issues.append(f"could not resolve window {window.raw_phrase or window.start_iso!r}")
            continue
        if local_is_ambiguous(start):
            # A phrase like "Nov 1 at 01:30" means two possible instants; the caller must clarify.
            issues.append(
                f"{window.raw_phrase or window.start_iso} is ambiguous in {tz} "
                "(DST fall-back): please use an explicit UTC offset"
            )
        regions.append(Interval(start, end))
    return regions, issues


def _parse_iso(raw: str | None, tz: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_phrase(
    phrase: str,
    tz: Any,
    now_local: datetime,
    duration: timedelta,
    flexibility: str = "window",
) -> tuple[datetime | None, datetime | None]:
    """Resolve phrases such as "next Friday at 14:00" or "2026-12-01 10:00"."""
    text = phrase.lower().strip()
    target: date | None = None

    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        try:
            target = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None, None
        text = text.replace(iso.group(0), " ")
    else:
        named = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", text)
        if named:
            month = MONTHS[named.group(1)]
            day = int(named.group(2))
            year = now_local.year + (1 if month < now_local.month else 0)
            try:
                target = date(year, month, day)
            except ValueError:
                return None, None
            text = text.replace(named.group(0), " ")

    if target is None:
        days_match = re.search(r"\bin\s+(\d{1,2})\s+days?\b", text)
        if days_match:
            target = now_local.date() + timedelta(days=int(days_match.group(1)))
            text = text.replace(days_match.group(0), " ")
        elif "day after tomorrow" in text:
            target = now_local.date() + timedelta(days=2)
        elif "tomorrow" in text:
            target = now_local.date() + timedelta(days=1)
        elif "today" in text:
            target = now_local.date()
        else:
            for word, index in sorted(WEEKDAYS.items(), key=lambda item: -len(item[0])):
                if re.search(rf"\b{word}\b", text):
                    ahead = (index - now_local.weekday()) % 7
                    if re.search(rf"\bnext\s+{word}\b", text):
                        ahead = ahead or 7
                    target = now_local.date() + timedelta(days=ahead)
                    break

    clock = _parse_clock(text)
    if clock is None and re.search(r"\b\d{1,2}:\d{2}\b", text):
        # a clock-looking token that is not a valid wall time (e.g. 25:00)
        return None, None
    if target is None:
        return None, None
    if clock is None:
        start_local = datetime.combine(target, time(0, 0), tzinfo=tz)
        end_local = datetime.combine(target, time(23, 59), tzinfo=tz)
        if flexibility == "exact":
            end_local = start_local + duration
        return start_local, end_local

    start_local = datetime.combine(target, clock, tzinfo=tz)
    if local_is_nonexistent(start_local):
        return None, None
    if flexibility == "exact":
        return start_local, start_local + duration
    return start_local - timedelta(minutes=30), start_local + max(duration, timedelta(minutes=90))


def _parse_clock(text: str) -> time | None:
    if "noon" in text:
        return time(12, 0)
    if "midnight" in text:
        return time(0, 0)
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3) == "pm":
            hour += 12
        if minute > 59 or hour > 23:
            return None
        return time(hour, minute)
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return time(hour, minute)
    match = re.search(r"\b(\d{1,2})\s*o'?clock\b", text)
    if match:
        hour = int(match.group(1))
        if hour > 23:
            return None
        return time(hour, 0)
    return None


# --------------------------------------------------------------------------
# Slot search
# --------------------------------------------------------------------------
def compute_slots(
    intent: MeetingIntent,
    prefs: Prefs,
    busy: Iterable[Any],
    now_utc: datetime | None = None,
    limit: int | None = None,
    issues: list[str] | None = None,
) -> list[SlotCandidate]:
    """Return scored slots; pass ``issues`` to receive window-resolution warnings."""
    now = to_utc(now_utc or datetime.now(timezone.utc))
    tz = ZoneInfo(prefs.owner_tz)
    duration = timedelta(minutes=intent.duration_min or 30)
    grid = max(5, prefs.grid_min)

    regions, resolution_issues = resolve_windows(intent, prefs, now)
    if issues is not None:
        issues.extend(resolution_issues)
    default = _default_regions(now, prefs, tz, intent)

    busy_intervals = [as_interval(item) for item in busy]
    buffered = [
        Interval(item.start - timedelta(minutes=prefs.buffer_before),
                 item.end + timedelta(minutes=prefs.buffer_after))
        for item in busy_intervals
    ]

    earliest = now + timedelta(hours=prefs.min_notice_hours)
    horizon_end = now + timedelta(days=prefs.horizon_days)

    def _search(search_regions: list[Interval]) -> list[SlotCandidate]:
        found: list[SlotCandidate] = []
        day = now.astimezone(tz).date()
        last_day = horizon_end.astimezone(tz).date()
        while day <= last_day:
            work = work_block(day, prefs, tz)
            if work is not None:
                day_meetings = [item for item in busy_intervals if overlaps(item, work)]
                free = subtract([work], buffered + protected_intervals(day, prefs, tz))
                for region in search_regions:
                    for chunk in intersect(free, [region]):
                        for start in _grid_starts(chunk, duration, grid):
                            if start < earliest or start + duration > horizon_end:
                                continue
                            score, reasons = _score(
                                start,
                                duration,
                                intent,
                                prefs,
                                tz,
                                region,
                                day_meetings,
                                busy_intervals,
                                now,
                            )
                            found.append(
                                SlotCandidate(
                                    start_utc=start,
                                    end_utc=start + duration,
                                    score=round(score, 2),
                                    reasons=reasons,
                                )
                            )
            day += timedelta(days=1)
        return found

    candidates = _search(regions or default)
    if candidates or not regions:
        return _rank(candidates)[: (limit or prefs.max_proposals)]

    # The requested window is fully booked: same-day later slots outrank following days.
    requested_day = regions[0].start.astimezone(tz).date()
    same_day: list[SlotCandidate] = []
    block = work_block(requested_day, prefs, tz)
    if block is not None:
        later = Interval(max(block.start, regions[0].start), block.end)
        if later.end > later.start:
            same_day = _rank(_search([later]))
    following_days = [
        candidate
        for candidate in _search(default)
        if candidate.start_utc.astimezone(tz).date() != requested_day
    ]
    return (same_day + _rank(following_days))[: (limit or prefs.max_proposals)]


def _rank(candidates: Iterable[SlotCandidate]) -> list[SlotCandidate]:
    """Deduplicate by start (keeping the best score), then order by score and time."""
    unique: dict[datetime, SlotCandidate] = {}
    for candidate in candidates:
        if candidate.start_utc not in unique or candidate.score > unique[candidate.start_utc].score:
            unique[candidate.start_utc] = candidate
    return sorted(unique.values(), key=lambda item: (-item.score, item.start_utc))


def _default_regions(now: datetime, prefs: Prefs, tz: ZoneInfo, intent: MeetingIntent) -> list[Interval]:
    regions: list[Interval] = []
    day = now.astimezone(tz).date()
    added = 0
    scan_limit = prefs.horizon_days
    while added < 5 and scan_limit > 0:
        block = work_block(day, prefs, tz)
        if block is not None:
            regions.append(block)
            added += 1
        day += timedelta(days=1)
        scan_limit -= 1
    return regions


def _grid_starts(chunk: Interval, duration: timedelta, grid: int) -> Iterable[datetime]:
    step = timedelta(minutes=grid)
    # Align to wall-clock multiples of the grid (:00/:15/:30/:45 for grid=15), not to the
    # chunk start: a buffer that ends at 11:10 must still offer the 11:15 slot.
    start = chunk.start.replace(second=0, microsecond=0)
    remainder = start.minute % grid
    if remainder:
        start += timedelta(minutes=grid - remainder)
    while start + duration <= chunk.end:
        yield start
        start += step


def _score(
    start: datetime,
    duration: timedelta,
    intent: MeetingIntent,
    prefs: Prefs,
    tz: ZoneInfo,
    region: Interval,
    day_meetings: list[Interval],
    busy: list[Interval],
    now: datetime,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    end = start + duration

    if Interval(start, end).start >= region.start and end <= region.end:
        score += 3.0
        reasons.append("inside requested window")

    local = start.astimezone(tz)
    pref_start = _resolve_local(local.date(), prefs.pref_start_t, tz)
    pref_end = _resolve_local(local.date(), prefs.pref_end_t, tz)
    if pref_start <= local < pref_end:
        score += 1.5
        reasons.append("preferred hours")

    score -= 1.0 * len(day_meetings)
    if day_meetings:
        reasons.append(f"{len(day_meetings)} meeting(s) that day")

    for item in busy:
        if item.end == start or item.start == end:
            score -= 2.0
            reasons.append("back-to-back")
            break

    days_away = (local.date() - now.astimezone(tz).date()).days
    score -= 0.05 * max(0, days_away)
    return score, reasons
