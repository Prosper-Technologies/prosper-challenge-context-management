"""Structural parsing (pydantic models) and graph/runtime validation."""

import pytest

from agent_builder import AgentConfig, parse, validate
from agent_builder.schema import Action, EndConversationAction, TtsSayAction
from tests.helpers import EXAMPLE, draft


def errors(issues):
    return [(i.path, i.message) for i in issues if i.level == "error"]


def warnings(issues):
    return [(i.path, i.message) for i in issues if i.level == "warning"]


def edge(function="go", target="b", **fields) -> dict:
    return {"function": function, "description": "", "target": target, **fields}


def node(name, edges=(), **fields) -> dict:
    return {
        "name": name,
        "task_messages": [{"role": "developer", "content": f"Do {name}."}],
        "edges": list(edges),
        **fields,
    }


def agent(*nodes, initial="a", **fields) -> dict:
    return {"name": "Test", "persona": "Be brief.", "initial_node": initial, "nodes": list(nodes), **fields}


def two_nodes(**a_fields) -> dict:
    """a --go--> b (end)."""
    return agent(node("a", [edge()], **a_fields), node("b", end=True))


# ---- structural parsing -------------------------------------------------------


def test_example_is_valid():
    config, issues = parse(EXAMPLE)
    assert issues == []
    assert validate(config) == []


