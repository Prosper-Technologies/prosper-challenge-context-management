"""SchedulingSession: explicit updates, invalidation, offers, confirmation and booking."""

from datetime import datetime

import pytest

from catalog.availability import CLINIC_TZ, Calendar
from catalog.models import default_catalog
from catalog.policy import default_policy
from catalog.session import BookingError, InvalidUpdate, SchedulingSession, SessionState

NOW = datetime(2026, 10, 19, 7, 0, tzinfo=CLINIC_TZ)
MRI = {"service_id": "appt_065", "provider_id": "prov_015", "location_id": "loc_005"}
EXISTING = {"patient": {"is_new_patient": False, "has_referral": True}}


@pytest.fixture
def calendar() -> Calendar:
    return Calendar(default_catalog(), clock=lambda: NOW)


@pytest.fixture
def session() -> SchedulingSession:
    return SchedulingSession(default_policy(), "s1")


def ready(session, **choices):
    session.update({**EXISTING, **(choices or MRI)})
    assert session.check().status == "ready"
    return session


def selected(session, calendar, index=0):
    offer = session.offer_times(calendar)
    session.update({"slot_id": offer.slots[index].id})
    return offer


# ---- updates ------------------------------------------------------------------------


def test_omitted_fields_stay_and_null_clears(session):
    session.update({"service_id": "appt_065", "patient": {"has_referral": True}})
    session.update({"provider_id": "prov_015"})
    assert session.state.draft.service_id == "appt_065"
    assert session.state.patient.has_referral is True

    session.update({"patient": {"is_new_patient": False}})
    assert session.state.patient.has_referral is True  # other facts stay too
    session.update({"service_id": None, "patient": {"has_referral": None}})
    assert session.state.draft.service_id is None
    assert session.state.patient.model_dump() == {"is_new_patient": False, "has_referral": None}


def test_each_effective_change_is_one_revision(session):
    assert session.update({"service_id": "appt_065"}).revision == 1
    result = session.update({"service_id": "appt_065"})
    assert not result.changed and result.revision == 1
    assert session.update({"provider_id": "prov_015", "location_id": "loc_005"}).revision == 2


@pytest.mark.parametrize(
    "args",
    [
        {"provider_id": "prov_999"},
        {"service_id": "appt_011", "location_id": "loc_042"},
        {"patient": {"is_new_patient": "yes"}},
        {"patient": {"referral": True}},
        {"service_id": 7},
        {"colour": "blue"},
        {"slot_id": "prov_015.loc_005.appt_065.20261019T0800"},  # never offered
    ],
)
def test_invalid_updates_change_nothing(session, args):
    session.update({"service_id": "appt_065"})
    before = session.state
    with pytest.raises(InvalidUpdate) as e:
        session.update(args)
    assert e.value.errors
    assert session.state == before


def test_searches_dont_choose(session):
    # A one-option draft is still a question for the caller, not a choice made.
    result = session.update({**EXISTING, "service_id": "appt_065", "location_id": "loc_004"})
    assert result.narrowing.next_question.field == "provider_id"
    assert [o.provider_id for o in result.narrowing.options] == ["prov_014"]
    assert session.state.draft.provider_id is None


# ---- corrections --------------------------------------------------------------------


def test_explicit_infeasible_choice_is_kept_and_explained(session):
    result = session.update({**EXISTING, "service_id": "appt_021", "provider_id": "prov_000", "location_id": "loc_007"})
    assert result.cleared == ()
    assert session.state.draft.location_id == "loc_007"
    assert [v.code for v in session.explain()] == ["LOCATION_LACKS_CAPABILITY"]


def test_service_change_clears_incompatible_downstream_choices(session):
    ready(session, service_id="appt_021", provider_id="prov_000", location_id="loc_001")
    # prov_000 offers EKG, and EKG needs no capability: both are kept.
    assert session.update({"service_id": "appt_023"}).cleared == ()
    # Nobody at loc_001 does MRIs: provider and location both go.
    result = session.update({"service_id": "appt_065"})
    assert [(c.field, c.reason) for c in result.cleared] == [
        ("provider_id", "PROVIDER_DOESNT_OFFER"),
        ("location_id", "NO_FEASIBLE_OPTION"),
    ]
    assert result.narrowing.next_question.field == "provider_id"


def test_downstream_change_keeps_upstream_choices(session):
    ready(session)
    result = session.update({"location_id": "loc_000"})  # no imaging there
    assert result.cleared == ()
    assert session.state.draft.service_id == "appt_065"
    assert [v.code for v in result.narrowing.violations] == ["LOCATION_LACKS_CAPABILITY"]


def test_provider_change_clears_a_location_they_dont_work_at(session):
    ready(session, service_id="appt_002", provider_id="prov_047", location_id="loc_000")
    result = session.update({"provider_id": "prov_045"})  # loc_004 only
    assert [(c.field, c.value) for c in result.cleared] == [("location_id", "loc_000")]


