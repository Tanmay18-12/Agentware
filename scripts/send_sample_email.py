#!/usr/bin/env python3
"""Deliver one sample meeting request to the configured SMTP host (Mailpit by default).

Usage: uv run python scripts/send_sample_email.py "Can we meet tomorrow at 10:00 IST for 30 min?"
"""

from __future__ import annotations

import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingpilot.config import Settings  # noqa: E402

DEFAULT_BODY = (
    "Hi, can we meet tomorrow at 10:00 IST for 30 minutes to go over the roadmap? "
    "Thanks, Dana"
)


def build(settings: Settings, body: str, sender: str) -> EmailMessage:
    now = datetime.now(timezone.utc)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = settings.prefs.owner_email
    message["Subject"] = "Meeting request"
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid(domain="dana.test")
    message["Thread-Id"] = f"sample-{int(now.timestamp())}"
    message["Reply-To"] = sender
    message["Authentication-Results"] = "spf=pass dkim=pass dmarc=pass"
    message.set_content(body)
    return message


def main(argv: list[str]) -> int:
    settings = Settings.from_env()
    body = argv[1] if len(argv) > 1 else DEFAULT_BODY
    sender = argv[2] if len(argv) > 2 else "dana@company.com"
    message = build(settings, body, sender)

    factory = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    with factory(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)

    print(
        f"delivered {message['Message-ID']} to "
        f"{settings.smtp_host}:{settings.smtp_port} (from {sender})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