def test_round_trip_preserves_the_document_and_unknown_fields():
    doc = two_nodes()
    doc["x_owner"] = "team"
    doc["nodes"][0]["task_messages"][0]["name"] = "coach"  # extra message field
    doc["nodes"][0]["task_messages"].append(
        {"role": "developer", "content": [{"type": "text", "text": "parts"}]}
    )
    doc["nodes"][0]["edges"][0]["properties"] = {
        "age": {"type": "integer", "minimum": 0, "description": "Age."},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    doc["nodes"][0]["pre_actions"] = [
        {"type": "tts_say", "text": "Hi", "x_voice": "soft"},
        {"type": "notify_slack", "channel": "#ops"},
        {"type": "tts_say"},  # malformed: still preserved
    ]
    doc["nodes"][0]["ui"] = {"x": 10, "y": 20.5}

    config, issues = parse(doc)
    assert issues == []
    assert config.to_dict() == doc
    assert AgentConfig.model_validate(config.to_dict()).to_dict() == doc


def test_supported_actions_parse_as_their_models():
    config, _ = parse(
        two_nodes(
            pre_actions=[
                {"type": "tts_say", "text": "Hi"},
                {"type": "end_conversation"},
                {"type": "tts_say"},
                {"type": "custom"},
            ]
        )
    )
    kinds = [type(a) for a in config.nodes[0].pre_actions]
    assert kinds == [TtsSayAction, EndConversationAction, Action, Action]


def test_to_dict_does_not_add_defaults():
    config, _ = parse({"name": "Min", "initial_node": "a", "nodes": [{"name": "a"}]})
    assert config.to_dict() == {"name": "Min", "initial_node": "a", "nodes": [{"name": "a"}]}


@pytest.mark.parametrize(
    "doc, path",
    [
        ("not an object", []),
        ({"name": "x", "initial_node": "a"}, ["nodes"]),
        ({"name": "x", "initial_node": "a", "nodes": {}}, ["nodes"]),
        (agent({"task_messages": []}), ["nodes", 0, "name"]),
        (agent(node("a", end="true")), ["nodes", 0, "end"]),  # no coercion
        (agent(node("a", [{"function": "go", "target": "b"}])), ["nodes", 0, "edges", 0, "description"]),
        (agent(node("a", [edge(required="x")])), ["nodes", 0, "edges", 0, "required"]),
        (agent(node("a", task_messages=[{"content": "hi"}])), ["nodes", 0, "task_messages", 0, "role"]),
        (agent(node("a", pre_actions=[{}])), ["nodes", 0, "pre_actions", 0, "type"]),
        (agent(node("a", pre_actions=["tts_say"])), ["nodes", 0, "pre_actions", 0]),
        (agent(node("a", ui={"x": 1})), ["nodes", 0, "ui", "y"]),
        (agent(node("a", [edge(properties={"p": {"type": 5}})])), ["nodes", 0, "edges", 0, "properties", "p", "type"]),
    ],
)
def test_structural_errors_point_at_the_document(doc, path):
    config, issues = parse(doc)

    assert config is None
    assert [i.path for i in issues] == [path]
    assert all(i.level == "error" for i in issues)
    assert validate(doc) == issues  # validate reports structural issues as-is


def test_structural_issue_names_node_and_edge():
    _, issues = parse(agent(node("a", [{"function": "go", "target": "b"}])))
    assert (issues[0].node, issues[0].edge) == ("a", "go")


def test_union_alternatives_are_reported_once():
    _, issues = parse(agent(node("a", [edge(properties={"p": {"type": 5}})])))
    assert issues[0].message == "Expected a string or an array"


# ---- graph errors -------------------------------------------------------------


def test_missing_initial_node():
    assert errors(validate(draft(initial_node="nope"))) == [
        (["initial_node"], "Initial node 'nope' is not a defined node.")
    ]


def test_no_nodes():
    assert [p for p, _ in errors(validate(agent()))] == [["nodes"], ["initial_node"]]


def test_unknown_edge_target():
    issues = validate(agent(node("a", [edge(target="ghost")]), node("b", end=True)))
    assert errors(issues) == [(["nodes", 0, "edges", 0, "target"], "Unknown target node 'ghost'.")]
    assert issues[0].node == "a" and issues[0].edge == "go"


def test_self_loop():
    issues = validate(agent(node("a", [edge(target="a")])))
    assert [p for p, _ in errors(issues)] == [["nodes", 0, "edges", 0, "target"]]


def test_duplicate_node_names_have_distinct_paths():
    doc = agent(node("a", [edge()]), node("b", end=True), node("b", end=True))
    assert [p for p, _ in errors(validate(doc))] == [["nodes", 1, "name"], ["nodes", 2, "name"]]


def test_duplicate_function_names_within_a_node():
    doc = agent(node("a", [edge(), edge(target="c")]), node("b", end=True), node("c", end=True))
    assert [p for p, _ in errors(validate(doc))] == [
        ["nodes", 0, "edges", 0, "function"],
        ["nodes", 0, "edges", 1, "function"],
    ]


def test_same_function_name_in_different_nodes_is_fine():
    doc = agent(node("a", [edge("next", "b")]), node("b", [edge("next", "c")]), node("c", end=True))
    assert errors(validate(doc)) == []


@pytest.mark.parametrize("name", ["", "has space", "dot.name", "x" * 65])
def test_invalid_function_name(name):
    doc = agent(node("a", [edge(name)]), node("b", end=True))
    assert [p for p, _ in errors(validate(doc))] == [["nodes", 0, "edges", 0, "function"]]


def test_required_must_be_defined():
    doc = agent(
        node("a", [edge(properties={"x": {"type": "string"}}, required=["x", "y"])]),
        node("b", end=True),
    )
    assert errors(validate(doc)) == [
        (["nodes", 0, "edges", 0, "required", 1], "Required property 'y' is not defined in properties.")
    ]


def test_property_types_must_be_json_schema_types():
    props = {"a": {"type": "str"}, "b": {"type": ["string", "null"]}, "c": {"type": ["date"]}, "d": {}}
    doc = agent(node("a", [edge(properties=props)]), node("b", end=True))
    assert [p for p, _ in errors(validate(doc))] == [
        ["nodes", 0, "edges", 0, "properties", "a", "type"],
        ["nodes", 0, "edges", 0, "properties", "c", "type"],
    ]


def test_unsupported_and_malformed_actions():
    doc = two_nodes(
        pre_actions=[{"type": "tts_say", "text": "Hi"}, {"type": "notify_slack"}],
        post_actions=[{"type": "tts_say"}, {"type": "end_conversation", "text": 5}],
    )
    assert [p for p, _ in errors(validate(doc))] == [
        ["nodes", 0, "pre_actions", 1, "type"],
        ["nodes", 0, "post_actions", 0, "text"],
        ["nodes", 0, "post_actions", 1, "text"],
    ]


# ---- warnings -----------------------------------------------------------------


def test_unreachable_node():
    doc = agent(node("a", [edge()]), node("b", end=True), node("island", end=True))
    assert warnings(validate(doc)) == [(["nodes", 2], "Node is unreachable from the initial node.")]


def test_dead_end_non_end_node():
    doc = agent(node("a", [edge()]), node("b"))
    assert [p for p, _ in warnings(validate(doc))] == [["nodes", 1]]


def test_end_node_with_edges():
    doc = agent(node("a", [edge()]), node("b", [edge("back", "a")], end=True))
    assert [p for p, _ in warnings(validate(doc))] == [["nodes", 1, "edges"]]


def test_node_without_task_messages():
    doc = agent(node("a", [edge()], task_messages=[]), node("b", end=True))
    assert [p for p, _ in warnings(validate(doc))] == [["nodes", 0, "task_messages"]]


def test_node_without_persona_or_role_message():
    doc = agent(node("a", [edge()]), node("b", end=True, role_message="Be kind."), persona="")
    assert [p for p, _ in warnings(validate(doc))] == [["nodes", 0, "role_message"]]


def test_unknown_fields_are_warned_about():
    doc = two_nodes(prompt="typo of task_messages")
    doc["owner"] = "me"
    assert [p for p, _ in warnings(validate(doc))] == [["owner"], ["nodes", 0, "prompt"]]


def test_warnings_do_not_block():
    doc = agent(node("a", [edge()]), node("b"), node("island", end=True))
    assert errors(validate(doc)) == [] and warnings(validate(doc))


def test_validate_does_not_mutate():
    doc = two_nodes()
    config, _ = parse(doc)
    before = config.to_dict()
    validate(config)
    validate(doc)
    assert config.to_dict() == before == doc


def test_an_empty_first_message_starts_the_call_in_silence():
    doc = two_nodes()
    assert validate({**doc, "first_message": "Hello!"}) == []
    assert warnings(validate({**doc, "first_message": "  "})) == [
        (["first_message"], "The first message is empty, so the call starts in silence.")
    ]
