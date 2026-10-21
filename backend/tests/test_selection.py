"""Per-call agent selection, the /start preflight, and bot startup failures."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bot
from api import AgentSelectionError, install, select_agent

EXAMPLE = json.loads((Path(__file__).parent.parent / "example_flow.json").read_text())


def _draft(**changes) -> dict:
    agent = copy.deepcopy(EXAMPLE)
    agent.update(changes)
    return agent


def test_no_selector_runs_the_example():
    assert select_agent(None).config.name == EXAMPLE["name"]
    assert select_agent({}).config.name == EXAMPLE["name"]


def test_inline_agent_wins_over_agent_id():
    builder = select_agent({"agent": _draft(name="Draft"), "agent_id": "something"})
    assert builder.config.name == "Draft"


@pytest.mark.parametrize(
    "body",
    [
        {"agent": _draft(initial_node="missing")},  # graph error
        {"agent": {"name": "No nodes"}},  # structural error
        {"agent": "not an object"},
        {"agent_id": "example"},  # saved agents arrive in M2; never fall back silently
        ["not", "an", "object"],
    ],
)
def test_invalid_explicit_selection_raises(body):
    with pytest.raises(AgentSelectionError):
        select_agent(body)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install(app)

    @app.post("/start")  # stands in for the runner's route
    async def start(payload: dict):
        return {"sessionId": "s1", "echo": payload}

    return TestClient(app)


def test_start_preflight_passes_valid_selection_through(client):
    payload = {"body": {"agent": _draft(name="Draft")}}
    response = client.post("/start", json=payload)

    assert response.status_code == 200
    assert response.json()["echo"] == payload  # body still readable downstream


def test_start_preflight_rejects_invalid_agent(client):
    response = client.post("/start", json={"body": {"agent": _draft(initial_node="missing")}})

    assert response.status_code == 422
    assert "initial_node" in response.json()["detail"]


def test_get_example_agent(client):
    assert client.get("/api/agents/example").json() == EXAMPLE


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
