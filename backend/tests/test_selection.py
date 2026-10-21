"""Per-call agent selection, the /start preflight, and bot startup failures."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api
import bot
from agent_builder import parse
from api import AgentSelectionError, install, select_agent
from tests.helpers import EXAMPLE, draft


def test_no_selector_runs_the_default_agent(store):
    assert select_agent(None, store).config.name == EXAMPLE["name"]
    assert select_agent({}, store).config.name == EXAMPLE["name"]


def test_default_store_holds_the_checked_in_example():
    assert select_agent({}).config.name == EXAMPLE["name"]


def test_agent_id_runs_the_saved_agent(store, client):
    client.put("/api/agents/saved", json=draft(name="Saved"))
    assert select_agent({"agent_id": "saved"}, store).config.name == "Saved"


def test_inline_agent_wins_over_agent_id(store):
    builder = select_agent({"agent": draft(name="Draft"), "agent_id": "example"}, store)
    assert builder.config.name == "Draft"


@pytest.mark.parametrize(
    "body",
    [
        {"agent": draft(initial_node="missing")},  # graph error
        {"agent": {"name": "No nodes"}},  # structural error
        {"agent": "not an object"},
        {"agent": None},
        {"agent_id": "unknown"},  # never fall back to the example
        {"agent_id": "../example"},
        {"agent_id": 5},
        ["not", "an", "object"],
    ],
)
def test_invalid_explicit_selection_raises(body, store):
    with pytest.raises(AgentSelectionError):
        select_agent(body, store)


def test_saved_agent_with_graph_errors_is_rejected(store):
    """A structurally valid draft saves, but can't run."""
    store.save("broken", parse(draft(initial_node="missing"))[0])

    with pytest.raises(AgentSelectionError) as e:
        select_agent({"agent_id": "broken"}, store)
    assert [i.path for i in e.value.issues if i.level == "error"] == [["initial_node"]]


def test_unreadable_saved_agent_is_rejected(store):
    (store.root / "garbled.json").write_text("{not json")
    with pytest.raises(AgentSelectionError):
        select_agent({"agent_id": "garbled"}, store)


@pytest.fixture
def client(store) -> TestClient:
    app = FastAPI()
    install(app, store)

    @app.post("/start")  # stands in for the runner's route
    async def start(payload: dict):
        return {"sessionId": "s1", "echo": payload}

    return TestClient(app)


def test_start_preflight_passes_valid_selection_through(client):
    payload = {"body": {"agent": draft(name="Draft")}}
    response = client.post("/start", json=payload)

    assert response.status_code == 200
    assert response.json()["echo"] == payload  # body still readable downstream


def test_start_preflight_rejects_invalid_agent(client):
    response = client.post("/start", json={"body": {"agent": draft(initial_node="missing")}})

    assert response.status_code == 422
    assert "initial_node" in response.json()["detail"]
    assert response.json()["issues"][0]["path"] == ["initial_node"]


def test_start_preflight_rejects_unknown_agent_id(client):
    response = client.post("/start", json={"body": {"agent_id": "unknown"}})

    assert response.status_code == 422
    assert "unknown" in response.json()["detail"]


async def test_bot_disconnects_on_invalid_selection(monkeypatch):
    disconnected = []

    async def disconnect():
        disconnected.append(True)

    async def unexpected(*args, **kwargs):
        raise AssertionError("transport must not be created for an invalid agent")

    monkeypatch.setattr(bot, "create_transport", unexpected)
    runner_args = SimpleNamespace(
        body={"agent": {"name": "broken"}},
        webrtc_connection=SimpleNamespace(disconnect=disconnect),
    )
    await bot.bot(runner_args)

    assert disconnected == [True]


async def test_bot_runs_a_saved_agent(monkeypatch, store):
    store.save("saved", parse(draft(name="Saved"))[0])
    monkeypatch.setattr(api, "store", store)
    started = []

    async def fake_transport(runner_args, params):
        return "transport"

    async def fake_run_bot(transport, runner_args, builder):
        started.append(builder.config.name)

    monkeypatch.setattr(bot, "create_transport", fake_transport)
    monkeypatch.setattr(bot, "run_bot", fake_run_bot)
    await bot.bot(SimpleNamespace(body={"agent_id": "saved"}, webrtc_connection=None))

    assert started == ["Saved"]


async def test_bot_disconnects_when_the_call_fails_to_start(monkeypatch):
    disconnected = []

    async def disconnect():
        disconnected.append(True)

    async def failing_transport(*args, **kwargs):
        raise RuntimeError("no transport")

    monkeypatch.setattr(bot, "create_transport", failing_transport)
    runner_args = SimpleNamespace(
        body={}, webrtc_connection=SimpleNamespace(disconnect=disconnect)
    )
    await bot.bot(runner_args)

    assert disconnected == [True]
