"""The catalog tool registry (catalog/tools.py): schemas, compact capped results,
structured errors, and the guards."""

import json
from datetime import datetime

import pytest

from catalog.availability import CLINIC_TZ, Calendar
from catalog.index import default_index
from catalog.models import default_catalog
from catalog.policy import default_policy
from catalog.session import SchedulingSession
from catalog.tools import GUARDS, TOOLS, ToolContext, approx_tokens

NOW = datetime(2026, 10, 19, 7, 0, tzinfo=CLINIC_TZ)
# Generous for the biggest page (five same-name providers with all their locations);
# typical results are well under half of this.
TOKEN_BUDGET = 400


@pytest.fixture
def ctx() -> ToolContext:
    return context()


def context(session_id: str = "t", calendar=None) -> ToolContext:
    calendar = calendar or Calendar(default_catalog(), clock=lambda: NOW)
    return ToolContext(SchedulingSession(default_policy(), session_id), calendar, default_index())


def run(ctx, tool: str, **args) -> dict:
    result = TOOLS[tool].run(ctx, args)
    assert approx_tokens(result) <= TOKEN_BUDGET, f"{tool} {args}: {approx_tokens(result)} tokens"
    json.dumps(result)  # JSON-compatible
    return result


def ready_mri(ctx) -> dict:
    return run(
        ctx,
        "update_booking",
        patient={"is_new_patient": False, "has_referral": True},
        service_id="appt_065",
        provider_id="prov_014",
        location_id="loc_004",
    )


def selected(ctx) -> dict:
    ready_mri(ctx)
    offer = run(ctx, "get_availability")
    return run(ctx, "update_booking", slot_id=offer["slots"][0]["id"])


# ---- schemas ------------------------------------------------------------------------


def test_every_tool_has_an_inline_llm_schema():
    for tool in TOOLS.values():
        schema = tool.parameters()
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text and '"title"' not in text, tool.name
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])
        assert len(tool.description) < 250


def test_optional_arguments_are_nullable_and_required_ones_are_not():
    update = TOOLS["update_booking"].parameters()
    assert update["required"] == []
    assert update["properties"]["service_id"]["type"] == ["string", "null"]
    assert update["properties"]["patient"]["properties"]["has_referral"]["type"] == ["boolean", "null"]
    book = TOOLS["book_appointment"].parameters()
    assert book["required"] == ["draft_revision", "confirmed"]


# ---- searches -----------------------------------------------------------------------


def test_overview_pages_are_small(ctx):
    first = run(ctx, "catalog_overview")
    assert [loc["id"] for loc in first["locations"]] == [f"loc_00{i}" for i in range(8)]
    assert first["next_cursor"] == "1"
    second = run(ctx, "catalog_overview", cursor="1")
    assert second["specialties"]["Radiology"] == 8
    assert second["not_offered"] == ["Ophthalmology", "Physical Therapy", "Urology"]
    assert "next_cursor" not in second
    assert max(approx_tokens(first), approx_tokens(second)) <= 250


def test_ambiguous_search_says_what_to_ask(ctx):
    result = run(ctx, "search_services", query="checkup")
    assert result["ambiguous"] and result["clarify"] == "specialty"
    assert result["total_matches"] == len(result["services"]) == 4
    assert "ask which one" in result["hint"]


@pytest.mark.parametrize(
    "tool, args, key",
    [("search_services", {"query": "moon base"}, "services"), ("find_providers", {"name": "moon base"}, "providers"),
     ("find_locations", {"query": "moon base"}, "locations")],
)  # fmt: skip
def test_unrelated_searches_match_nothing_and_say_so(ctx, tool, args, key):
    result = run(ctx, tool, **args)
    assert result[key] == [] and result["total_matches"] == 0
    assert "ambiguous" not in result and "rephrase" in result["hint"]


def test_service_notes_flag_unbookable_and_gated_services(ctx):
    eye = run(ctx, "search_services", query="eye exam")["services"][0]
    assert "can't be booked" in eye["notes"][0]
    mri = run(ctx, "search_services", query="knee MRI")["services"][0]
    assert mri["requires_referral"] and not mri["new_patients_allowed"]
    assert mri["notes"] == ["Needs imaging: bookable at 2 location(s)."]


def test_every_chen_is_reachable_through_the_cap(ctx):
    first = run(ctx, "find_providers", name="Dr. Chen")
    assert len(first["providers"]) == 5 and first["total_matches"] == 7
    assert first["truncated"] and first["next_cursor"] == "5"
    second = run(ctx, "find_providers", name="Dr. Chen", cursor=first["next_cursor"])
    ids = [p["id"] for p in first["providers"] + second["providers"]]
    assert len(set(ids)) == 7
    assert "truncated" not in second


