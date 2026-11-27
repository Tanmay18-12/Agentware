"""Provider adapters (mock first, real IMAP/SMTP, Mailpit and CalDAV behind the same ports)."""

from .caldav_calendar import CalDavCalendar
from .fault_injector import FaultInjector
from .imap_mail import ImapMail
from .mailpit_mail import MailpitMail
from .mock_calendar import MockCalendar
from .mock_mail import MockMail

__all__ = [
    "CalDavCalendar",
    "FaultInjector",
    "ImapMail",
    "MailpitMail",
    "MockCalendar",
    "MockMail",
]
