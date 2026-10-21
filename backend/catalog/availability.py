#
# Availability — a deterministic mock calendar.
#
# A provider's base schedule at a location on a date comes from a stable digest of
# (provider, location, date): whether they work there that day, and which half-hour
# blocks are already taken. Python's `hash()` is randomized per process, so it isn't
# used. Days are the clinic's local calendar days (America/Los_Angeles), Mon-Fri
# 08:00-17:00, for 14 days from the injected clock's today. Slots are sized by the
# service's duration and laid back to back from 08:00.
#
# The base schedules of a multi-location provider can overlap across clinics (it's a
# mock), but reservations can't: they occupy the provider's time at every service and
# location, so a 45-minute booking blocks each overlapping candidate, whatever its
# slot ID or clinic. Revalidation and reservation happen under one lock, keyed by an
# idempotency key, so a retry returns the first receipt instead of booking twice.
#
# Everything is in memory. `voice_calendar()` is shared by voice calls in this
# process; playground sessions and evaluation runs each create their own Calendar.
#

import hashlib
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from functools import cache
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from .models import Catalog, PatientFacts, default_catalog

CLINIC_TZ = ZoneInfo("America/Los_Angeles")
OPEN, CLOSE = time(8, 0), time(17, 0)
HORIZON_DAYS = 14  # local calendar days, today included
BLOCK = timedelta(minutes=30)  # granularity of the pre-booked base schedule
TAKEN_SHARE = 90  # of 256: about a third of blocks are already booked
SEED = "clinic-calendar-v1"

PartOfDay = Literal["morning", "afternoon"]
Clock = Callable[[], datetime]


class Slot(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)

    id: str
    service_id: str
    provider_id: str
    location_id: str
    start: datetime
    end: datetime


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)

    confirmation: str
    slot: Slot
    patient: PatientFacts
    session_id: str
    draft_revision: int
    booked_at: datetime


class SlotUnavailable(Exception):
    def __init__(self, reason: Literal["not_in_schedule", "outside_window", "taken"], message: str):
        self.reason = reason
        super().__init__(message)


def system_clock() -> datetime:
    return datetime.now(CLINIC_TZ)


class Calendar:
    def __init__(self, catalog: Optional[Catalog] = None, clock: Clock = system_clock):
        self.catalog = catalog or default_catalog()
        self._clock = clock
        self._lock = threading.Lock()
        self._booked: dict[str, list[tuple[datetime, datetime]]] = {}  # provider id -> intervals
        self._receipts: dict[str, Receipt] = {}  # idempotency key -> receipt

    def now(self) -> datetime:
        return self._clock().astimezone(CLINIC_TZ)

    def days(self) -> list[date]:
        """The bookable local days: weekdays in the horizon, today included."""
        today = self.now().date()
        days = (today + timedelta(days=i) for i in range(HORIZON_DAYS))
        return [d for d in days if d.weekday() < 5]

    # ---- reading ----------------------------------------------------------------

    def slots(
        self,
        service_id: str,
        provider_id: str,
        location_id: str,
        day: Optional[date] = None,
        part_of_day: Optional[PartOfDay] = None,
    ) -> list[Slot]:
        """Free slots in time order. Doesn't check the booking policy: callers ask
        only for combinations the policy allows."""
        now = self.now()
        days = [d for d in self.days() if day is None or d == day]
        with self._lock:
            booked = list(self._booked.get(provider_id, ()))
        out = []
        for d in days:
            for slot in self._candidates(service_id, provider_id, location_id, d):
                if slot.start <= now or _overlaps(slot, booked):
                    continue
                if part_of_day is not None and _part_of_day(slot.start) != part_of_day:
                    continue
                out.append(slot)
        return out

    def receipt(self, key: str) -> Optional[Receipt]:
        with self._lock:
            return self._receipts.get(key)

    # ---- booking ----------------------------------------------------------------

    def reserve(self, slot: Slot, key: str, patient: PatientFacts, session_id: str, revision: int) -> Receipt:
        """Revalidate `slot` and book it, atomically. The same key returns the
        receipt it already booked."""
        with self._lock:
            if key in self._receipts:
                return self._receipts[key]
            day = slot.start.astimezone(CLINIC_TZ).date()
            if day not in self.days() or slot.start <= self.now():
                raise SlotUnavailable("outside_window", "That time is no longer bookable.")
            if slot not in self._candidates(slot.service_id, slot.provider_id, slot.location_id, day):
                raise SlotUnavailable("not_in_schedule", "That time isn't in the provider's schedule.")
            booked = self._booked.setdefault(slot.provider_id, [])
            if _overlaps(slot, booked):
                raise SlotUnavailable("taken", "The provider was booked at an overlapping time.")
            booked.append((slot.start, slot.end))
            receipt = Receipt(
                confirmation="BK-" + _digest(key)[:4].hex().upper(),
                slot=slot,
                patient=patient,
                session_id=session_id,
                draft_revision=revision,
                booked_at=self.now(),
            )
            self._receipts[key] = receipt
            return receipt

    # ---- base schedule ----------------------------------------------------------

    def _candidates(self, service_id: str, provider_id: str, location_id: str, day: date) -> list[Slot]:
        """Slots the base schedule leaves open, ignoring reservations and the clock."""
        provider = self.catalog.provider_by_id[provider_id]
        duration = timedelta(minutes=self.catalog.type_by_id[service_id].duration_min)
        digest = _digest(f"{provider_id}|{location_id}|{day.isoformat()}")
        # Works here on about 1 in N days for N locations; every weekday for one.
        if day.weekday() >= 5 or digest[0] % len(provider.location_ids):
            return []
        opens = datetime.combine(day, OPEN, CLINIC_TZ)
        closes = datetime.combine(day, CLOSE, CLINIC_TZ)
        taken = [
            (opens + i * BLOCK, opens + (i + 1) * BLOCK)
            for i, byte in enumerate(digest[1 : 1 + (closes - opens) // BLOCK])
            if byte < TAKEN_SHARE
        ]
        out = []
        start = opens
        while start + duration <= closes:
            slot = Slot(
                id=f"{provider_id}.{location_id}.{service_id}.{start:%Y%m%dT%H%M}",
                service_id=service_id,
                provider_id=provider_id,
                location_id=location_id,
                start=start,
                end=start + duration,
            )
            if not _overlaps(slot, taken):
                out.append(slot)
            start += duration
        return out


def _digest(text: str) -> bytes:
    return hashlib.sha256(f"{SEED}|{text}".encode()).digest()


def _overlaps(slot: Slot, intervals) -> bool:
    return any(slot.start < end and start < slot.end for start, end in intervals)


def _part_of_day(start: datetime) -> PartOfDay:
    return "morning" if start.astimezone(CLINIC_TZ).hour < 12 else "afternoon"


@cache
def voice_calendar() -> Calendar:
    """The calendar voice calls share in this server process."""
    return Calendar()
