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

import json
from pathlib import Path
from typing import Union

from loguru import logger
from pipecat_flows import FlowManager, FlowsFunctionSchema, NodeConfig

from .schema import AgentConfig, Edge, Node
from .validation import AgentValidationError, has_errors, parse, validate


class AgentBuilder:
    """Builds a runnable Pipecat Flows graph from a declarative AgentConfig.

    Raises AgentValidationError if the agent can't run (any validation error).
    Warnings are logged, not raised.
    """

    def __init__(self, config: AgentConfig):
        issues = validate(config)
        if has_errors(issues):
            raise AgentValidationError(issues)
        for issue in issues:
            logger.warning(f"Agent '{config.name}': {issue.message} (at {issue.path})")
        self.config = config
        self._nodes_by_name = {n.name: n for n in config.nodes}

    # ---- loading -----------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "AgentBuilder":
        config, issues = parse(data)
        if config is None:
            raise AgentValidationError(issues)
        return cls(config)

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "AgentBuilder":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

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
            "functions": [self._make_edge_function(edge) for edge in node.edges],
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

    def _make_edge_function(self, edge: Edge) -> FlowsFunctionSchema:
        async def handler(args: dict, flow_manager: FlowManager):
            # Persist what the caller gave us so later nodes can use it.
            flow_manager.state.update(args)
            logger.info(f"[{edge.function}] -> {edge.target} | collected: {args}")
            next_node = self._make_node(self._nodes_by_name[edge.target])
            return {"status": "success", **args}, next_node

        return FlowsFunctionSchema(
            name=edge.function,
            description=edge.description,
            properties={k: p.to_dict() for k, p in edge.properties.items()},
            required=list(edge.required),
            handler=handler,
        )
