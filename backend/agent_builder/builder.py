#
# AgentBuilder — loads a declarative agent (JSON / dict) and compiles its node
# graph into Pipecat Flows objects.
#
#   JSON  ->  AgentConfig (parsed + validated)  ->  Pipecat Flows NodeConfig graph
#
# This is the seam between "agent as data" (what the Phase 2 Copilot produces)
# and "agent as a running conversation" (what bot.py executes). Keeping the
# compile + validation here means bot.py never touches the graph internals.
#
# Node tools and edge guards come from an injected Registry. A tool runs in place
# (its handler returns `(result, None)`, so the node stays active); a guarded edge
# checks its guard before it touches state or transitions, and a failed check
# returns recovery information to the LLM instead.
#

import copy
import json
from pathlib import Path
from typing import Union

from loguru import logger
from pipecat_flows import FlowManager, FlowsFunctionSchema, NodeConfig

from .registry import EMPTY_REGISTRY, Registry, ToolBinding
from .schema import AgentConfig, Edge, Node
from .validation import AgentValidationError, has_errors, parse, validate


class AgentBuilder:
    """Builds a runnable Pipecat Flows graph from a declarative AgentConfig.

    Raises AgentValidationError if the agent can't run (any validation error).
    Warnings are logged, not raised.
    """

    def __init__(self, config: AgentConfig, registry: Registry = EMPTY_REGISTRY):
        issues = validate(config, registry)
        if has_errors(issues):
            raise AgentValidationError(issues)
        for issue in issues:
            logger.warning(f"Agent '{config.name}': {issue.message} (at {issue.path})")
        self.config = config
        self.registry = registry
        self._nodes_by_name = {n.name: n for n in config.nodes}

    # ---- loading -----------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict, registry: Registry = EMPTY_REGISTRY) -> "AgentBuilder":
        config, issues = parse(data)
        if config is None:
            raise AgentValidationError(issues)
        return cls(config, registry)

    @classmethod
    def from_json(cls, path: Union[str, Path], registry: Registry = EMPTY_REGISTRY) -> "AgentBuilder":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data, registry)

    # ---- compilation -------------------------------------------------------
    def build_initial_node(self) -> NodeConfig:
        """Return the entry NodeConfig; downstream nodes are built lazily on transition."""
        return self._make_node(self._nodes_by_name[self.config.initial_node])

    def _make_node(self, node: Node) -> NodeConfig:
        # Everything handed to Pipecat is a fresh dict, so compiling never mutates
        # the config and repeated compilation gives the same result.
        node_config: NodeConfig = {
            "name": node.name,
            "role_message": node.role_message or self.config.persona,
            "task_messages": [m.to_dict() for m in node.task_messages],
            "functions": [self._make_tool_function(self.registry.tools[t]) for t in node.tools]
            + [self._make_edge_function(edge) for edge in node.edges],
        }
        if node.pre_actions:
            node_config["pre_actions"] = [a.to_dict() for a in node.pre_actions]
        post_actions = [a.to_dict() for a in node.post_actions]
        # A terminal node ends the call after its own post-actions, unless one of
        # them already does.
        if node.end and not any(a["type"] == "end_conversation" for a in post_actions):
            post_actions.append({"type": "end_conversation"})
        if post_actions:
            node_config["post_actions"] = post_actions
        return node_config

    def _make_tool_function(self, tool: ToolBinding) -> FlowsFunctionSchema:
        async def handler(args: dict, flow_manager: FlowManager):
            result, changed = tool.run(args, flow_manager.state)
            logger.info(f"[{tool.name}] {args} -> {'state changed' if changed else 'no change'}")
            if changed:
                await _report_state(flow_manager, tool.name)
            return result, None  # no transition: the node stays active

        return FlowsFunctionSchema(
            name=tool.name,
            description=tool.description,
            properties=copy.deepcopy(tool.properties),
            required=list(tool.required),
            handler=handler,
        )

    def _make_edge_function(self, edge: Edge) -> FlowsFunctionSchema:
        guard = self.registry.guards[edge.guard] if edge.guard else None

        async def handler(args: dict, flow_manager: FlowManager):
            if guard is not None:
                failure = guard.check(flow_manager.state)
                if failure is not None:
                    logger.info(f"[{edge.function}] blocked by guard {guard.name}: {failure.get('message')}")
                    return {"status": "error", **failure}, None
            # Persist what the caller gave us so later nodes can use it, except the
            # state the tools own.
            collected = {k: v for k, v in args.items() if k not in self.registry.state_keys}
            flow_manager.state.update(collected)
            logger.info(f"[{edge.function}] -> {edge.target} | collected: {collected}")
            next_node = self._make_node(self._nodes_by_name[edge.target])
            return {"status": "success", **collected}, next_node

        return FlowsFunctionSchema(
            name=edge.function,
            description=edge.description,
            properties={k: p.to_dict() for k, p in edge.properties.items()},
            required=list(edge.required),
            handler=handler,
        )


async def _report_state(flow_manager: FlowManager, via: str) -> None:
    """Tell the runtime adapter that a tool changed the state without a transition
    (TrackingFlowManager sends it to the client; a plain FlowManager has no hook)."""
    report = getattr(flow_manager, "report_state", None)
    if report is not None:
        await report(via)
