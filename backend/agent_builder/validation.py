#
# Validation — two separate questions about an agent document:
#
#   parse(data)       is it structurally an agent?   (shape; else it can't be saved)
#   validate(config)  can its graph actually run?    (errors block calls, not saves)
#
# Both report problems as `Issue`s, so the UI shows one list. An issue's `path`
# addresses the offending value by array index and field (e.g. ["nodes", 2,
# "edges", 0, "target"]), which stays unambiguous even when names are duplicated;
# `node` / `edge` name the node and edge function where they're known.
#

import re
from collections import Counter
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, ValidationError

from .registry import EMPTY_REGISTRY, Registry
from .schema import (
    SUPPORTED_ACTIONS,
    AgentConfig,
    EndConversationAction,
    TtsSayAction,
)

JSON_SCHEMA_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}
# What OpenAI accepts as a tool name.
FUNCTION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

PathPart = Union[str, int]


class Issue(BaseModel):
    """One problem with an agent document."""

    # Always serialized, so the generated type marks every field present.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    level: Literal["error", "warning"]
    path: list[PathPart]
    node: Optional[str] = None
    edge: Optional[str] = None  # the edge's function name
    message: str


class AgentValidationError(ValueError):
    """The agent can't run: it is malformed or its graph has errors."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        errors = [i for i in issues if i.level == "error"]
        super().__init__("; ".join(_describe(i) for i in errors) or "Invalid agent")


def has_errors(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)


def _describe(issue: Issue) -> str:
    where = ".".join(str(p) for p in issue.path)
    return f"{where}: {issue.message}" if where else issue.message


# ---- structural parsing --------------------------------------------------------


def parse(data: Any) -> tuple[Optional[AgentConfig], list[Issue]]:
    """Parse a JSON document into an AgentConfig, or report why its shape is wrong."""
    try:
        return AgentConfig.model_validate(data), []
    except ValidationError as e:
        return None, _structural_issues(data, e)


_MESSAGES = {
    "model_type": "Expected an object",
    "model_attributes_type": "Expected an object",
    "dict_type": "Expected an object",
    "list_type": "Expected an array",
    "string_type": "Expected a string",
    "bool_type": "Expected a boolean",
    "float_type": "Expected a number",
}


def _structural_issues(data: Any, error: ValidationError) -> list[Issue]:
    by_path: dict[tuple, list[str]] = {}
    for err in error.errors():
        path = tuple(_data_path(data, err["loc"]))
        message = _MESSAGES.get(err["type"], err["msg"])
        messages = by_path.setdefault(path, [])
        if message not in messages:
            messages.append(message)
    return [
        Issue(level="error", path=list(path), message=_join(messages), **_context(data, path))
        for path, messages in by_path.items()
    ]


def _join(messages: list[str]) -> str:
    """One message for a union's alternatives: "Expected a string or an array"."""
    prefix = "Expected "
    if len(messages) > 1 and all(m.startswith(prefix) for m in messages):
        return prefix + " or ".join(m[len(prefix) :] for m in messages)
    return " or ".join(messages)


def _data_path(data: Any, loc: tuple) -> list[PathPart]:
    """Map a pydantic error location onto the document, dropping union tags
    (e.g. the "other" in ("nodes", 0, "pre_actions", 1, "other", "type"))."""
    path: list[PathPart] = []
    current = data
    for i, part in enumerate(loc):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            current = current[part]
        elif i == len(loc) - 1 and isinstance(current, dict):
            pass  # a missing field: keep its name
        else:
            continue
        path.append(part)
    return path


def _context(data: Any, path: tuple) -> dict:
    """Name the node and edge a raw-document path points into, where they're known."""
    context: dict = {}
    if len(path) < 2 or path[0] != "nodes" or not isinstance(data, dict):
        return context
    node = _index(data.get("nodes"), path[1])
    if isinstance(node, dict) and isinstance(node.get("name"), str):
        context["node"] = node["name"]
    if len(path) >= 4 and path[2] == "edges" and isinstance(node, dict):
        edge = _index(node.get("edges"), path[3])
        if isinstance(edge, dict) and isinstance(edge.get("function"), str):
            context["edge"] = edge["function"]
    return context


