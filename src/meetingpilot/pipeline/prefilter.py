"""Stage 2 -- Prefilter: drop loops, bulk mail and abusive senders before any model call."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Prefs
from ..event_bus import StateStore
from ..schemas import EmailEnvelope


@dataclass(frozen=True)
class PrefilterResult:
    skip: bool
    reason: str = ""


LOOP_FLAGS = {"auto_submitted": "auto-submitted header", "list_unsubscribe": "list mail", "precedence_bulk": "bulk precedence"}


def prefilter(envelope: EmailEnvelope, prefs: Prefs, store: StateStore | None = None) -> PrefilterResult:
    for flag, reason in LOOP_FLAGS.items():
        if flag in envelope.header_flags:
            return PrefilterResult(True, f"skipped: {reason}")
    if "no_reply" in envelope.header_flags:
        return PrefilterResult(True, "skipped: no-reply sender")
    if "self_sent" in envelope.header_flags:
        return PrefilterResult(True, "skipped: message from the owner mailbox (loop guard)")

    domain = envelope.from_addr.rsplit("@", 1)[-1].lower()
    if domain in {d.lower() for d in prefs.blocked_domains}:
        return PrefilterResult(True, f"skipped: blocked sender domain {domain}")

    if store is not None:
        key = f"mail:{envelope.from_addr.lower()}"
        if store.hits_since(key, 3600) >= prefs.sender_emails_per_hour:
            store.hit(key)
            return PrefilterResult(True, "skipped: sender exceeded per-hour email rate limit")
        store.hit(key)

    return PrefilterResult(False, "")
