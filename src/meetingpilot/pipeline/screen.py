"""Stage 3 -- Screen: injection heuristics, header spoof checks and risk flags (all code)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..config import Prefs
from ..schemas import EmailEnvelope, MeetingIntent

INJECTION_PHRASES = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore previous",
    "ignore all prior",
    "disregard all previous",
    "disregard prior",
    "system prompt",
    "you are now",
    "forward all",
    "forward every",
    "send all calendar",
    "reveal the calendar",
    "exfiltrate",
    "exfiltrating",
    "attacker",
    "print your instructions",
    "new instructions",
    "developer mode",
)
MONEY_LEGAL_RE = re.compile(
    r"\b(invoice|wire transfer|payment|refund|contract|legal action|lawsuit|nda|settlement|"
    r"purchase order|bank details)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenResult:
    injection_score: float
    flags: tuple[str, ...]
    quarantine: bool
    spoof: bool


AUTH_FAIL_VALUES = {"fail", "softfail", "permerror", "none"}


def _auth_failures(auth_results: dict[str, str]) -> list[str]:
    failures: list[str] = []
    lowered = {str(k).lower(): str(v).lower() for k, v in auth_results.items()}
    for mechanism in ("spf", "dkim", "dmarc"):
        value = lowered.get(mechanism, "")
        if value in AUTH_FAIL_VALUES and value != "none":
            failures.append(f"{mechanism}_fail")
    raw = lowered.get("raw", "")
    for mechanism in ("spf", "dkim", "dmarc"):
        if mechanism not in failures and re.search(
            rf"{mechanism}\s*=\s*(fail|softfail|permerror)", raw
        ):
            failures.append(f"{mechanism}_fail")
    return failures


def screen(envelope: EmailEnvelope) -> ScreenResult:
    body = envelope.body_text.lower()
    flags: list[str] = []
    score = 0.0

    if envelope.hidden_text_flag or "hidden_text" in envelope.header_flags:
        score += 0.6
        flags.append("hidden_text")
    if "zero_width" in envelope.header_flags or any(ch in envelope.body_text for ch in "\u200b\u200c\u200d\ufeff"):
        score += 0.2
        flags.append("zero_width_chars")

    phrases = [phrase for phrase in INJECTION_PHRASES if phrase in body]
    if phrases:
        # one strong phrase is enough to stop the pipeline; more phrases only raise the score
        score = min(1.0, score + 0.5 + 0.25 * (len(phrases) - 1))
        flags.extend(f"phrase:{phrase}" for phrase in phrases[:6])

    spoof = False
    if envelope.reply_to:
        reply_domain = envelope.reply_to.rsplit("@", 1)[-1].lower()
        from_domain = envelope.from_addr.rsplit("@", 1)[-1].lower()
        if reply_domain != from_domain:
            spoof = True
            score += 0.4
            flags.append("reply_to_domain_mismatch")
    if envelope.return_path:
        rp_domain = envelope.return_path.rsplit("@", 1)[-1].lower()
        from_domain = envelope.from_addr.rsplit("@", 1)[-1].lower()
        if rp_domain != from_domain:
            spoof = True
            score += 0.2
            flags.append("return_path_mismatch")

    failures = _auth_failures(envelope.auth_results)
    if failures:
        spoof = True
        score += 0.3 * len(failures)
        flags.extend(failures)

    if MONEY_LEGAL_RE.search(envelope.body_text):
        flags.append("money_or_legal_wording")

    score = round(min(1.0, score), 3)
    # One strong injection phrase (0.5) or any hidden-text signal is enough to stop here,
    # before the reader model or any provider call happens.
    quarantine = score >= 0.5 or ("spf_fail" in failures and "dkim_fail" in failures)
    return ScreenResult(injection_score=score, flags=tuple(flags), quarantine=quarantine, spoof=spoof)


def risk_flags(envelope: EmailEnvelope, intent: MeetingIntent, prefs: Prefs) -> list[str]:
    """The risky 10%: reasons to stop and ask a human before acting."""
    risks: list[str] = []
    domain = envelope.from_addr.rsplit("@", 1)[-1].lower()
    if envelope.from_addr.lower() in {v.lower() for v in prefs.vip_senders}:
        risks.append("vip_sender")
    if not prefs.domain_allowed(domain) and not prefs.domain_auto_approve(domain):
        risks.append("unknown_external_domain")
    if len(intent.attendees) > prefs.max_attendees:
        risks.append("too_many_attendees")
    external = [
        address
        for address in intent.attendees
        if address.rsplit("@", 1)[-1].lower() not in {d.lower() for d in prefs.auto_approve_domains}
    ]
    if external:
        risks.append("external_attendees")
    if MONEY_LEGAL_RE.search(envelope.body_text):
        risks.append("money_or_legal_wording")
    if intent.injection_suspected:
        risks.append("injection_suspected")
    if intent.confidence < 0.75:
        risks.append("low_confidence")
    return risks


def quoted_regions(body: str) -> Iterable[str]:
    """Lines that look like a quoted reply (used to keep extraction on the new text)."""
    for line in body.splitlines():
        if line.strip().startswith(">"):
            continue
        yield line