def _index(items: Any, i: PathPart) -> Any:
    if isinstance(items, list) and isinstance(i, int) and 0 <= i < len(items):
        return items[i]
    return None


# ---- graph / runtime validation ------------------------------------------------


def validate(config: Union[AgentConfig, Any], registry: Registry = EMPTY_REGISTRY) -> list[Issue]:
    """Every issue with an agent: structural ones if it doesn't parse, else graph ones.

    Node tools and edge guards must be names in `registry`. Pure: the document is
    not modified. Errors mean the agent can't run; warnings point at likely
    mistakes but don't block a call.
    """
    if not isinstance(config, AgentConfig):
        config, issues = parse(config)
        if config is None:
            return issues
    return _GraphCheck(config, registry).run()


class _GraphCheck:
    def __init__(self, config: AgentConfig, registry: Registry):
        self.config = config
        self.registry = registry
        self.issues: list[Issue] = []

    def add(self, level, path, message, node=None, edge=None) -> None:
        self.issues.append(Issue(level=level, path=path, node=node, edge=edge, message=message))

    def run(self) -> list[Issue]:
        config = self.config
        names = Counter(n.name for n in config.nodes)
        self._extra_fields(config, [])

        if not config.nodes:
            self.add("error", ["nodes"], "The agent has no nodes.")
        if config.initial_node not in names:
            self.add(
                "error",
                ["initial_node"],
                f"Initial node '{config.initial_node}' is not a defined node.",
            )

        for i, node in enumerate(config.nodes):
            self._node(i, node, names)

        self._completion_guards()

        if config.initial_node in names:
            reachable = self._reachable(config.initial_node)
            for i, node in enumerate(config.nodes):
                if node.name not in reachable:
                    self.add(
                        "warning",
                        ["nodes", i],
                        "Node is unreachable from the initial node.",
                        node=node.name,
                    )
        return self.issues

    def _node(self, i: int, node, names: Counter) -> None:
        path: list[PathPart] = ["nodes", i]
        name = node.name
        self._extra_fields(node, path, node=name)
        if node.ui is not None:
            self._extra_fields(node.ui, path + ["ui"], node=name)

        if not name.strip():
            self.add("error", path + ["name"], "Node name is empty.", node=name)
        elif names[name] > 1:
            self.add("error", path + ["name"], f"Duplicate node name '{name}'.", node=name)

        if not node.task_messages:
            self.add("warning", path + ["task_messages"], "Node has no task messages.", node=name)
        if not node.role_message and not self.config.persona:
            self.add(
                "warning",
                path + ["role_message"],
                "Node has no role message and the agent has no persona.",
                node=name,
            )
        if node.end and node.edges:
            self.add(
                "warning",
                path + ["edges"],
                "End node has edges; the call ends after this node.",
                node=name,
            )
        if not node.end and not node.edges:
            self.add(
                "warning",
                path,
                "Node has no edges and isn't an end node, so the call gets stuck here.",
                node=name,
            )

        functions = Counter(e.function for e in node.edges)
        for j, edge in enumerate(node.edges):
            self._edge(path + ["edges", j], node, edge, names, functions)
        self._tools(path, node, functions)

        for field in ("pre_actions", "post_actions"):
            for k, action in enumerate(getattr(node, field)):
                self._action(path + [field, k], action, node=name)

    def _edge(self, path, node, edge, names: Counter, functions: Counter) -> None:
        fn = edge.function
        context = {"node": node.name, "edge": fn}
        self._extra_fields(edge, path, **context)

        if not FUNCTION_NAME.match(fn):
            self.add(
                "error",
                path + ["function"],
                f"Invalid function name '{fn}': use 1-64 letters, digits, '_' or '-'.",
                **context,
            )
        elif functions[fn] > 1:
            self.add("error", path + ["function"], f"Duplicate function name '{fn}' in this node.", **context)

        if edge.target == node.name:
            self.add("error", path + ["target"], "An edge can't target its own node.", **context)
        elif edge.target not in names:
            self.add("error", path + ["target"], f"Unknown target node '{edge.target}'.", **context)

        if edge.guard is not None and edge.guard not in self.registry.guards:
            self.add("error", path + ["guard"], f"Unknown guard '{edge.guard}'.", **context)

        for prop_name in edge.properties:
            if prop_name in self.registry.state_keys:
                self.add(
                    "warning",
                    path + ["properties", prop_name],
                    f"'{prop_name}' is kept by the tools, so this edge can't collect it.",
                    **context,
                )
        for k, prop in enumerate(edge.required):
            if prop not in edge.properties:
                self.add(
                    "error",
                    path + ["required", k],
                    f"Required property '{prop}' is not defined in properties.",
                    **context,
                )
        for prop_name, prop in edge.properties.items():
            types = prop.type if isinstance(prop.type, list) else [prop.type]
            for t in types:
                if t is not None and t not in JSON_SCHEMA_TYPES:
                    self.add(
                        "error",
                        path + ["properties", prop_name, "type"],
                        f"'{t}' is not a JSON-schema type.",
                        **context,
                    )

    def _tools(self, path, node, functions: Counter) -> None:
        seen: set[str] = set()
        for k, tool in enumerate(node.tools):
            where = path + ["tools", k]
            if tool not in self.registry.tools:
                self.add("error", where, f"Unknown tool '{tool}'.", node=node.name)
            elif tool in seen:
                self.add("error", where, f"Tool '{tool}' is listed twice.", node=node.name)
            elif tool in functions:
                self.add("error", where, f"Tool '{tool}' has the same name as an edge function.", node=node.name)
            seen.add(tool)

    def _completion_guards(self) -> None:
        """An agent that can book must end in success only through the guard that
        checks the booking: no edge into that end node may skip it."""
        nodes = self.config.nodes
        used = {t for n in nodes for t in n.tools}
        ends = {n.name for n in nodes if n.end}
        for guard in self.registry.guards.values():
            tools = sorted(used & guard.completes)
            if not tools:
                continue
            guarded = {e.target for n in nodes for e in n.edges if e.guard == guard.name}
            if not guarded & ends:
                self.add(
                    "error",
                    ["nodes"],
                    f"Nodes use {', '.join(tools)}, so an end node must be entered through the "
                    f"'{guard.name}' guard.",
                )
            for i, node in enumerate(nodes):
                for j, edge in enumerate(node.edges):
                    if edge.target in guarded and edge.guard != guard.name:
                        self.add(
                            "error",
                            ["nodes", i, "edges", j, "guard"],
                            f"Edges into '{edge.target}' need the '{guard.name}' guard; this one skips it.",
                            node=node.name,
                            edge=edge.function,
                        )

    def _action(self, path, action, node: str) -> None:
        if isinstance(action, TtsSayAction):
            if not action.text.strip():
                self.add("warning", path + ["text"], "tts_say has no text to say.", node=node)
            return
        if isinstance(action, EndConversationAction):
            return
        model = SUPPORTED_ACTIONS.get(action.type)
        if model is None:
            supported = ", ".join(SUPPORTED_ACTIONS)
            self.add(
                "error",
                path + ["type"],
                f"Unsupported action type '{action.type}' (supported: {supported}).",
                node=node,
            )
            return
        # A supported type that didn't parse as its model: say what's wrong with it.
        data = action.to_dict()
        try:
            model.model_validate(data)
        except ValidationError as e:
            for issue in _structural_issues(data, e):
                self.add(
                    "error",
                    path + issue.path,
                    f"Malformed {action.type} action: {issue.message}.",
                    node=node,
                )

    def _extra_fields(self, model: BaseModel, path: list[PathPart], **context) -> None:
        for key in model.model_extra or {}:
            self.add(
                "warning",
                path + [key],
                f"Unknown field '{key}' is kept but not used by the runtime.",
                **context,
            )

    def _reachable(self, start: str) -> set[str]:
        edges: dict[str, set[str]] = {}
        for node in self.config.nodes:
            edges.setdefault(node.name, set()).update(e.target for e in node.edges)
        seen, frontier = {start}, [start]
        while frontier:
            for target in edges.get(frontier.pop(), ()):
                if target in edges and target not in seen:
                    seen.add(target)
                    frontier.append(target)
        return seen
