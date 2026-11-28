"""CalDAV calendar adapter (Radicale, Baikal, Nextcloud, ...) with a minimal iCalendar codec.

Only the surface the policy gateway exposes is implemented: free/busy reads, agent-tagged
tentative holds, confirming them, and cancelling the agent's *own* holds. Foreign events are
never modified or deleted.

Stamps are decoded with their parameters: ``DTSTART;TZID=Asia/Kolkata:...`` is resolved through
:mod:`zoneinfo` (with a VTIMEZONE offset fallback for non-IANA ids), and ``DURATION`` is honoured
when ``DTEND`` is absent. ``freebusy`` expands ``RRULE``/``EXDATE`` (DAILY/WEEKLY/MONTHLY/YEARLY with
``INTERVAL``, ``COUNT``, ``UNTIL`` and ``BYDAY``) inside the requested window; ``list_events`` keeps
one row per series for the dashboard. This is still not a general-purpose CalDAV client.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from calendar import monthrange
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..config import Settings
from .base import ProviderError, ProviderTimeout, RateLimitError, ServerError

DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"
AGENT_TAG = "meetingpilot"

_MKCALENDAR_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:set><D:prop><D:displayname>MeetingPilot</D:displayname></D:prop></D:set>
</C:mkcalendar>"""

_REPORT_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-data/></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
    <C:time-range start="{start}" end="{end}"/>
  </C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>"""


# --------------------------------------------------------------------------
# iCalendar codec
# --------------------------------------------------------------------------
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


_DURATION_RE = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


def _parse_params(name: str) -> tuple[str, dict[str, str]]:
    """Split ``DTSTART;TZID=...;VALUE=DATE`` into its key and parameters."""
    parts = name.split(";")
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, value = part.partition("=")
        params[key.strip().upper()] = value.strip().strip('"')
    return parts[0].strip().upper(), params


def _naive_dt(value: str) -> datetime:
    value = value.strip()
    if "T" in value:
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    return datetime.strptime(value, "%Y%m%d")


def _parse_dt(value: str) -> datetime:
    """Parse a bare iCalendar stamp (no ``TZID``/parameters) as UTC."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1]
    return _naive_dt(value).replace(tzinfo=timezone.utc)


def _parse_offset(value: str) -> timedelta | None:
    """Parse a VTIMEZONE ``TZOFFSETTO`` such as ``+0530``."""
    match = re.match(r"^([+-])(\d{2})(\d{2})(\d{2})?$", value.strip())
    if not match:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return sign * timedelta(
        hours=int(match.group(2)), minutes=int(match.group(3)), seconds=int(match.group(4) or 0)
    )


def _resolve_tz(tzid: str, offsets: dict[str, timedelta]) -> tzinfo:
    """Resolve a TZID: IANA name, path-prefixed IANA name, then VTIMEZONE offset."""
    name = tzid.strip().strip('"')
    candidates = [name]
    if name.startswith("/"):
        parts = [part for part in name.split("/") if part]
        candidates.extend("/".join(parts[index:]) for index in range(1, len(parts)))
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    if name in offsets:
        return timezone(offsets[name])
    return timezone.utc


def _parse_duration(value: str) -> timedelta | None:
    """Parse a positive iCalendar DURATION (``PT45M``, ``P1D``, ``P1W``)."""
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    return timedelta(**{key: int(raw or 0) for key, raw in match.groupdict().items()})


def _parse_stamp(
    value: str, params: dict[str, str], offsets: dict[str, timedelta]
) -> tuple[datetime, bool] | None:
    """Decode one DTSTART/DTEND value, returning ``(aware datetime, is_all_day)``."""
    raw = value.strip()
    all_day = params.get("VALUE", "").upper() == "DATE" or ("T" not in raw and len(raw) == 8)
    try:
        if raw.endswith("Z"):
            return _naive_dt(raw[:-1]).replace(tzinfo=timezone.utc), False
        parsed = _naive_dt(raw)
    except ValueError:
        return None
    if all_day:
        return parsed.replace(tzinfo=timezone.utc), True
    tzid = params.get("TZID")
    if tzid:
        return parsed.replace(tzinfo=_resolve_tz(tzid, offsets)), False
    # Floating local time: no TZID, no Z. Interpreted as UTC, as before.
    return parsed.replace(tzinfo=timezone.utc), False


