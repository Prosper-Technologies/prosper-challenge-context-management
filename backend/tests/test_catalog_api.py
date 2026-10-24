"""The catalog API (catalog_api.py): catalog, lint and tool metadata, and the tool
playground's isolated, expiring sessions."""

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog import lint
from catalog.availability import CLINIC_TZ, Calendar, voice_calendar
from catalog.models import CATALOG_PATH, default_catalog
from catalog.tools import GUARDS, TOOLS
from catalog_api import PlaygroundSessions, build_router
from agent_builder import AgentBuilder
from catalog.policy import default_policy
from catalog_voice import build_registry, load_session
from eval.flow import TextFlow

NOW = datetime(2026, 10, 19, 7, 0, tzinfo=CLINIC_TZ)
MRI = {
    "patient": {"is_new_patient": False, "has_referral": True},
    "service_id": "appt_065",
    "provider_id": "prov_014",
    "location_id": "loc_004",
}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sessions(clock) -> PlaygroundSessions:
    return PlaygroundSessions(ttl_s=600, limit=3, clock=clock, calendar=lambda: Calendar(clock=lambda: NOW))


@pytest.fixture
def client(sessions) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(sessions))
    return TestClient(app)


def new_session(client) -> str:
    response = client.post("/api/catalog/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def run(client, session_id: str, tool: str, args=None) -> dict:
    response = client.post(f"/api/catalog/tools/{tool}", json={"session_id": session_id, "args": args})
    assert response.status_code == 200, response.text
    return response.json()


# ---- catalog, lint, tools -----------------------------------------------------------


def test_catalog_is_served_as_loaded(client):
    catalog = client.get("/api/catalog").json()
    assert catalog == default_catalog().model_dump(mode="json")
    raw = json.loads(CATALOG_PATH.read_text())
    assert [p["id"] for p in catalog["providers"]] == [p["id"] for p in raw["providers"]]
    assert (len(catalog["locations"]), len(catalog["providers"]), len(catalog["appointment_types"])) == (8, 50, 82)


def test_lint_matches_the_domain(client):
    issues = client.get("/api/catalog/lint").json()["issues"]
    assert issues == [i.model_dump(mode="json") for i in lint(default_catalog())]
    codes = [i["code"] for i in issues]
    assert codes.count("UNBOOKABLE_TYPE") == 8 and codes.count("DUPLICATE_PROVIDER_NAME") == 3


def test_tools_list_schemas_and_guards(client):
    body = client.get("/api/catalog/tools").json()
    assert [t["name"] for t in body["tools"]] == list(TOOLS)
    for spec in body["tools"]:
        assert spec["parameters"] == TOOLS[spec["name"]].parameters()
        assert spec["description"] == TOOLS[spec["name"]].description
    guards = {g["name"]: g for g in body["guards"]}
    assert list(guards) == list(GUARDS)
    assert guards["booking_completed"]["completes"] == ["book_appointment"]
    assert guards["booking_ready"]["completes"] == []


# ---- playground sessions ---------------------------------------------------------------


def test_a_new_session_is_empty(client):
    body = client.post("/api/catalog/sessions").json()
    assert body["session_id"].startswith("play-")
    assert body["ttl_s"] == 600
    state = body["state"]
    assert state["id"] == body["session_id"]
    assert state["revision"] == 0 and state["reservation"] is None
    assert state["patient"] == {"is_new_patient": None, "has_referral": None}


def test_tool_runs_return_result_state_and_estimate(client):
    sid = new_session(client)
    body = run(client, sid, "update_booking", MRI)
    assert body["result"]["check"]["status"] == "ready"
    assert body["state"]["revision"] == 1
    assert body["state"]["draft"]["provider_id"] == "prov_014"
    assert body["approx_tokens"] == pytest.approx(len(json.dumps(body["result"], separators=(",", ":"))) / 4, abs=1)

    # Patient controls are update_booking calls: an explicit null clears one fact.
    body = run(client, sid, "update_booking", {"patient": {"has_referral": None}})
    assert body["state"]["patient"] == {"is_new_patient": False, "has_referral": None}
    assert body["result"]["check"]["next_question"]["field"] == "has_referral"


def test_check_booking_reports_echo_at_richmond(client):
    sid = new_session(client)
    run(client, sid, "update_booking", {"service_id": "appt_021", "provider_id": "prov_000", "location_id": "loc_007"})
    check = run(client, sid, "check_booking")["result"]["check"]
    assert check["status"] == "blocked"
    (violation,) = check["violations"]
    assert violation["code"] == "LOCATION_LACKS_CAPABILITY"
    assert "Mission District Family Clinic" in violation["fix"]


def test_a_full_booking_in_the_playground(client):
    sid = new_session(client)
    run(client, sid, "update_booking", MRI)
    slot = run(client, sid, "get_availability")["result"]["slots"][0]
    revision = run(client, sid, "update_booking", {"slot_id": slot["id"]})["state"]["revision"]
    body = run(client, sid, "book_appointment", {"draft_revision": revision, "confirmed": True})
    assert body["result"]["confirmation"].startswith("BK-")
    assert body["state"]["reservation"]["slot"]["id"] == slot["id"]


def test_tool_errors_are_results_and_change_nothing(client):
    sid = new_session(client)
    before = run(client, sid, "update_booking", MRI)["state"]

    bad = run(client, sid, "update_booking", {"service_id": "appt_999", "location_id": "loc_001"})
    assert bad["result"]["error"]["code"] == "INVALID_UPDATE"
    assert bad["state"] == before

    bad = run(client, sid, "get_availability", {"part_of_day": "midnight"})
    assert bad["result"]["error"]["code"] == "INVALID_ARGUMENTS"
    assert bad["state"] == before


def test_request_errors(client):
    sid = new_session(client)
    assert client.post("/api/catalog/tools/teleport", json={"session_id": sid}).status_code == 404
    assert client.post("/api/catalog/tools/check_booking", json={"session_id": "play-nope"}).status_code == 404
    assert client.post("/api/catalog/tools/check_booking", json={"session": sid}).status_code == 422
    extra = {"session_id": sid, "args": {}, "agent": "x"}
    assert client.post("/api/catalog/tools/check_booking", json=extra).status_code == 422
    assert client.delete("/api/catalog/sessions/play-nope").status_code == 404


def test_reset_disposes_of_the_session(client):
    sid = new_session(client)
    run(client, sid, "update_booking", MRI)

    assert client.delete(f"/api/catalog/sessions/{sid}").status_code == 204
    response = client.post("/api/catalog/tools/check_booking", json={"session_id": sid})
    assert response.status_code == 404  # never silently a fresh session under the old id

    fresh = new_session(client)
    assert fresh != sid
    assert run(client, fresh, "check_booking")["state"]["revision"] == 0


def test_sessions_expire_after_inactivity(client, clock):
    idle, active = new_session(client), new_session(client)
    clock.t = 500
    run(client, active, "check_booking")  # a request keeps a session alive
    clock.t = 700

    response = client.post("/api/catalog/tools/check_booking", json={"session_id": idle})
    assert response.status_code == 410
    assert "expired" in response.json()["detail"]
    assert run(client, active, "check_booking")["state"]["id"] == active
    assert client.delete(f"/api/catalog/sessions/{idle}").status_code == 204  # already gone


def test_the_least_recently_used_session_expires_at_the_limit(client, sessions, clock):
    ids = []
    for t in range(3):
        clock.t = t
        ids.append(new_session(client))
    clock.t = 3
    run(client, ids[0], "check_booking")
    clock.t = 4
    new_session(client)

    assert len(sessions) == 3
    assert client.post("/api/catalog/tools/check_booking", json={"session_id": ids[1]}).status_code == 410
    assert run(client, ids[0], "check_booking")["state"]["id"] == ids[0]


def test_sessions_have_their_own_calendars(client):
    a, b = new_session(client), new_session(client)
    for sid in (a, b):
        run(client, sid, "update_booking", MRI)
    slot = run(client, a, "get_availability")["result"]["slots"][0]
    assert run(client, b, "get_availability")["result"]["slots"][0] == slot

    for sid in (a, b):  # the same time books in both sandboxes
        revision = run(client, sid, "update_booking", {"slot_id": slot["id"]})["state"]["revision"]
        body = run(client, sid, "book_appointment", {"draft_revision": revision, "confirmed": True})
        assert body["result"]["start"] == slot["label"]


def test_the_playground_never_uses_the_voice_calendar():
    before = voice_calendar.cache_info().currsize
    app = FastAPI()
    app.include_router(build_router(PlaygroundSessions()))
    client = TestClient(app)
    sid = new_session(client)
    run(client, sid, "update_booking", MRI)
    run(client, sid, "get_availability")
    assert voice_calendar.cache_info().currsize == before


# An agent whose one node exposes every tool, for driving the evaluation adapter.
ALL_TOOLS_AGENT = {
    "name": "All tools",
    "persona": "Test.",
    "initial_node": "tools",
    "nodes": [
        {
            "name": "tools",
            "task_messages": [{"role": "developer", "content": "Use the tools."}],
            "tools": sorted(TOOLS),
            "edges": [{"function": "booking_done", "description": "Booked.", "target": "done", "guard": "booking_completed"}],
        },
        {"name": "done", "task_messages": [{"role": "developer", "content": "Say goodbye."}], "end": True},
    ],
}


def without_session_ids(value):
    """A result or session state without what derives from the session id."""
    if isinstance(value, dict):
        return {k: without_session_ids(v) for k, v in value.items() if k not in {"id", "session_id", "confirmation"}}
    if isinstance(value, list):
        return [without_session_ids(v) for v in value]
    return value


def test_voice_http_and_evaluation_adapters_give_the_same_results(client):
    """The same calls through the voice adapter (a registry binding on a flow state),
    HTTP (a playground session) and the evaluation (the compiled graph driven as
    text) give the same results, refusals included, and leave the same session."""
    calls = [
        ("apply_request", {"service_text": "knee MRI", "provider_text": "Chen"}),  # clarification, no update
        ("find_providers", {"name": "Dr. Chen"}),
        ("update_booking", {"provider_id": "prov_999"}),
        ("get_availability", {"day": "not a date"}),
        ("apply_request", {"service_text": "knee MRI", "provider_id": MRI["provider_id"],
                           "location_text": "Downtown", "patient": MRI["patient"]}),
        ("get_availability", {"part_of_day": "afternoon"}),
        ("update_booking", {"location_id": "loc_001"}),
        ("check_booking", {}),
        ("update_booking", {"location_id": "loc_004"}),
        ("get_availability", {}),
        ("update_booking", lambda seen: {"slot_id": seen["slots"][0]["id"]}),
        ("book_appointment", lambda seen: {"draft_revision": seen["draft"]["revision"], "confirmed": False}),
        ("book_appointment", lambda seen: {"draft_revision": seen["draft"]["revision"] - 1, "confirmed": True}),
        ("book_appointment", lambda seen: {"draft_revision": seen["draft"]["revision"], "confirmed": True}),
        ("book_appointment", lambda seen: {"draft_revision": seen["draft"]["revision"], "confirmed": True}),
    ]
    refused = {2, 3, 11, 12}
    registry = build_registry(calendar=Calendar(clock=lambda: NOW))
    voice_state: dict = {}
    sid = new_session(client)
    flow = TextFlow(AgentBuilder.from_dict(ALL_TOOLS_AGENT, build_registry(calendar=Calendar(clock=lambda: NOW))))
    flow.start()

    seen: dict = {}  # the latest of each part of a result, for later arguments
    receipts = []
    for n, (name, args) in enumerate(calls):
        args = args(seen) if callable(args) else args
        voice, _ = registry.tools[name].run(args, voice_state)
        http = run(client, sid, name, args)
        evaluation, _ = asyncio.run(flow.call(name, json.dumps(args)))
        assert without_session_ids(http["result"]) == without_session_ids(voice), name
        assert without_session_ids(evaluation) == without_session_ids(voice), name
        assert ("error" in voice) == (n in refused), (n, name, voice)
        seen.update(voice)
        if "confirmation" in voice:
            receipts.append(voice)
    assert len(receipts) == 2 and receipts[0] == receipts[1]  # the retry returns the same receipt

    sessions = [load_session(voice_state, default_policy()), load_session(flow.state, default_policy())]
    states = [without_session_ids(s.state.dump()) for s in sessions]
    assert states[0] == states[1] == without_session_ids(http["state"])
    assert states[0]["reservation"] is not None


def test_importing_the_catalog_api_loads_no_pipecat():
    script = (
        "import sys, catalog_api; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'pipecat', 'pipecat_flows', 'agent_builder'}))"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent.parent,
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert out.stdout.strip() == "[]"