def test_becoming_a_new_patient_clears_a_closed_provider(session):
    ready(session, service_id="appt_002", provider_id="prov_043", location_id="loc_004")
    result = session.update({"patient": {"is_new_patient": True}})
    assert [(c.field, c.reason) for c in result.cleared] == [("provider_id", "PROVIDER_NOT_ACCEPTING")]
    assert session.state.draft.location_id == "loc_004"
    accepting = {p.id for p in default_catalog().providers if p.accepting_new_patients}
    assert {o.provider_id for o in result.narrowing.options} <= accepting


def test_a_fact_change_never_clears_the_service(session):
    ready(session)
    result = session.update({"patient": {"has_referral": False}})
    assert result.cleared == ()
    assert session.state.draft.model_dump(exclude={"slot_id"}) == MRI
    assert [v.code for v in result.narrowing.violations] == ["REFERRAL_REQUIRED"]


# ---- offers -------------------------------------------------------------------------


def test_offers_need_a_ready_draft(session, calendar):
    session.update({"service_id": "appt_065"})
    with pytest.raises(BookingError) as e:
        session.offer_times(calendar)
    assert e.value.code == "NOT_READY"
    assert session.state.offer is None


def test_selecting_a_slot_carries_the_offer_forward(session, calendar):
    ready(session)
    offer = session.offer_times(calendar)
    assert len(offer.slots) == 3 and offer.revision == session.state.revision
    session.update({"slot_id": offer.slots[0].id})
    assert session.state.offer.revision == session.state.revision
    # Another offered time can still be chosen at the new revision.
    session.update({"slot_id": offer.slots[1].id})
    assert session.selected_slot() == offer.slots[1]


def test_offers_accumulate_within_a_revision(session, calendar):
    ready(session)
    first = session.offer_times(calendar, part_of_day="morning")
    later = session.offer_times(calendar, part_of_day="afternoon")
    session.update({"slot_id": first.slots[0].id})  # still offered
    assert all(s.start.hour >= 12 for s in later.slots)


@pytest.mark.parametrize("change", [{"location_id": "loc_004", "provider_id": "prov_014"}, {"patient": {"has_referral": None}}])
def test_changes_invalidate_offers_and_the_slot(session, calendar, change):
    ready(session)
    offer = selected(session, calendar)
    result = session.update(change)
    assert ("slot_id", "OFFER_INVALIDATED") in [(c.field, c.reason) for c in result.cleared]
    assert session.state.offer is None and session.state.draft.slot_id is None
    with pytest.raises(InvalidUpdate):
        session.update({"slot_id": offer.slots[0].id})


def test_a_slot_cant_be_selected_with_a_choice_change(session, calendar):
    ready(session)
    offer = session.offer_times(calendar)
    with pytest.raises(InvalidUpdate):
        session.update({"slot_id": offer.slots[0].id, "patient": {"is_new_patient": False, "has_referral": None}})


# ---- confirmation and booking -------------------------------------------------------


def test_booking_needs_confirmation_of_the_current_revision(session, calendar):
    ready(session)
    selected(session, calendar)
    revision = session.state.revision
    with pytest.raises(BookingError) as e:
        session.book(calendar, revision)
    assert e.value.code == "NOT_CONFIRMED"

    session.confirm(revision)
    session.update({"slot_id": session.state.offer.slots[1].id})  # a change clears it
    assert session.state.confirmed is False
    with pytest.raises(BookingError) as e:
        session.book(calendar, revision)
    assert e.value.code == "STALE_REVISION"


def test_confirm_needs_a_selected_slot(session, calendar):
    ready(session)
    with pytest.raises(BookingError) as e:
        session.confirm(session.state.revision)
    assert e.value.code == "NO_SLOT"


def test_book_and_retry(session, calendar):
    ready(session)
    offer = selected(session, calendar)
    revision = session.state.revision
    session.confirm(revision)
    receipt = session.book(calendar, revision)
    assert receipt.slot == offer.slots[0]
    assert receipt.patient == session.state.patient
    assert session.book(calendar, revision) == receipt  # a retry
    assert calendar.slots(*MRI.values(), day=offer.slots[0].start.date())[0] != offer.slots[0]

    # The reservation stays apart from the draft; a second booking isn't supported.
    session.update({"slot_id": offer.slots[1].id})
    session.confirm(session.state.revision)
    with pytest.raises(BookingError) as e:
        session.book(calendar, session.state.revision)
    assert e.value.code == "ALREADY_BOOKED"
    assert session.state.reservation == receipt


def test_idempotency_survives_lost_session_state(calendar):
    # A retry from state saved before the receipt was written: the calendar key
    # returns the first receipt instead of booking again.
    session = ready(SchedulingSession(default_policy(), "s2"))
    selected(session, calendar)
    session.confirm(session.state.revision)
    saved = session.state.dump()
    receipt = session.book(calendar, session.state.revision)

    retry = SchedulingSession(default_policy(), SessionState.load(saved))
    assert retry.book(calendar, retry.state.revision) == receipt


def test_state_round_trips_through_json(session, calendar):
    ready(session)
    selected(session, calendar)
    session.confirm(session.state.revision)
    session.book(calendar, session.state.revision)
    data = session.state.dump()
    assert SessionState.load(data) == session.state
    resumed = SchedulingSession(default_policy(), SessionState.load(data))
    assert resumed.check() == session.check()
    assert resumed.selected_slot() == session.selected_slot()
