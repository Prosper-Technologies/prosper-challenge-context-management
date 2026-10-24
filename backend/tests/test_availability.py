"""The mock calendar: deterministic schedules, the time window, intervals and atomic booking."""

import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from catalog.availability import CLINIC_TZ, Calendar, Slot, SlotUnavailable
from catalog.models import PatientFacts, default_catalog

MONDAY_7AM = datetime(2026, 10, 19, 7, 0, tzinfo=CLINIC_TZ)
ECHO = ("appt_021", "prov_000", "loc_001")  # 45 min, Mission District
EKG = ("appt_023", "prov_000", "loc_000")  # 20 min, same provider at Mission Bay
FACTS = PatientFacts(is_new_patient=False, has_referral=True)


def calendar(now=MONDAY_7AM) -> Calendar:
    return Calendar(default_catalog(), clock=lambda: now)


def reserve(cal, slot, key):
    return cal.reserve(slot, key, FACTS, key, 1)


def test_schedule_is_deterministic_across_processes():
    first = [s.id for s in calendar().slots(*ECHO)]
    script = (
        "from datetime import datetime\n"
        "from catalog.availability import CLINIC_TZ, Calendar\n"
        "cal = Calendar(clock=lambda: datetime(2026, 10, 19, 7, tzinfo=CLINIC_TZ))\n"
        "print(','.join(s.id for s in cal.slots('appt_021', 'prov_000', 'loc_001')))\n"
    )
    other = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parent.parent,
        env={"PYTHONHASHSEED": "12345"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert other.stdout.strip().split(",") == first
    # Pinned, so a change to the generator is a deliberate one.
    assert [s.start.strftime("%a %H:%M") for s in first_day(calendar().slots(*ECHO))] == [
        "Mon 08:00", "Mon 08:45", "Mon 09:30", "Mon 10:15", "Mon 12:30", "Mon 14:00", "Mon 15:30", "Mon 16:15",
    ]  # fmt: skip


def first_day(slots):
    return [s for s in slots if s.start.date() == slots[0].start.date()]


def test_slots_are_weekdays_in_clinic_hours_sized_by_duration():
    cal = calendar()
    slots = cal.slots(*ECHO) + cal.slots(*EKG)
    assert slots
    for s in slots:
        local = s.start.astimezone(CLINIC_TZ)
        assert local.weekday() < 5
        assert local.date() < MONDAY_7AM.date() + timedelta(days=14)
        assert local.hour >= 8 and s.end.astimezone(CLINIC_TZ) <= local.replace(hour=17, minute=0)
        minutes = 45 if s.service_id == "appt_021" else 20
        assert s.end - s.start == timedelta(minutes=minutes)
        assert ((local.hour - 8) * 60 + local.minute) % minutes == 0


def test_days_are_local_calendar_days_across_dst():
    # DST ends on Sunday 1 Nov 2026: the next Monday still opens at 08:00 local.
    cal = calendar(datetime(2026, 10, 30, 12, 0, tzinfo=CLINIC_TZ))
    assert cal.days()[0] == date(2026, 10, 30) and len(cal.days()) == 10
    november = [s for s in cal.slots(*EKG) if s.start.date() >= date(2026, 11, 2)]
    assert november
    assert november[0].start.utcoffset() == timedelta(hours=-8)
    assert min(s.start.astimezone(CLINIC_TZ).hour for s in november) >= 8


def test_past_times_are_not_offered():
    noon = MONDAY_7AM.replace(hour=12)
    assert all(s.start > noon for s in calendar(noon).slots(*ECHO))


def test_filters_by_day_and_part_of_day():
    cal = calendar()
    monday = date(2026, 10, 19)
    assert {s.start.date() for s in cal.slots(*ECHO, day=monday)} == {monday}
    assert all(s.start.hour < 12 for s in cal.slots(*ECHO, part_of_day="morning"))
    assert all(s.start.hour >= 12 for s in cal.slots(*ECHO, part_of_day="afternoon"))


def test_a_reservation_blocks_overlaps_across_services_and_clinics():
    cal = calendar()
    echo = [s for s in cal.slots(*ECHO) if s.start.strftime("%H:%M") == "08:45"][0]  # 08:45-09:30
    ekg_before = cal.slots(*EKG, day=echo.start.date())
    overlapping = [s for s in ekg_before if s.start < echo.end and echo.start < s.end]
    assert overlapping

    reserve(cal, echo, "a")
    ekg_after = cal.slots(*EKG, day=echo.start.date())
    assert not [s for s in ekg_after if s.start < echo.end and echo.start < s.end]
    assert set(ekg_before) - set(ekg_after) == set(overlapping)
    with pytest.raises(SlotUnavailable) as e:
        reserve(cal, overlapping[0], "b")
    assert e.value.reason == "taken"


def test_adjacent_slots_dont_conflict():
    cal = calendar()
    first, second = cal.slots(*ECHO)[:2]  # 08:00-08:45, 08:45-09:30
    assert first.end == second.start
    reserve(cal, first, "a")
    reserve(cal, second, "b")


def test_reservation_revalidates_the_window_and_schedule():
    slot = calendar().slots(*ECHO)[0]
    with pytest.raises(SlotUnavailable) as e:
        reserve(calendar(slot.start + timedelta(minutes=1)), slot, "late")
    assert e.value.reason == "outside_window"

    invented = Slot(**{**slot.model_dump(), "id": "x", "start": slot.start + timedelta(minutes=5), "end": slot.end + timedelta(minutes=5)})
    with pytest.raises(SlotUnavailable) as e:
        reserve(calendar(), invented, "invented")
    assert e.value.reason == "not_in_schedule"


def test_the_same_key_returns_the_first_receipt():
    cal = calendar()
    first, second = cal.slots(*ECHO)[:2]
    receipt = reserve(cal, first, "k")
    assert reserve(cal, second, "k") == receipt
    assert cal.receipt("k") == receipt
    assert second in cal.slots(*ECHO)  # nothing else was booked


def test_concurrent_reservations_book_once():
    cal = calendar()
    slot = cal.slots(*ECHO)[0]
    results = []
    barrier = threading.Barrier(8)

    def attempt(i):
        barrier.wait()
        try:
            results.append(reserve(cal, slot, f"call-{i}"))
        except SlotUnavailable:
            results.append(None)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results if r is not None]) == 1


def test_calendars_are_independent():
    a, b = calendar(), calendar()
    slot = a.slots(*ECHO)[0]
    reserve(a, slot, "a")
    assert slot in b.slots(*ECHO)
