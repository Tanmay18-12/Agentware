"""Provider adapters (mock first, fault-injectable)."""

from .fault_injector import FaultInjector
from .mock_calendar import MockCalendar
from .mock_mail import MockMail

__all__ = ["FaultInjector", "MockCalendar", "MockMail"]
