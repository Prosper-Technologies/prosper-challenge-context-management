#
# Agent schema — the declarative contract the Phase 2 Copilot reads and writes.
#
# Design rule: stay as close to Pipecat Flows' own vocabulary as possible. A node
# carries Pipecat's native fields (`role_message`, `task_messages`, `pre/post_actions`)
# verbatim. The ONLY things we add are `edges`: transitions expressed as DATA (a string
# `target`) rather than as Python closures — because a Copilot can emit a string, not
# a callable — and an optional canvas position (`ui`). `AgentBuilder` turns the edge
# strings back into the closures Pipecat wants. Likewise, node `tools` and
# `enter_tools` and edge `guard`s are names the application's registry (registry.py)
# resolves. An edge's `auto_after` lets code take a transition the LLM would
# otherwise spend a round trip on.
#
# These models check document *shape* only (structural parsing). Whether the graph
# can actually run — targets exist, names are unique, actions are supported — is
# `validation.validate`'s job, so a structurally valid draft with graph errors can
# still be saved and repaired.
#
# Additional-fields policy: every model accepts and preserves fields it doesn't
# declare (`extra="allow"`), so parsing and dumping never discards imported data.
# Preserved isn't supported: validation warns about extra fields the runtime ignores.
#
# `AgentConfig.model_json_schema()` is the document contract; `make types` generates
# the frontend's TypeScript types from it.
#

from typing import Annotated, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationError,
)

DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel"
DEFAULT_MODEL = "gpt-4o"


class _Model(BaseModel):
    # strict: JSON documents are not coerced (e.g. "true" is not a boolean).
    model_config = ConfigDict(extra="allow", strict=True)

    def to_dict(self) -> dict:
        """The document as written: JSON-compatible, without defaults it didn't set."""
        return self.model_dump(mode="json", exclude_unset=True)


class Message(_Model):
    """An LLM message, passed to Pipecat verbatim (any additional fields included)."""

    role: str
    # Usually text; structured content (e.g. a list of parts) is preserved as-is.
    content: JsonValue


class Position(_Model):
    """A node's canvas position. The compiler ignores it."""

    x: float
    y: float


class PropertySchema(_Model):
    """A JSON-schema property for a value an edge collects.

    Only the keywords the builder's forms edit are declared; any other keyword
    (`items`, `minimum`, ...) is preserved and passed to the LLM unchanged.
    """

    type: Optional[Union[str, list[str]]] = None
    description: Optional[str] = None
    enum: Optional[list[JsonValue]] = None


class TtsSayAction(_Model):
    """Speak `text` (Pipecat's built-in `tts_say`)."""

    type: Literal["tts_say"]
    text: str
    append_text_to_context: Optional[bool] = None


class EndConversationAction(_Model):
    """End the call, optionally saying `text` first (Pipecat's built-in `end_conversation`)."""

    type: Literal["end_conversation"]
    text: Optional[str] = None
    append_text_to_context: Optional[bool] = None


class Action(_Model):
    """Any other action. Preserved, but validation reports it: Pipecat only runs
    actions with a registered handler, and a JSON document can't carry one."""

    type: str


SUPPORTED_ACTIONS: dict[str, type[_Model]] = {
    "tts_say": TtsSayAction,
    "end_conversation": EndConversationAction,
}


def _action_kind(value) -> str:
    """Parse a well-formed supported action as its own model and anything else as
    `Action`, so a malformed `tts_say` stays saveable and validation can report it."""
    action_type = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    model = SUPPORTED_ACTIONS.get(action_type)
    if model is not None:
        try:
            model.model_validate(value)
            return action_type
        except ValidationError:
            pass
    return "other"


AnyAction = Annotated[
    Union[
        Annotated[TtsSayAction, Tag("tts_say")],
        Annotated[EndConversationAction, Tag("end_conversation")],
        Annotated[Action, Tag("other")],
    ],
    Discriminator(_action_kind),
]


class Edge(_Model):
    """A transition out of a node, exposed to the LLM as a callable tool."""

    function: str = Field(description="Tool name the LLM calls to take this edge.")
    description: str = Field(description="When the model should call it.")
    target: str = Field(description="Name of the node to transition to.")
    properties: dict[str, PropertySchema] = Field(
        default_factory=dict, description="Values to collect on this edge, as JSON-schema properties."
    )
    required: list[str] = Field(default_factory=list)
    guard: Optional[str] = Field(
        default=None, description="A named check that must pass before the edge is taken (see the registry)."
    )
    auto_after: list[str] = Field(
        default_factory=list,
        description="Node tools after which the edge is taken without an LLM call, when the call changed the "
        "state and the guard passes.",
    )


class Node(_Model):
    """A single conversational state. Fields mirror Pipecat Flows' NodeConfig."""

    name: str = Field(description="Unique within the agent; edges target nodes by name.")
    task_messages: list[Message] = Field(default_factory=list, description="This node's objectives.")
    role_message: Optional[str] = Field(default=None, description="Overrides the agent persona.")
    edges: list[Edge] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="Registry tools the node exposes besides its edges.")
    enter_tools: list[str] = Field(
        default_factory=list,
        description="Registry tools run without arguments when the node is entered; their results are added to "
        "the result of the transition into it.",
    )
    pre_actions: list[AnyAction] = Field(default_factory=list)
    post_actions: list[AnyAction] = Field(default_factory=list)
    end: bool = Field(default=False, description="Terminal: the call ends after this node.")
    ui: Optional[Position] = None


class AgentConfig(_Model):
    """A complete agent: identity + the conversation graph."""

    # Field order is the order documents are written in: identity first, graph last.
    name: str
    voice_id: str = DEFAULT_VOICE_ID
    model: str = DEFAULT_MODEL
    persona: str = Field(default="", description="Global role message, applied to every node.")
    first_message: Optional[str] = Field(
        default=None,
        description="Said when the call starts, without an LLM call; the initial node then waits for the caller.",
    )
    initial_node: str
    nodes: list[Node]
