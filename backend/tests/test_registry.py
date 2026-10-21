"""Node tools and edge guards: registry-aware validation, compilation, and the
checked-in scheduler agent."""

import copy
import subprocess
import sys
import json
from pathlib import Path

import pytest

from agent_builder import AgentBuilder, AgentValidationError, GuardBinding, Registry, ToolBinding, validate
from api import select_agent
from catalog_voice import STATE_KEYS, default_registry
from catalog.tools import GUARDS, TOOLS

SCHEDULER = json.loads((Path(__file__).parent.parent / "agents" / "scheduler.json").read_text())


def fake_registry() -> Registry:
    def lookup(args, state):
        return {"found": args.get("q")}, False

    return Registry(
        tools={
            "lookup": ToolBinding("lookup", "Look something up.", {"q": {"type": "string"}}, ("q",), lookup),
            "book": ToolBinding("book", "Book it.", {}, (), lambda args, state: ({}, True)),
        },
        guards={
            "ready": GuardBinding("ready", "Ready.", lambda state: None),
            "done": GuardBinding("done", "Booked.", lambda state: None, frozenset({"book"})),
        },
        state_keys=frozenset({"booking"}),
    )


def agent(nodes) -> dict:
    return {"name": "T", "persona": "Be brief.", "initial_node": nodes[0]["name"], "nodes": nodes}


def node(name, edges=(), tools=None, end=False) -> dict:
    out = {"name": name, "task_messages": [{"role": "developer", "content": "Go."}], "edges": list(edges)}
    if tools is not None:
        out["tools"] = tools
    if end:
        out["end"] = True
    return out


def edge(function, target, guard=None, **fields) -> dict:
    out = {"function": function, "description": "", "target": target, **fields}
    if guard is not None:
        out["guard"] = guard
    return out


def errors(doc, registry=None) -> list[tuple[list, str]]:
    issues = validate(doc, registry or fake_registry())
    return [(i.path, i.message) for i in issues if i.level == "error"]


def two_nodes(tools=None, guard=None) -> dict:
    return agent([node("a", [edge("go", "b", guard)], tools), node("b", end=True)])


# ---- validation -------------------------------------------------------------------------


def test_agents_without_tools_or_guards_are_unaffected():
    doc = two_nodes()
    assert validate(doc, fake_registry()) == validate(doc)


def test_known_tools_and_guards_validate():
    assert errors(two_nodes(tools=["lookup"], guard="ready")) == []


def test_unknown_duplicate_and_clashing_tools_are_errors():
    doc = two_nodes(tools=["lookup", "nope", "lookup"], guard="done")
    doc["nodes"][0]["edges"].append(edge("book", "b", "done"))
    doc["nodes"][0]["tools"].append("book")
    assert errors(doc) == [
        (["nodes", 0, "tools", 1], "Unknown tool 'nope'."),
        (["nodes", 0, "tools", 2], "Tool 'lookup' is listed twice."),
        (["nodes", 0, "tools", 3], "Tool 'book' has the same name as an edge function."),
    ]


def test_unknown_guard_is_an_error():
    assert errors(two_nodes(guard="nope")) == [(["nodes", 0, "edges", 0, "guard"], "Unknown guard 'nope'.")]


def test_without_a_registry_tools_and_guards_are_unknown():
    issues = validate(two_nodes(tools=["lookup"], guard="ready"))
    assert [i.message for i in issues if i.level == "error"] == ["Unknown guard 'ready'.", "Unknown tool 'lookup'."]


def test_an_agent_that_books_needs_a_guarded_end_node():
    doc = two_nodes(tools=["book"])
    assert errors(doc) == [(["nodes"], "Nodes use book, so an end node must be entered through the 'done' guard.")]
    assert errors(two_nodes(tools=["book"], guard="done")) == []


