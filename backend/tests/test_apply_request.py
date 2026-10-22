"""Combined lookup/update: conservative identity resolution and atomic corrections."""

import pytest

from tests.test_tools import context, run, selected


@pytest.fixture
def ctx():
    return context()


MRI = {
    "service_text": "knee MRI",
    "provider_text": "Hannah Nguyen",
    "location_text": "Midtown",
    "patient": {"has_referral": True, "is_new_patient": False},
}


def test_one_request_resolves_names_and_applies_one_revision(ctx):
    result = run(ctx, "apply_request", **MRI)
    assert result["changed"] and result["check"]["status"] == "ready"
    assert result["draft"]["revision"] == 1
    assert result["draft"]["service"]["id"] == "appt_065"
    assert result["draft"]["provider"]["id"] == "prov_015"
    assert result["draft"]["location"]["id"] == "loc_005"
    assert ctx.session.state.reservation is None
    assert not run(ctx, "apply_request", **MRI)["changed"]
    assert ctx.session.state.revision == 1


def test_request_returns_next_question_without_guessing_missing_facts(ctx):
    result = run(ctx, "apply_request", service_text="knee MRI", provider_text="Hannah Nguyen")
    assert result["check"]["next_question"]["field"] == "has_referral"
    assert result["draft"]["patient"] == {"is_new_patient": None, "has_referral": None}
    assert "location" not in result["draft"]


@pytest.mark.parametrize("field, query, reason", [
    ("service_text", "checkup", "ambiguous"),
    ("service_text", "eye exam", "ambiguous"),  # exact plus a longer matching name
    ("service_text", "kne MRI", "confirm_match"),
    ("service_text", "moon base", "no_match"),
    ("provider_text", "Linda Ramirez", "ambiguous"),  # don't silently filter out the namesake
    ("provider_text", "Chen", "ambiguous"),
    ("location_text", "Mission", "ambiguous"),
    ("location_text", "moon base", "no_match"),
])
def test_unresolved_requests_leave_even_confirmed_drafts_untouched(ctx, field, query, reason):
    selected(ctx)
    ctx.session.confirm(ctx.session.state.revision)
    before = ctx.session.state
    result = run(ctx, "apply_request", **{**MRI, field: query})
    assert result["changed"] is False
    assert result["clarification"]["field"] == field
    assert result["clarification"]["reason"] == reason
    assert "draft" not in result and "check" not in result and "error" not in result
    assert ctx.session.state == before


def test_ambiguous_candidates_are_capped_distinguishable_and_pageable(ctx):
    clarification = run(ctx, "apply_request", provider_text="Chen")["clarification"]
    assert len(clarification["candidates"]) == 3
    assert clarification["total_matches"] == 7
    assert clarification["clarify"] == "specialty"
    assert "Cardiology" in clarification["candidates"][0]["name"]
    assert "Mission District" in clarification["candidates"][0]["name"]
    page = run(ctx, clarification["lookup_tool"], name="Chen", cursor=clarification["next_cursor"])
    ids = [c["id"] for c in clarification["candidates"] + page["providers"]]
    assert len(set(ids)) == len(ids) == 7


def test_caller_can_confirm_a_weak_match_and_retry_all_fields(ctx):
    result = run(ctx, "apply_request", **{**MRI, "service_text": "kne MRI"})
    candidate = result["clarification"]["candidates"][0]
    retry = {k: v for k, v in MRI.items() if k != "service_text"}
    result = run(ctx, "apply_request", **retry, service_id=candidate["id"])
    assert result["check"]["status"] == "ready"


def test_named_incompatible_choices_are_recorded_for_policy_explanation(ctx):
    result = run(ctx, "apply_request", **{**MRI, "location_text": "Richmond"})
    assert result["changed"] and result["check"]["status"] == "blocked"
    assert result["draft"]["provider"]["id"] == "prov_015"
    assert result["draft"]["location"]["id"] == "loc_007"
    assert "LOCATION_LACKS_CAPABILITY" in {v["code"] for v in result["check"]["violations"]}


def test_unoffered_service_is_recorded_instead_of_replaced(ctx):
    result = run(ctx, "apply_request", service_id="appt_045")
    assert result["check"]["status"] == "blocked"
    assert result["check"]["violations"][0]["code"] == "NOT_OFFERED"


def test_service_correction_invalidates_old_provider_slot_and_confirmation(ctx):
    selected(ctx)
    ctx.session.confirm(ctx.session.state.revision)
    before = ctx.session.state.revision
    result = run(ctx, "apply_request", service_text="Annual Physical")
    assert result["draft"]["revision"] == before + 1
    assert {c["field"] for c in result["cleared"]} == {"provider_id", "slot_id"}
    assert result["draft"]["location"]["id"] == "loc_004"
    assert not ctx.session.state.confirmed and ctx.session.state.offer is None


def test_omissions_preserve_and_nulls_clear(ctx):
    run(ctx, "apply_request", **MRI)
    result = run(ctx, "apply_request", location_text=None, patient=None)
    assert "location" not in result["draft"]
    assert result["draft"]["provider"]["id"] == "prov_015"
    assert result["draft"]["patient"] == MRI["patient"]
    result = run(ctx, "apply_request", patient={"has_referral": None})
    assert result["draft"]["patient"] == {"has_referral": None, "is_new_patient": False}


@pytest.mark.parametrize("extra", [
    {"service_id": "appt_065"},  # even matching IDs cannot accompany text for that field
    {"location_id": "loc_999"},
    {"slot_id": "not-offered"},
    {"patient": {"has_referral": "maybe"}},
    {"service_text": ""},
    {"confirmed": True},
])
def test_invalid_request_never_partially_updates(ctx, extra):
    selected(ctx)
    before = ctx.session.state
    result = run(ctx, "apply_request", **{**MRI, **extra})
    assert "error" in result
    assert ctx.session.state == before
