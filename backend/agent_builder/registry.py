#
# Registry — the tools and edge guards an agent document can name, supplied by the
# application rather than known to the compiler.
#
#   Node.tools  ["update_booking", ...]   functions the node exposes besides its edges
#   Edge.guard  "booking_ready"           a check that must pass before the edge runs
#
# The builder and validator only see names, schemas and callables here. What a tool
# does (e.g. the clinic scheduling tools, bound in catalog_voice.py) stays outside the
# generic compiler, and an agent without tools or guards behaves as before.
#
# Tools own the `state_keys` of the flow state: an edge can't overwrite them with
# the arguments it collects.
#

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional

# (arguments, flow state) -> (JSON result, whether the state changed). May mutate state.
ToolRun = Callable[[dict, dict], tuple[dict, bool]]
# flow state -> None if the edge may be taken, else recovery information for the LLM.
GuardCheck = Callable[[dict], Optional[dict]]


@dataclass(frozen=True)
class ToolBinding:
    name: str
    description: str
    properties: dict
    required: tuple[str, ...]
    run: ToolRun


@dataclass(frozen=True)
class GuardBinding:
    name: str
    description: str
    check: GuardCheck
    # An agent that exposes any of these tools must have an end node entered only
    # through this guard (e.g. success only after book_appointment has booked).
    completes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Registry:
    tools: Mapping[str, ToolBinding] = field(default_factory=dict)
    guards: Mapping[str, GuardBinding] = field(default_factory=dict)
    state_keys: frozenset[str] = frozenset()


EMPTY_REGISTRY = Registry()