def test_provider_cards_tell_namesakes_apart_and_show_service_locations(ctx):
    result = run(ctx, "find_providers", name="Dr. Ramirez", service_id="appt_065")
    (card,) = result["providers"]
    assert card["id"] == "prov_014" and card["specialty"] == "Radiology"
    assert card["locations"] == {"loc_004": "Downtown Health Center"}
    assert {e["id"] for e in result["excluded"]} == {"prov_005", "prov_021"}


def test_locations_for_a_service_carry_unknown_facts_as_conditions(ctx):
    result = run(ctx, "find_locations", service_id="appt_065")
    assert [loc["id"] for loc in result["locations"]] == ["loc_004", "loc_005"]
    assert all(loc["conditions"] == ["has_referral", "is_new_patient"] for loc in result["locations"])
    assert "ambiguous" not in result  # a listing, not an ambiguous query

    run(ctx, "update_booking", patient={"has_referral": True, "is_new_patient": False})
    result = run(ctx, "find_locations", service_id="appt_065")
    assert all("conditions" not in loc for loc in result["locations"])


def test_a_named_location_that_cant_work_says_why(ctx):
    result = run(ctx, "find_locations", query="Richmond", service_id="appt_065")
    assert result["locations"] == []
    assert result["excluded"][0]["reason"] == "MRI - Knee needs imaging, which Richmond Care Center doesn't have."


def test_locations_ruled_out_by_facts_report_the_violation(ctx):
    run(ctx, "update_booking", patient={"is_new_patient": True})
    result = run(ctx, "find_locations", service_id="appt_065")
    assert result["locations"] == []
    assert [v["code"] for v in result["violations"]] == ["TYPE_NOT_FOR_NEW_PATIENTS"]


def test_searches_never_change_the_session(ctx):
    before = ctx.session.state
    run(ctx, "catalog_overview")
    run(ctx, "search_services", query="MRI")
    run(ctx, "find_providers", name="Chen")
    run(ctx, "find_locations", service_id="appt_065")
    run(ctx, "check_booking")
    assert ctx.session.state == before


# ---- the booking ----------------------------------------------------------------------


def test_update_returns_named_draft_and_next_question(ctx):
    result = run(ctx, "update_booking", service_id="appt_065")
    assert result["changed"]
    assert result["draft"]["service"] == {"id": "appt_065", "name": "MRI - Knee"}
    assert result["draft"]["patient"] == {"is_new_patient": None, "has_referral": None}
    assert result["check"]["next_question"]["field"] == "has_referral"
    labels = [o["label"] for o in result["check"]["options"]]
    assert labels[0] == "MRI - Knee with Dr. Linda Ramirez (DO, Radiology) at Downtown Health Center"


def test_omitted_fields_stay_null_clears_and_null_patient_changes_nothing(ctx):
    ready_mri(ctx)
    result = run(ctx, "update_booking", patient=None, location_id=None)
    assert result["draft"]["patient"] == {"is_new_patient": False, "has_referral": True}
    assert "location" not in result["draft"] and result["draft"]["provider"]["id"] == "prov_014"
    result = run(ctx, "update_booking", patient={"has_referral": None})
    assert result["draft"]["patient"] == {"is_new_patient": False, "has_referral": None}


def test_corrections_report_what_was_cleared(ctx):
    selected(ctx)
    # Annual Physical: the radiologist doesn't offer it, but Downtown still works.
    result = run(ctx, "update_booking", service_id="appt_002")
    assert {c["field"] for c in result["cleared"]} == {"provider_id", "slot_id"}
    assert result["draft"]["location"]["id"] == "loc_004"
    assert "slot" not in result["draft"] and "provider" not in result["draft"]


def test_invalid_updates_are_structured_and_change_nothing(ctx):
    ready_mri(ctx)
    before = ctx.session.state
    unknown = run(ctx, "update_booking", service_id="appt_999", provider_id="prov_001")
    assert unknown["error"]["code"] == "INVALID_UPDATE"
    assert unknown["error"]["errors"][0]["path"] == ["service_id"]
    bad_type = run(ctx, "update_booking", patient={"is_new_patient": "maybe"})
    assert bad_type["error"]["code"] == "INVALID_ARGUMENTS"
    extra = run(ctx, "update_booking", doctor="Chen")
    assert extra["error"]["errors"] == [{"path": ["doctor"], "message": "Extra inputs are not permitted"}]
    unknown_filter = run(ctx, "find_providers", service_id="appt_999")
    assert unknown_filter["error"]["code"] == "UNKNOWN_ID"
    assert ctx.session.state == before


