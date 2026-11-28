"""Prefilter rate limiting: dropped mail must not count towards the sender window."""

from __future__ import annotations

from datetime import datetime, timezone

from helpers import base_prefs, make_world
from meetingpilot.pipeline.prefilter import prefilter
from meetingpilot.schemas import EmailEnvelope

SENDER = "dana@company.com"


def _envelope(message_id: str) -> EmailEnvelope:
    return EmailEnvelope(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        from_addr=SENDER,
        received_at=datetime.now(timezone.utc),
    )


def test_rate_limited_sender_does_not_extend_its_own_block(tmp_path):
    world = make_world(tmp_path, prefs=base_prefs(sender_emails_per_hour=2), busy=[])
    key = f"mail:{SENDER}"

    accepted = [
        prefilter(_envelope(f"<accepted-{index}@mail.example.com>"), world.settings.prefs, world.bus.store)
        for index in range(2)
    ]
    assert [result.skip for result in accepted] == [False, False]
    hits_at_limit = world.bus.store.hits_since(key, 3600)

    blocked = [
        prefilter(_envelope(f"<blocked-{index}@mail.example.com>"), world.settings.prefs, world.bus.store)
        for index in range(3)
    ]
    assert all(result.skip for result in blocked)
    assert "rate limit" in blocked[0].reason
    # The old behaviour recorded a hit per dropped message, pushing the window forward forever.
    assert world.bus.store.hits_since(key, 3600) == hits_at_limit


def test_sender_under_the_limit_is_still_processed(tmp_path):
    world = make_world(tmp_path, prefs=base_prefs(sender_emails_per_hour=3), busy=[])

    result = prefilter(_envelope("<first@mail.example.com>"), world.settings.prefs, world.bus.store)

    assert result.skip is False
    assert world.bus.store.hits_since(f"mail:{SENDER}", 3600) == 1
