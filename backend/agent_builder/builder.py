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
# Code takes the transitions it can decide, so the LLM doesn't spend a round trip
# on them:
#
#   Edge.auto_after   after one of these tools changes the state, the edge is taken
#                     if its guard passes; the tool's result says so under
#                     AUTO_TRANSITION, and the LLM's next call is already in the
#                     target node
#   Node.enter_tools  run on entry; their results join the transition's result, so
#                     the node starts with them instead of having to call them
#

import copy
import json
from pathlib import Path
from typing import Optional, Union

from loguru import logger
from pipecat_flows import FlowManager, FlowsFunctionSchema, NodeConfig

from .registry import EMPTY_REGISTRY, Registry, ToolBinding
from .schema import AgentConfig, Edge, Node
from .validation import AgentValidationError, has_errors, parse, validate

# The key under which a tool's result reports the edge it took automatically.
AUTO_TRANSITION = "auto_transition"


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
        """Return the entry NodeConfig; downstream nodes are built lazily on transition.

        With a first message, the call opens by saying it and the LLM's first call
        waits for the caller's reply. Only the call's start gets it: an edge back
        into the initial node enters it as usual."""
        node_config = self._make_node(self._nodes_by_name[self.config.initial_node])
        if self.config.first_message is not None:
            say = {"type": "tts_say", "text": self.config.first_message}
            node_config["pre_actions"] = [say, *node_config.get("pre_actions", [])]
            node_config["respond_immediately"] = False
        return node_config

    def _make_node(self, node: Node) -> NodeConfig:
        # Everything handed to Pipecat is a fresh dict, so compiling never mutates
        # the config and repeated compilation gives the same result.
        node_config: NodeConfig = {
            "name": node.name,
            "role_message": node.role_message or self.config.persona,
            "task_messages": [m.to_dict() for m in node.task_messages],
            "functions": [self._make_tool_function(self.registry.tools[t], node) for t in node.tools]
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

    def _make_tool_function(self, tool: ToolBinding, node: Node) -> FlowsFunctionSchema:
        auto_edges = [e for e in node.edges if tool.name in e.auto_after]

        async def handler(args: dict, flow_manager: FlowManager):
            result, changed = tool.run(args, flow_manager.state)
            logger.info(f"[{tool.name}] {args} -> {'state changed' if changed else 'no change'}")
            if not changed:
                return result, None  # no transition: the node stays active
            await _report_state(flow_manager, tool.name)
            for edge in auto_edges:
                if self._guard_failure(edge, flow_manager.state, {}) is None:
                    logger.info(f"[{tool.name}] takes {edge.function} automatically")
                    taken, next_node = await self._transition(edge, {}, flow_manager)
                    return {**result, AUTO_TRANSITION: {"function": edge.function, **taken}}, next_node
            return result, None

        return FlowsFunctionSchema(
            name=tool.name,
            description=tool.description,
            properties=copy.deepcopy(tool.properties),
            required=list(tool.required),
            handler=handler,
        )

    def _make_edge_function(self, edge: Edge) -> FlowsFunctionSchema:
        async def handler(args: dict, flow_manager: FlowManager):
            failure = self._guard_failure(edge, flow_manager.state, args)
            if failure is not None:
                logger.info(f"[{edge.function}] blocked by guard {edge.guard}: {failure.get('message')}")
                return {"status": "error", **failure}, None
            return await self._transition(edge, args, flow_manager)

        return FlowsFunctionSchema(
            name=edge.function,
            description=edge.description,
            properties={k: p.to_dict() for k, p in edge.properties.items()},
            required=list(edge.required),
            handler=handler,
        )


    def _guard_failure(self, edge: Edge, state: dict, args: dict) -> Optional[dict]:
        if edge.guard is None:
            return None
        return self.registry.guards[edge.guard].check(state, args)

    async def _transition(self, edge: Edge, args: dict, flow_manager: FlowManager) -> tuple[dict, NodeConfig]:
        """Take `edge` (its guard already passed): collect its arguments, run the
        target's entry tools, and return the result and the node to enter."""
        # Persist what the caller gave us so later nodes can use it, except the
        # state the tools own.
        collected = {k: v for k, v in args.items() if k not in self.registry.state_keys}
        flow_manager.state.update(collected)
        logger.info(f"[{edge.function}] -> {edge.target} | collected: {collected}")
        target = self._nodes_by_name[edge.target]
        entered = {}
        for name in target.enter_tools:
            result, changed = self.registry.tools[name].run({}, flow_manager.state)
            entered[name] = result
            if changed:
                await _report_state(flow_manager, name)
        return {"status": "success", **collected, **entered}, self._make_node(target)


async def _report_state(flow_manager: FlowManager, via: str) -> None:
    """Tell the runtime adapter that a tool changed the state without a transition
    (TrackingFlowManager sends it to the client; a plain FlowManager has no hook)."""
    report = getattr(flow_manager, "report_state", None)
    if report is not None:
        await report(via)