def test_check_booking_reports_violations_with_fixes(ctx):
    # Echocardiogram with Dr. David Chen at Richmond, which has no imaging.
    run(ctx, "update_booking", service_id="appt_021", provider_id="prov_000", location_id="loc_007")
    result = run(ctx, "check_booking")
    assert result["check"]["status"] == "blocked"
    (violation,) = result["check"]["violations"]
    assert violation["code"] == "LOCATION_LACKS_CAPABILITY"
    assert violation["fix"] == "Dr. David Chen (MD, Cardiology) offers Echocardiogram at Mission District Family Clinic, Downtown Health Center."
    assert "handoff" in result["check"]["hint"]


def test_availability_needs_a_ready_draft(ctx):
    run(ctx, "update_booking", service_id="appt_065")
    result = run(ctx, "get_availability")
    assert result["error"]["code"] == "NOT_READY"
    assert result["error"]["next_question"]["field"] == "has_referral"
    assert ctx.session.state.offer is None


def test_availability_offers_three_times_and_ways_to_refine(ctx):
    ready_mri(ctx)
    result = run(ctx, "get_availability")
    assert len(result["slots"]) == 3 and result["total"] > 3 and result["truncated"]
    assert result["today"] == "Monday 2026-10-19"
    assert result["slots"][0]["label"] == "Wednesday, October 21 at 9:30 AM"
    assert result["revision"] == ctx.session.state.revision
    day = result["other_days"][0].split()[1]
    later = run(ctx, "get_availability", day=day)
    assert later["slots"] and all(s["start"].startswith(day) for s in later["slots"])
    empty = run(ctx, "get_availability", day="2026-10-24")  # a Saturday
    assert empty["slots"] == [] and empty["other_days"] and "other_days" in empty["hint"]


def test_booking_needs_explicit_confirmation_of_the_current_revision(ctx):
    draft = selected(ctx)["draft"]
    refused = run(ctx, "book_appointment", draft_revision=draft["revision"], confirmed=False)
    assert refused["error"]["code"] == "NOT_CONFIRMED"
    stale = run(ctx, "book_appointment", draft_revision=draft["revision"] - 1, confirmed=True)
    assert stale["error"]["code"] == "STALE_REVISION"
    assert ctx.session.state.reservation is None

    receipt = run(ctx, "book_appointment", draft_revision=draft["revision"], confirmed=True)
    assert receipt["confirmation"].startswith("BK-")
    assert receipt["provider"] == "Dr. Linda Ramirez (DO, Radiology)"
    assert receipt["start"] == draft["slot"]["label"]
    # A retry returns the same receipt instead of booking twice.
    assert run(ctx, "book_appointment", draft_revision=draft["revision"], confirmed=True) == receipt
    assert run(ctx, "check_booking")["draft"]["confirmation"] == receipt["confirmation"]


def test_a_time_taken_by_another_session_is_a_structured_error():
    calendar = Calendar(default_catalog(), clock=lambda: NOW)
    a, b = context("a", calendar), context("b", calendar)
    revision_a = selected(a)["draft"]["revision"]
    revision_b = selected(b)["draft"]["revision"]
    assert run(a, "book_appointment", draft_revision=revision_a, confirmed=True)["confirmation"]
    taken = run(b, "book_appointment", draft_revision=revision_b, confirmed=True)
    assert taken["error"]["code"] == "SLOT_UNAVAILABLE"
    assert "get_availability" in taken["error"]["hint"]


# ---- guards -------------------------------------------------------------------------


def test_guards_follow_the_booking(ctx):
    def failing():
        return {name for name, g in GUARDS.items() if g.run(ctx.session) is not None}

    assert failing() == set(GUARDS)
    run(ctx, "update_booking", service_id="appt_065")
    assert failing() == {"booking_ready", "slot_selected", "booking_completed"}
    draft = selected(ctx)["draft"]
    assert failing() == {"booking_completed"}
    run(ctx, "book_appointment", draft_revision=draft["revision"], confirmed=True)
    assert failing() == set()


def test_a_failed_guard_says_how_to_recover(ctx):
    run(ctx, "update_booking", service_id="appt_065")
    failure = GUARDS["booking_ready"].run(ctx.session)
    assert failure["guard"] == "booking_ready"
    assert failure["next_question"]["field"] == "has_referral"
    run(ctx, "update_booking", patient={"has_referral": False})
    failure = GUARDS["booking_ready"].run(ctx.session)
    assert [v["code"] for v in failure["violations"]] == ["REFERRAL_REQUIRED"]


def test_only_the_completion_guard_completes_booking():
    assert {name: g.completes for name, g in GUARDS.items() if g.completes} == {
        "booking_completed": ("book_appointment",)
    }
