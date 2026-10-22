#
# Catalog tools on voice calls — binds catalog/tools.py to the agent builder's
# Registry. The domain stays free of Pipecat; this adapter only moves the session
# in and out of the flow state:
#
#   flow_manager.state["patient"]   the caller's facts (PatientFacts)
#   flow_manager.state["booking"]   the rest of the SchedulingSession state
#
# Each tool call loads the session from the state, runs the shared handler and
# writes the session back if it changed. Only the tools own those two keys; edges
# can't overwrite them. Calls book in the process's shared voice calendar, which is
# created on first use, so building the registry (e.g. for validation or schema
# generation) creates no calendar or session.
#

import uuid
from functools import cache
from typing import Optional

from agent_builder import GuardBinding, Registry, ToolBinding
from catalog import Calendar, CatalogIndex, Policy, SchedulingSession, SessionState
from catalog.availability import voice_calendar
from catalog.index import default_index
from catalog.policy import default_policy
from catalog.tools import GUARDS, TOOLS, Guard, Tool, ToolContext

STATE_KEYS = frozenset({"patient", "booking"})


def load_session(state: dict, policy: Policy) -> SchedulingSession:
    """The call's session, or a new one if no tool has changed it yet."""
    booking = state.get("booking")
    if booking is None:
        return SchedulingSession(policy, f"call-{uuid.uuid4().hex[:12]}")
    return SchedulingSession(policy, SessionState.load({**booking, "patient": state.get("patient") or {}}))


def save_session(state: dict, session: SchedulingSession) -> None:
    data = session.state.dump()
    state["patient"] = data.pop("patient")
    state["booking"] = data


def build_registry(
    calendar: Optional[Calendar] = None,
    index: Optional[CatalogIndex] = None,
    policy: Optional[Policy] = None,
) -> Registry:
    """The catalog tools and guards, for sessions kept in the flow state. Defaults
    are resolved on the first call: the voice calendar and the cached catalog."""

    def context(state: dict) -> ToolContext:
        session = load_session(state, policy or default_policy())
        return ToolContext(session, calendar or voice_calendar(), index or default_index())

    def tool_binding(tool: Tool) -> ToolBinding:
        def run(args: dict, state: dict) -> tuple[dict, bool]:
            ctx = context(state)
            before = ctx.session.state
            result = tool.run(ctx, args)
            changed = ctx.session.state != before
            if changed:
                save_session(state, ctx.session)
            return result, changed

        schema = tool.parameters()
        return ToolBinding(tool.name, tool.description, schema["properties"], tuple(schema["required"]), run)

    def guard_binding(guard: Guard) -> GuardBinding:
        return GuardBinding(
            guard.name,
            guard.description,
            lambda state, args: guard.run(context(state).session, args),
            frozenset(guard.completes),
        )

    return Registry(
        tools={name: tool_binding(t) for name, t in TOOLS.items()},
        guards={name: guard_binding(g) for name, g in GUARDS.items()},
        state_keys=STATE_KEYS,
    )


@cache
def default_registry() -> Registry:
    return build_registry()