def test_every_edge_into_the_guarded_end_node_needs_the_guard():
    doc = agent(
        [
            node("a", [edge("go", "b"), edge("finish", "c", "done")], ["book"]),
            node("b", [edge("skip", "c")]),
            node("c", end=True),
        ]
    )
    assert errors(doc) == [
        (["nodes", 1, "edges", 0, "guard"], "Edges into 'c' need the 'done' guard; this one skips it.")
    ]


def test_edge_properties_cant_collect_tool_state():
    doc = two_nodes()
    doc["nodes"][0]["edges"][0]["properties"] = {"booking": {"type": "string"}}
    issues = validate(doc, fake_registry())
    assert [(i.level, i.path) for i in issues] == [("warning", ["nodes", 0, "edges", 0, "properties", "booking"])]


# ---- compilation ----------------------------------------------------------------------


def test_node_tools_compile_beside_edges():
    builder = AgentBuilder.from_dict(two_nodes(tools=["lookup"]), fake_registry())
    functions = builder.build_initial_node()["functions"]
    assert [f.name for f in functions] == ["lookup", "go"]
    assert functions[0].properties == {"q": {"type": "string"}}
    assert functions[0].required == ["q"]


def test_compiled_tool_schemas_are_copies():
    registry = fake_registry()
    builder = AgentBuilder.from_dict(two_nodes(tools=["lookup"]), registry)
    builder.build_initial_node()["functions"][0].properties["q"]["type"] = "mutated"
    assert registry.tools["lookup"].properties == {"q": {"type": "string"}}


def test_invalid_tools_block_the_runtime():
    with pytest.raises(AgentValidationError):
        AgentBuilder.from_dict(two_nodes(tools=["nope"]), fake_registry())


# ---- the catalog registry and the scheduler agent ----------------------------------------


def test_catalog_registry_matches_the_tool_definitions():
    registry = default_registry()
    assert set(registry.tools) == set(TOOLS) and set(registry.guards) == set(GUARDS)
    assert registry.state_keys == STATE_KEYS == {"patient", "booking"}
    for name, binding in registry.tools.items():
        schema = TOOLS[name].parameters()
        assert binding.properties == schema["properties"]
        assert list(binding.required) == schema["required"]
    assert registry.guards["booking_completed"].completes == {"book_appointment"}


def test_scheduler_validates_without_issues():
    assert validate(copy.deepcopy(SCHEDULER), default_registry()) == []


def test_scheduler_success_is_only_entered_after_booking():
    edges_in = [(n["name"], e) for n in SCHEDULER["nodes"] for e in n.get("edges", []) if e["target"] == "success"]
    assert edges_in and all(e.get("guard") == "booking_completed" for _, e in edges_in)
    (confirm,) = [n for n in SCHEDULER["nodes"] if n["name"] == "confirm"]
    assert not confirm.get("end") and "book_appointment" in confirm["tools"]

    doc = copy.deepcopy(SCHEDULER)
    for n in doc["nodes"]:
        for e in n.get("edges", []):
            if e["target"] == "success":
                del e["guard"]
    assert errors(doc, default_registry())


def test_every_scheduler_step_can_take_corrections():
    for n in SCHEDULER["nodes"]:
        if n["name"] != "greeting" and not n.get("end"):
            assert "update_booking" in n["tools"], n["name"]
            assert any(e["target"] == "handoff" for e in n["edges"]), n["name"]


def test_saved_scheduler_runs_through_call_selection(store, tmp_path):
    (store.root / "scheduler.json").write_text(json.dumps(SCHEDULER))
    builder = select_agent({"agent_id": "scheduler"}, store)
    assert builder.config.name == "Clinic Scheduler"
    assert select_agent({"agent": SCHEDULER}, store).config.initial_node == "greeting"


def test_building_the_registry_and_contract_creates_no_calendar():
    script = (
        "import contract, catalog_voice; from catalog.availability import voice_calendar; "
        "contract.json_schema(); catalog_voice.default_registry(); print(voice_calendar.cache_info().currsize)"
    )
    out = subprocess.run([sys.executable, "-W", "ignore", "-c", script], cwd=Path(__file__).parent.parent,
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert out.stdout.strip() == "0"
