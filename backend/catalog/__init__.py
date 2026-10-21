"""The clinic catalog domain: typed catalog data, free-text matching, the booking
policy engine, the mock calendar, per-call scheduling sessions and catalog lint.

Pure Python with no Pipecat or HTTP dependency, so voice, HTTP and evaluation
adapters can share it."""

from .availability import Calendar, Receipt, Slot, SlotUnavailable, voice_calendar
from .index import CatalogIndex, SearchResult, default_index
from .lint import CatalogIssue, lint
from .models import (
    AppointmentType,
    BookingDraft,
    Catalog,
    Location,
    PatientFacts,
    Provider,
    default_catalog,
    load_catalog,
)
from .policy import Narrowing, Policy, Violation, default_policy
from .session import BookingError, InvalidUpdate, SchedulingSession, SessionState

__all__ = [
    "AppointmentType",
    "BookingDraft",
    "BookingError",
    "Calendar",
    "Catalog",
    "CatalogIndex",
    "CatalogIssue",
    "InvalidUpdate",
    "Location",
    "Narrowing",
    "PatientFacts",
    "Policy",
    "Provider",
    "Receipt",
    "SchedulingSession",
    "SearchResult",
    "SessionState",
    "Slot",
    "SlotUnavailable",
    "Violation",
    "default_catalog",
    "default_index",
    "default_policy",
    "lint",
    "load_catalog",
    "voice_calendar",
]
