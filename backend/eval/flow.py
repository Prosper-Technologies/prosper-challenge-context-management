#
# TextFlow — runs a compiled agent graph as text, for the evaluation.
#
# It drives the same NodeConfigs and FlowsFunctionSchema handlers AgentBuilder
# compiles for a voice call, so tools, guards and transitions behave as on a call.
# Only the Pipecat pipeline is replaced, and its context handling is reproduced:
#
#   role_message    the system prompt, replaced by each node that sets one
#   task_messages   appended to the context when a node is entered (Pipecat Flows'
#                   default APPEND strategy: nothing earlier is removed)
#   functions       the tools the LLM may call while the node is active
#
# A handler returning a next node transitions after the tool round's results are in
# the context. A node with an `end_conversation` post-action ends the call once the
# LLM has spoken in it.
#

import json
from typing import Any, Optional

from agent_builder import AgentBuilder


class TextFlow:
    def __init__(self, builder: AgentBuilder):
        self.builder = builder
        self.state: dict = {}  # the flow state handlers read and write, as on a call
        self.messages: list[dict] = []  # the context, without the system prompt
        self.system = ""
        self.node: Optional[dict] = None
        self.functions: dict[str, Any] = {}
        self.path: list[str] = []
        self.state_updates: list[str] = []  # tools that changed state without a transition
        self.ending = False  # in a node that ends the call after its reply
        self.ended = False

    def start(self) -> None:
        self.enter(self.builder.build_initial_node())

    def enter(self, node: dict) -> None:
        self.node = node
        if node.get("role_message"):
            self.system = node["role_message"]
        self.messages.extend(node["task_messages"])
        self.functions = {f.name: f for f in node["functions"]}
        self.path.append(node["name"])
        self.ending = any(a.get("type") == "end_conversation" for a in node.get("post_actions", []))

    def prompt(self) -> list[dict]:
        return [{"role": "system", "content": self.system}, *self.messages]

    def tools(self) -> list[dict]:
        """The active node's functions in OpenAI's tool format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": f.name,
                    "description": f.description,
                    "parameters": {"type": "object", "properties": f.properties, "required": f.required},
                },
            }
            for f in self.functions.values()
        ]

    async def call(self, name: str, arguments: str) -> tuple[dict, Optional[dict]]:
        """Run one function call; returns its result and the node it proposes, if any."""
        function = self.functions.get(name)
        if function is None:
            return {"status": "error", "message": f"There is no function {name!r} in this step."}, None
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {"status": "error", "message": "The arguments aren't valid JSON."}, None
        if not isinstance(args, dict):
            return {"status": "error", "message": "The arguments must be a JSON object."}, None
        return await function.handler(args, self)

    async def report_state(self, via: str) -> None:
        """Called by the builder's tool handlers after a state change (as on a call)."""
        self.state_updates.append(via)