def _as_dt(value: str | datetime) -> datetime:
    """Accept either an ICS stamp or an ISO 8601 timestamp from the gateway."""
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _parse_dt(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt_dt(value: str | datetime) -> str:
    return _as_dt(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Parse every VEVENT in an iCalendar payload into plain dicts.

    Honours ``TZID`` (IANA, path-prefixed or VTIMEZONE offset), ``VALUE=DATE`` and ``DURATION``;
    falls back to UTC only for genuinely floating times.
    """
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    offsets: dict[str, timedelta] = {}
    tz_name: str | None = None
    in_timezone = False
    for line in _unfold(text):
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key, params = _parse_params(name)
        upper = value.strip().upper()

        if key == "BEGIN" and upper == "VTIMEZONE":
            in_timezone, tz_name = True, None
            continue
        if in_timezone:
            if key == "END" and upper == "VTIMEZONE":
                in_timezone, tz_name = False, None
            elif key == "TZID":
                tz_name = value.strip()
            elif key == "TZOFFSETTO" and tz_name:
                offset = _parse_offset(value)
                if offset is not None:
                    # Keep the first observance: enough to place the event when ZoneInfo fails.
                    offsets.setdefault(tz_name, offset)
            continue

        if key == "BEGIN" and upper == "VEVENT":
            current = {}
            continue
        if key == "END" and upper == "VEVENT":
            if current and current.get("event_id") and current.get("start"):
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        if key == "UID":
            current["event_id"] = value.strip()
        elif key == "SUMMARY":
            current["title"] = _unescape(value.strip())
        elif key in {"DTSTART", "DTEND"}:
            stamp = _parse_stamp(value, params, offsets)
            if stamp is None:
                continue
            parsed, all_day = stamp
            current["start" if key == "DTSTART" else "end"] = parsed
            if key == "DTSTART":
                current["all_day"] = all_day
        elif key == "DURATION":
            duration = _parse_duration(value)
            if duration is not None:
                current["duration"] = duration
        elif key == "RRULE":
            current["rrule"] = value.strip()
        elif key == "EXDATE":
            for token in value.split(","):
                stamp = _parse_stamp(token, params, offsets)
                if stamp is not None:
                    current.setdefault("exdates", []).append(_iso(stamp[0]))
        elif key == "STATUS":
            current["status"] = value.strip().lower()
        elif key == "X-MEETINGPILOT-AGENT":
            current["agent_tag"] = value.strip()
        elif key == "ATTENDEE":
            address = value.strip()
            if address.lower().startswith("mailto:"):
                address = address[7:]
            current.setdefault("attendees", []).append(address)

    out: list[dict[str, Any]] = []
    for item in events:
        start = item["start"]
        end = item.get("end")
        if end is None:
            duration = item.get("duration")
            if duration is not None:
                end = start + duration
            elif item.get("all_day"):
                end = start + timedelta(days=1)
            else:
                end = start
        row: dict[str, Any] = {
            "event_id": str(item["event_id"]),
            "id": str(item["event_id"]),
            "title": str(item.get("title") or "Meeting"),
            "start_utc": _iso(start),
            "end_utc": _iso(end),
            "status": str(item.get("status") or "confirmed"),
            "agent_tag": str(item.get("agent_tag") or ""),
            "attendees": list(item.get("attendees") or []),
        }
        # Recurrence metadata has to survive into the rows: expand_events() needs it.
        if item.get("rrule"):
            row["rrule"] = item["rrule"]
        if item.get("exdates"):
            row["exdates"] = item["exdates"]
        out.append(row)
    return out


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MAX_OCCURRENCES = 400
_MAX_STEPS = 40000  # bounds a rule with no COUNT/UNTIL (~109 years of days)


def _parse_rrule(value: str) -> dict[str, str]:
    rule: dict[str, str] = {}
    for chunk in value.split(";"):
        key, _, raw = chunk.partition("=")
        if key.strip():
            rule[key.strip().upper()] = raw.strip()
    return rule


def _add_months(moment: datetime, months: int) -> datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    # 31 Jan + 1 month is 28/29 Feb, the way every calendar client does it.
    day = min(moment.day, monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _stepped_starts(start: datetime, freq: str, interval: int):
    for step in range(_MAX_STEPS):
        if freq == "DAILY":
            yield start + timedelta(days=step * interval)
        elif freq == "MONTHLY":
            yield _add_months(start, step * interval)
        else:  # YEARLY
            yield _add_months(start, 12 * step * interval)


def _weekly_starts(start: datetime, interval: int, byday: list[str]):
    weekdays = sorted(
        {_WEEKDAYS[token.strip()[-2:]] for token in byday if token.strip()[-2:] in _WEEKDAYS}
    )
    if not weekdays:
        weekdays = [start.weekday()]
    week_start = start - timedelta(days=start.weekday())
    for step in range(_MAX_STEPS):
        base = week_start + timedelta(weeks=step * interval)
        for weekday in weekdays:
            candidate = (base + timedelta(days=weekday)).replace(
                hour=start.hour,
                minute=start.minute,
                second=start.second,
                microsecond=start.microsecond,
            )
            if candidate >= start:
                yield candidate


def _occurrence_starts(start: datetime, rule: dict[str, str]):
    """Yield occurrence starts for one RRULE, honouring COUNT, UNTIL, INTERVAL and BYDAY."""
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    count = None
    if rule.get("COUNT"):
        try:
            count = max(0, int(rule["COUNT"]))
        except ValueError:
            count = None
    until = None
    if rule.get("UNTIL"):
        try:
            until = _parse_dt(rule["UNTIL"])
        except ValueError:
            until = None
    byday = [token for token in rule.get("BYDAY", "").split(",") if token.strip()]

    if freq == "WEEKLY":
        starts = _weekly_starts(start, interval, byday)
    elif freq in {"DAILY", "MONTHLY", "YEARLY"}:
        starts = _stepped_starts(start, freq, interval)
    else:
        yield start
        return

    emitted = 0
    for occurrence in starts:
        if until is not None and occurrence > until:
            return
        yield occurrence
        emitted += 1
        if count is not None and emitted >= count:
            return


def expand_events(
    events: list[dict[str, Any]],
    range_start: datetime,
    range_end: datetime,
    *,
    cap: int = _MAX_OCCURRENCES,
) -> list[dict[str, Any]]:
    """Expand recurring events into the occurrences overlapping ``[range_start, range_end)``.

    Non-recurring events pass through unchanged. Occurrences keep the series' ``event_id``
    (free/busy only needs the interval) and carry ``recurrence_id`` so a trace can name one.
    """
    expanded: list[dict[str, Any]] = []
    for event in events:
        start = _as_dt(event["start_utc"])
        end = _as_dt(event["end_utc"])
        duration = end - start
        rule = _parse_rrule(str(event.get("rrule") or ""))
        if not rule:
            if start < range_end and end > range_start:
                expanded.append(event)
            continue
        excluded = {_as_dt(stamp).astimezone(timezone.utc) for stamp in event.get("exdates") or []}
        emitted = 0
        for occurrence_start in _occurrence_starts(start, rule):
            if occurrence_start >= range_end:
                break
            occurrence_end = occurrence_start + duration
            if occurrence_end <= range_start:
                continue
            if occurrence_start.astimezone(timezone.utc) in excluded:
                continue
            expanded.append(
                {
                    **event,
                    "start_utc": _iso(occurrence_start),
                    "end_utc": _iso(occurrence_end),
                    "recurrence_id": _iso(occurrence_start),
                }
            )
            emitted += 1
            if emitted >= cap:
                break
    return expanded


def render_ics(
    event_id: str,
    title: str,
    start: str | datetime,
    end: str | datetime,
    attendees: list[str] | None = None,
    status: str = "CONFIRMED",
    agent_tag: str = AGENT_TAG,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MeetingPilot//CalDAV//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{event_id}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{_fmt_dt(start)}",
        f"DTEND:{_fmt_dt(end)}",
        f"SUMMARY:{_escape(title)}",
        f"STATUS:{status.upper()}",
        f"X-MEETINGPILOT-AGENT:{agent_tag}",
    ]
    for address in attendees or []:
        lines.append(f"ATTENDEE:mailto:{address}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------
class CalDavCalendar:
    def __init__(self, settings: Settings):
        self.settings = settings
        base = settings.caldav_url.rstrip("/") + "/"
        self._principal = base
        self._collection = base + quote(settings.caldav_calendar.strip("/")) + "/"
        self._ready = False
        self._cache: dict[str, dict[str, Any]] = {}
        self._http: httpx.AsyncClient | None = None

    # -- plumbing ------------------------------------------------------
    def _http_client(self) -> httpx.AsyncClient:
        """One pooled client per adapter, so DAV connections survive between calls."""
        if self._http is None or self._http.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": self.settings.caldav_timeout_s,
                "verify": self.settings.caldav_verify_tls,
            }
            if self.settings.caldav_user:
                kwargs["auth"] = (self.settings.caldav_user, self.settings.caldav_password)
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    @contextlib.asynccontextmanager
    async def _client(self) -> Any:
        yield self._http_client()

    async def aclose(self) -> None:
        client, self._http = self._http, None
        if client is not None:
            await client.aclose()

    def _href(self, event_id: str) -> str:
        return f"{self._collection}{quote(event_id)}.ics"

    def _fail(self, response: httpx.Response, action: str) -> None:
        status = response.status_code
        detail = f"caldav {action} failed: HTTP {status}"
        if status == 429:
            raise RateLimitError(detail)
        if status >= 500:
            raise ServerError(detail)
        raise ProviderError(detail)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async with self._client() as client:
            if not self._ready:
                created = await client.request(
                    "MKCALENDAR",
                    self._collection,
                    content=_MKCALENDAR_BODY,
                    headers={"Content-Type": "application/xml; charset=utf-8"},
                )
                if created.status_code == 409:
                    # The principal collection does not exist yet (fresh Radicale user).
                    await client.request("MKCOL", self._principal)
                    created = await client.request(
                        "MKCALENDAR",
                        self._collection,
                        content=_MKCALENDAR_BODY,
                        headers={"Content-Type": "application/xml; charset=utf-8"},
                    )
                if created.status_code not in {200, 201, 204, 405, 409}:
                    self._fail(created, "MKCALENDAR")
                self._ready = True
            try:
                response = await client.request(method, url, content=content, headers=headers)
            except httpx.TimeoutException as error:
                raise ProviderTimeout(f"caldav {method} timed out: {error}") from error
            except httpx.HTTPError as error:
                raise ProviderError(f"caldav {method} failed: {error}") from error
        self._note_response(response)
        return response

    def _note_response(self, response: httpx.Response) -> None:
        """Re-probe the collection next time when it vanished (Radicale restart, fresh volume)."""
        if response.status_code in {409, 410} or response.status_code >= 500:
            self._ready = False

    # -- reads ---------------------------------------------------------
    async def freebusy(self, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        start, end = _as_dt(start_utc), _as_dt(end_utc)
        # Recurring series are expanded here: the REPORT only returns each series once.
        events = expand_events(await self._report(start_utc, end_utc), start, end)
        busy: list[dict[str, Any]] = []
        for event in events:
            if event["status"] == "cancelled":
                continue
            busy.append(
                {
                    "event_id": event["event_id"],
                    "start_utc": event["start_utc"],
                    "end_utc": event["end_utc"],
                    "status": event["status"],
                    "agent_tag": event["agent_tag"],
                }
            )
        return busy

    async def _report(self, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        body = _REPORT_BODY.format(start=_fmt_dt(start_utc), end=_fmt_dt(end_utc))
        response = await self._request(
            "REPORT",
            self._collection,
            content=body,
            headers={
                "Content-Type": "application/xml; charset=utf-8",
                "Depth": "1",
            },
        )
        if response.status_code != 207:
            self._fail(response, "REPORT")
        events: list[dict[str, Any]] = []
        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as error:
            raise ProviderError(f"caldav returned invalid XML: {error}") from error
        for node in root.findall(f"{DAV}response"):
            data = node.find(f".//{CALDAV}calendar-data")
            if data is None or not data.text:
                continue
            for event in parse_ics(data.text):
                self._cache[event["event_id"]] = event
                events.append(event)
        return events

    async def get_own_event(self, event_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", self._href(event_id))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._fail(response, "GET event")
        events = parse_ics(response.text)
        if not events:
            return None
        self._cache[events[0]["event_id"]] = events[0]
        return events[0]

    async def list_events(self) -> list[dict[str, Any]]:
        events = await self._report("20000101T000000Z", "21000101T000000Z")
        return sorted(events, key=lambda event: event["start_utc"])

    def _rows(self) -> list[dict[str, Any]]:
        """Dashboard-compatible view of the last events this adapter saw."""
        return sorted(self._cache.values(), key=lambda event: event["start_utc"])

    # -- writes --------------------------------------------------------
    async def create_hold(
        self,
        title: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = AGENT_TAG,
    ) -> dict[str, Any]:
        event_id = f"hold_{uuid.uuid4().hex[:10]}"
        body = render_ics(event_id, title, start_utc, end_utc, attendees, "TENTATIVE", agent_tag)
        response = await self._request(
            "PUT",
            self._href(event_id),
            content=body,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if response.status_code not in {200, 201, 204}:
            self._fail(response, "PUT hold")
        hold = {
            "event_id": event_id,
            "id": event_id,
            "title": title,
            "start_utc": _iso(_as_dt(start_utc)),
            "end_utc": _iso(_as_dt(end_utc)),
            "status": "tentative",
            "agent_tag": agent_tag,
            "attendees": list(attendees),
        }
        self._cache[event_id] = hold
        return hold

    async def cancel_own_hold(self, event_id: str) -> dict[str, Any]:
        event = await self.get_own_event(event_id)
        if event is None:
            return {"ok": False, "reason": "not_found"}
        if event["agent_tag"] != AGENT_TAG:
            return {"ok": False, "reason": "not_agent_owned"}
        body = render_ics(
            event_id,
            event["title"],
            event["start_utc"],
            event["end_utc"],
            event["attendees"],
            "CANCELLED",
        )
        response = await self._request(
            "PUT",
            self._href(event_id),
            content=body,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if response.status_code not in {200, 201, 204}:
            self._fail(response, "cancel hold")
        cancelled = {**event, "status": "cancelled"}
        self._cache[event_id] = cancelled
        return {"ok": True, "event_id": event_id, "status": "cancelled"}

    async def confirm_event(
        self,
        event_id: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str],
        agent_tag: str = AGENT_TAG,
    ) -> dict[str, Any]:
        event = await self.get_own_event(event_id)
        if event is None:
            return {"ok": False, "reason": "not_found"}
        if event["agent_tag"] != AGENT_TAG:
            return {"ok": False, "reason": "not_agent_owned"}
        body = render_ics(
            event_id, event["title"], start_utc, end_utc, attendees, "CONFIRMED", agent_tag
        )
        response = await self._request(
            "PUT",
            self._href(event_id),
            content=body,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if response.status_code not in {200, 201, 204}:
            self._fail(response, "confirm event")
        confirmed = {
            "event_id": event_id,
            "id": event_id,
            "title": event["title"],
            "start_utc": _iso(_as_dt(start_utc)),
            "end_utc": _iso(_as_dt(end_utc)),
            "status": "confirmed",
            "agent_tag": agent_tag,
            "attendees": list(attendees),
        }
        self._cache[event_id] = confirmed
        return {
            "ok": True,
            "event_id": event_id,
            "status": "confirmed",
            "start_utc": confirmed["start_utc"],
            "end_utc": confirmed["end_utc"],
        }

    # -- test/one-off helper -------------------------------------------
    async def insert_event(
        self,
        title: str,
        start_utc: str,
        end_utc: str,
        attendees: list[str] | None = None,
        status: str = "confirmed",
        agent_tag: str = "",
        event_id: str | None = None,
    ) -> str:
        """Seed one event; used by tests and local demos, never by the pipeline."""
        event_id = event_id or f"evt_{uuid.uuid4().hex[:10]}"
        body = render_ics(event_id, title, start_utc, end_utc, attendees, status, agent_tag)
        response = await self._request(
            "PUT",
            self._href(event_id),
            content=body,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if response.status_code not in {200, 201, 204}:
            self._fail(response, "seed event")
        return event_id
