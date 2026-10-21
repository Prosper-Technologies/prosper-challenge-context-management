"""The agents API: CRUD, ids, atomic writes and draft-vs-run validity."""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import AgentSelectionError, install, select_agent
from tests.helpers import EXAMPLE, EXAMPLE_PATH, draft


@pytest.fixture
def client(store) -> TestClient:
    app = FastAPI()
    install(app, store)
    return TestClient(app)


def test_list_agents(client, store):
    (store.root / "garbled.json").write_text("{not json")
    (store.root / "notes.txt").write_text("ignored")

    assert client.get("/api/agents").json() == [
        {"id": "example", "name": EXAMPLE["name"]},
        {"id": "garbled", "name": None},
    ]


def test_example_round_trips(client, store):
    original = client.get("/api/agents/example").json()
    assert original == EXAMPLE

    response = client.put("/api/agents/example", json=original)
    assert response.status_code == 200
    assert response.json() == {"issues": [], "malformed": False}
    assert client.get("/api/agents/example").json() == EXAMPLE
    # The checked-in file is already in the store's format: saving changes nothing.
    assert (store.root / "example.json").read_text() == EXAMPLE_PATH.read_text()


def test_put_creates_then_replaces(client):
    assert client.get("/api/agents/new").status_code == 404

    assert client.put("/api/agents/new", json=draft(name="One")).status_code == 200
    assert client.get("/api/agents/new").json()["name"] == "One"

    assert client.put("/api/agents/new", json=draft(name="Two")).status_code == 200
    assert client.get("/api/agents/new").json()["name"] == "Two"
    assert [a["id"] for a in client.get("/api/agents").json()] == ["example", "new"]


def test_put_preserves_unknown_fields_and_positions(client):
    doc = draft(x_owner="team")
    doc["nodes"][0]["ui"] = {"x": 120.0, "y": -40.5}
    doc["nodes"][0]["edges"][0]["properties"]["intent"]["x_hint"] = "kept"

    issues = client.put("/api/agents/extra", json=doc).json()["issues"]
    assert client.get("/api/agents/extra").json() == doc
    assert [i["path"] for i in issues] == [["x_owner"]]  # kept, but flagged as unused


def test_draft_with_graph_errors_saves_but_cannot_run(client, store):
    doc = draft(initial_node="missing")

    response = client.put("/api/agents/wip", json=doc)
    assert response.status_code == 200
    assert response.json() == {
        "issues": [
            {
                "level": "error",
                "path": ["initial_node"],
                "node": None,
                "edge": None,
                "message": "Initial node 'missing' is not a defined node.",
            }
        ],
        "malformed": False,
    }
    assert client.get("/api/agents/wip").json() == doc

    with pytest.raises(AgentSelectionError):
        select_agent({"agent_id": "wip"}, store)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "No nodes", "initial_node": "a"},
        draft(nodes="not a list"),
        ["not", "an", "object"],
    ],
)
def test_malformed_document_is_rejected_and_leaves_the_file_intact(client, store, body):
    before = (store.root / "example.json").read_text()

    response = client.put("/api/agents/example", json=body)
    assert response.status_code == 422
    assert response.json()["malformed"] is True
    issues = response.json()["issues"]
    assert issues and all(i["level"] == "error" for i in issues)
    assert (store.root / "example.json").read_text() == before


def test_invalid_json_body_is_rejected(client, store):
    before = (store.root / "example.json").read_text()
    response = client.put(
        "/api/agents/example", content="{nope", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert (store.root / "example.json").read_text() == before


def test_malformed_document_does_not_create_a_file(client, store):
    assert client.put("/api/agents/fresh", json={"name": "x"}).status_code == 422
    assert not (store.root / "fresh.json").exists()


def test_failed_write_keeps_the_previous_file(client, store, monkeypatch):
    before = (store.root / "example.json").read_text()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        client.put("/api/agents/example", json=draft(name="Changed"))

    assert (store.root / "example.json").read_text() == before
    assert sorted(p.name for p in store.root.iterdir()) == ["example.json"]  # no temp left


@pytest.mark.parametrize("agent_id", ["Example", "has space", "dot.json", "a..b", "a" * 65])
def test_bad_ids_are_rejected(client, store, agent_id):
    assert client.get(f"/api/agents/{agent_id}").status_code == 400
    assert client.put(f"/api/agents/{agent_id}", json=EXAMPLE).status_code == 400
    assert client.delete(f"/api/agents/{agent_id}").status_code == 400
    assert sorted(p.name for p in store.root.iterdir()) == ["example.json"]


def test_path_traversal_never_reaches_the_store(client, store):
    outside = store.root.parent / "outside.json"
    for path in ["/api/agents/..%2Foutside", "/api/agents/%2E%2E%2Foutside"]:
        assert client.put(path, json=EXAMPLE).status_code in (400, 404, 405)
    assert not outside.exists()


def test_delete(client, store):
    client.put("/api/agents/gone", json=EXAMPLE)

    assert client.delete("/api/agents/gone").status_code == 204
    assert client.get("/api/agents/gone").status_code == 404
    assert client.delete("/api/agents/gone").status_code == 404
    assert not (store.root / "gone.json").exists()


def test_get_unreadable_saved_agent(client, store):
    (store.root / "garbled.json").write_text("{not json")
    assert client.get("/api/agents/garbled").status_code == 500


def test_validate_reports_graph_issues_without_saving(client, store):
    response = client.post("/api/agents/validate", json=draft(initial_node="missing"))

    assert response.status_code == 200
    assert [i["path"] for i in response.json()["issues"]] == [["initial_node"]]
    assert response.json()["malformed"] is False
    assert sorted(p.name for p in store.root.iterdir()) == ["example.json"]


def test_validate_reports_structural_issues(client):
    response = client.post("/api/agents/validate", json={"name": "x", "initial_node": "a"})

    assert response.status_code == 200
    assert response.json()["malformed"] is True
    assert response.json()["issues"][0]["path"] == ["nodes"]


def test_validate_valid_agent(client):
    assert client.post("/api/agents/validate", json=EXAMPLE).json() == {"issues": [], "malformed": False}


def test_saved_file_is_formatted_json(client, store):
    client.put("/api/agents/pretty", json=draft(name="Café"))
    text = (store.root / "pretty.json").read_text()

    assert text.endswith("}\n") and '\n  "name": "Café"' in text
    assert json.loads(text)["name"] == "Café"


def test_validate_resolves_catalog_tools_and_guards(client):
    scheduler = json.loads((Path(__file__).parent.parent / "agents" / "scheduler.json").read_text())
    assert client.post("/api/agents/validate", json=scheduler).json() == {"issues": [], "malformed": False}
    scheduler["nodes"][1]["tools"].append("teleport")
    issues = client.post("/api/agents/validate", json=scheduler).json()["issues"]
    assert [i["message"] for i in issues] == ["Unknown tool 'teleport'."]
