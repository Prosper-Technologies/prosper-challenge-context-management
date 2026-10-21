"""AgentBuilder: run-time validation and compilation into Pipecat Flows nodes."""

import copy

import pytest

from agent_builder import AgentBuilder, AgentValidationError, parse
from tests.helpers import EXAMPLE, draft

END = {"type": "end_conversation"}


def end_node(**fields) -> dict:
    """A two-node agent whose second node is terminal, with `fields` set on it."""
    return {
        "name": "Test",
        "persona": "Be brief.",
        "initial_node": "a",
        "nodes": [
            {"name": "a", "task_messages": [{"role": "developer", "content": "Go."}],
             "edges": [{"function": "go", "description": "", "target": "b"}]},
            {"name": "b", "task_messages": [{"role": "developer", "content": "Bye."}],
             "end": True, **fields},
        ],
    }


def compile_node(doc: dict, name: str):
    builder = AgentBuilder.from_dict(doc)
    return builder._make_node(builder._nodes_by_name[name])


def test_checked_in_example_compiles():
    node = AgentBuilder.from_dict(copy.deepcopy(EXAMPLE)).build_initial_node()
    assert node["name"] == EXAMPLE["initial_node"]
    assert [f.name for f in node["functions"]] == ["choose_intent"]


def test_graph_errors_block_the_runtime():
    with pytest.raises(AgentValidationError) as e:
        AgentBuilder.from_dict(draft(initial_node="missing"))
    assert [i.path for i in e.value.issues] == [["initial_node"]]
    assert "initial_node" in str(e.value)


def test_structural_errors_block_the_runtime():
    with pytest.raises(AgentValidationError):
        AgentBuilder.from_dict({"name": "No nodes"})


def test_warnings_do_not_block_the_runtime():
    doc = draft()
    doc["nodes"][-1]["task_messages"] = []
    AgentBuilder.from_dict(doc)


def test_end_node_without_post_actions_ends_the_call():
    assert compile_node(end_node(), "b")["post_actions"] == [END]


def test_end_node_keeps_its_post_actions_and_then_ends():
    say = {"type": "tts_say", "text": "Goodbye"}
    assert compile_node(end_node(post_actions=[say]), "b")["post_actions"] == [say, END]


def test_end_node_with_its_own_termination_is_not_duplicated():
    actions = [{"type": "end_conversation", "text": "Bye"}, {"type": "tts_say", "text": "x"}]
    assert compile_node(end_node(post_actions=actions), "b")["post_actions"] == actions


def test_non_end_node_gets_no_termination():
    say = {"type": "tts_say", "text": "Moving on"}
    doc = end_node()
    doc["nodes"][0]["post_actions"] = [say]
    assert compile_node(doc, "a")["post_actions"] == [say]
    assert "post_actions" not in compile_node(end_node(), "a")


def test_repeated_compilation_does_not_mutate_or_duplicate():
    say = {"type": "tts_say", "text": "Goodbye"}
    doc = end_node(post_actions=[say], pre_actions=[{"type": "tts_say", "text": "Hi"}])
    snapshot = copy.deepcopy(doc)
    builder = AgentBuilder.from_dict(doc)
    config_before = builder.config.to_dict()

    first = builder._make_node(builder._nodes_by_name["b"])
    first["post_actions"].append({"type": "tts_say", "text": "mutated by Pipecat"})
    first["task_messages"][0]["content"] = "mutated"
    second = builder._make_node(builder._nodes_by_name["b"])

    assert second["post_actions"] == [say, END]
    assert second["task_messages"] == [{"role": "developer", "content": "Bye."}]
    assert builder.config.to_dict() == config_before
    assert doc == snapshot


def test_messages_properties_and_actions_pass_through_verbatim():
    doc = end_node(pre_actions=[{"type": "tts_say", "text": "Hi", "append_text_to_context": False}])
    doc["nodes"][0]["task_messages"][0]["name"] = "coach"
    doc["nodes"][0]["edges"][0]["properties"] = {
        "age": {"type": "integer", "minimum": 0},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    doc["nodes"][0]["edges"][0]["required"] = ["age"]

    a = compile_node(doc, "a")
    assert a["task_messages"] == [{"role": "developer", "content": "Go.", "name": "coach"}]
    schema = a["functions"][0]
    assert schema.properties == doc["nodes"][0]["edges"][0]["properties"]
    assert schema.required == ["age"]
    assert compile_node(doc, "b")["pre_actions"] == doc["nodes"][1]["pre_actions"]


def test_ui_positions_are_ignored_by_the_compiler():
    node = compile_node(end_node(ui={"x": 1, "y": 2}), "b")
    assert node.keys() == compile_node(end_node(), "b").keys()
    assert "ui" not in node


def test_builder_accepts_a_parsed_config():
    config, _ = parse(end_node())
    assert AgentBuilder(config).build_initial_node()["name"] == "a"
