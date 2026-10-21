"""The policy engine: static triples, patient eligibility, explain() and narrow()."""

import pytest

from catalog.models import BookingDraft, PatientFacts, default_catalog
from catalog.policy import Policy, UnknownId, default_policy

EXISTING = PatientFacts(is_new_patient=False, has_referral=True)
NEW = PatientFacts(is_new_patient=True)
UNKNOWN = PatientFacts()


@pytest.fixture
def policy() -> Policy:
    return default_policy()


def narrow(policy, patient=UNKNOWN, **draft):
    return policy.narrow(BookingDraft(**draft), patient)


def codes(narrowing):
    return [v.code for v in narrowing.violations]


def test_static_triples(policy):
    assert len(policy.triples) == 760
    # Every Radiology imaging type has exactly two (provider, location) pairs.
    for t in default_catalog().appointment_types:
        if t.specialty == "Radiology":
            pairs = {(o.provider_id, o.location_id) for o in policy.options(service_id=t.id)}
            assert pairs == {("prov_014", "loc_004"), ("prov_015", "loc_005")}, t.name
    echo = {(o.provider_id, o.location_id) for o in policy.options(service_id="appt_021")}
    assert echo == {("prov_000", "loc_001"), ("prov_000", "loc_004"), ("prov_005", "loc_005"), ("prov_046", "loc_004")}


def test_eligibility_comes_from_ids_not_specialty_labels(policy):
    # A pediatrician (by label) offers Annual Physical (a "Family Medicine" type).
    assert policy.options("appt_002", "prov_002", "loc_000")
    assert narrow(policy, EXISTING, service_id="appt_002", provider_id="prov_002", location_id="loc_000").status == "ready"


# ---- the tricky cases ---------------------------------------------------------------


def test_ramirez_mri_is_only_the_radiologist_downtown(policy):
    ramirez = {"prov_005", "prov_014"}  # two Dr. Linda Ramirez
    options = [o for o in policy.options(service_id="appt_065") if o.provider_id in ramirez]
    assert [(o.provider_id, o.location_id) for o in options] == [("prov_014", "loc_004")]
    n = narrow(policy, EXISTING, service_id="appt_065", provider_id="prov_005")
    assert n.status == "blocked" and codes(n) == ["PROVIDER_DOESNT_OFFER"]


def test_echo_with_david_chen_at_richmond(policy):
    n = narrow(policy, EXISTING, service_id="appt_021", provider_id="prov_000", location_id="loc_007")
    assert n.status == "blocked"
    (v,) = n.violations
    assert v.code == "LOCATION_LACKS_CAPABILITY"
    assert v.fields == ("service_id", "location_id")
    assert v.fix == (
        "Dr. David Chen (MD, Cardiology) offers Echocardiogram at Mission District Family Clinic, Downtown Health Center."
    )


def test_dental_at_north_gate_is_blocked(policy):
    n = narrow(policy, EXISTING, service_id="appt_074", location_id="loc_003")
    assert codes(n) == ["LOCATION_LACKS_CAPABILITY"]
    assert "Mission District Family Clinic" in n.violations[0].fix


def test_new_patient_obgyn_is_not_accepting(policy):
    assert codes(narrow(policy, NEW, service_id="appt_040")) == ["PROVIDER_NOT_ACCEPTING"]
    assert codes(narrow(policy, NEW, service_id="appt_040", provider_id="prov_036")) == ["PROVIDER_NOT_ACCEPTING"]
    # An existing patient can book it.
    assert narrow(policy, EXISTING, service_id="appt_040").status == "needs_choice"


def test_new_patient_xray_is_blocked_but_mammogram_is_not(policy):
    xray = narrow(policy, NEW, service_id="appt_062")
    assert codes(xray) == ["TYPE_NOT_FOR_NEW_PATIENTS"]
    assert "Mammogram" in xray.violations[0].fix
    mammogram = narrow(policy, NEW, service_id="appt_068")
    assert mammogram.status == "needs_choice" and mammogram.next_question.field == "provider_id"


def test_ophthalmology_is_not_offered(policy):
    assert codes(narrow(policy, service_id="appt_045")) == ["NOT_OFFERED"]


def test_multi_location_provider_is_never_ready_before_the_location(policy):
    n = narrow(policy, EXISTING, service_id="appt_027", provider_id="prov_027")
    assert n.status == "needs_choice" and n.next_question.field == "location_id"
    assert n.total_options == 4 and len(n.options) == 3 and n.truncated
    assert narrow(policy, EXISTING, service_id="appt_027", provider_id="prov_027", location_id="loc_006").status == "ready"


def test_choices_with_no_completion(policy):
    # Cardiology needs no capability, but no cardiologist works at North Gate.
    n = narrow(policy, EXISTING, service_id="appt_020", location_id="loc_003")
    assert codes(n) == ["NO_FEASIBLE_OPTION"]


# ---- unknown facts ------------------------------------------------------------------


def test_unknown_facts_are_asked_not_assumed(policy):
    full = {"service_id": "appt_065", "provider_id": "prov_014", "location_id": "loc_004"}
    n = narrow(policy, UNKNOWN, **full)
    assert n.status == "needs_choice"
    assert n.next_question.kind == "fact" and n.next_question.field == "has_referral"
    assert n.unresolved == ("has_referral", "is_new_patient")
    n = narrow(policy, PatientFacts(has_referral=True), **full)
    assert n.next_question.field == "is_new_patient"
    assert narrow(policy, EXISTING, **full).status == "ready"


def test_unknown_isnt_false(policy):
    # An unknown referral doesn't block, and an unknown status isn't "new".
    n = narrow(policy, UNKNOWN, service_id="appt_065")
    assert n.status == "needs_choice" and n.violations == ()
    n = narrow(policy, UNKNOWN, service_id="appt_002", provider_id="prov_043", location_id="loc_004")
    assert n.status == "needs_choice" and n.next_question.field == "is_new_patient"


def test_new_patient_status_is_only_asked_when_it_matters(policy):
    # Accepting provider, type open to new patients: either answer can book.
    n = narrow(policy, UNKNOWN, service_id="appt_002", provider_id="prov_047", location_id="loc_000")
    assert n.status == "ready" and n.unresolved == ()


def test_known_facts_filter_options(policy):
    unknown = narrow(policy, UNKNOWN, service_id="appt_002")
    new = narrow(policy, NEW, service_id="appt_002")
    assert new.remaining["provider_id"] < unknown.remaining["provider_id"]
    closed = {p.id for p in default_catalog().providers if not p.accepting_new_patients}
    assert not closed & {o.provider_id for o in new.options}


# ---- narrow -------------------------------------------------------------------------


def test_service_is_asked_first(policy):
    n = narrow(policy, EXISTING, provider_id="prov_014")
    assert n.next_question.field == "service_id"
    assert [o.service_id for o in n.options] == ["appt_062", "appt_063", "appt_064"]
    assert n.total_options == 8 and n.truncated


def test_ready_lists_the_one_combination(policy):
    n = narrow(policy, EXISTING, service_id="appt_065", provider_id="prov_014", location_id="loc_004")
    assert n.status == "ready" and n.next_question is None
    assert [o.model_dump() for o in n.options] == [
        {"service_id": "appt_065", "provider_id": "prov_014", "location_id": "loc_004", "conditions": ()}
    ]


def test_unknown_ids_are_rejected(policy):
    with pytest.raises(UnknownId):
        narrow(policy, service_id="appt_999")
    with pytest.raises(UnknownId):
        narrow(policy, location_id="Downtown")
