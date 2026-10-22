#
# One evaluated conversation: a scheduler agent (naive or tools) talking to the
# simulated caller of one scenario, with a fresh session and calendar.
#
#   naive   the scheduler document with the full catalog JSON in its system prompt
#           and no catalog search tools
#   tools   the scheduler document as is: search tools, no catalog in the prompt
#
# Everything else is the same in both modes: the graph, its guards, the scheduler
# model and settings, the caller, update_booking/check_booking/get_availability/
# book_appointment (so the same patient-state mutations and booking validator), the
# simulated date and the initial calendar. Catalog access is the variable.
#
# The outcome is checked against the session state the tools left in the flow state,
# with the scenario's check_outcome; what the agent said doesn't count. A reservation
# is also checked against the caller's true facts, because the validator only knows
# the facts the agent recorded; a booking that violates them is never correct.
#

import copy
import json
from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import Optional

from agent_builder import AgentBuilder
from catalog import BookingDraft, Calendar, PatientFacts, default_catalog, default_policy
from catalog.models import CATALOG_PATH
from catalog.scenarios import Scenario, check_outcome, prebook
from catalog_voice import build_registry, load_session

from .caller import SimulatedCaller
from .flow import TextFlow
from .llm import LLM, Usage

MODES = ("naive", "tools")
SEARCH_TOOLS = frozenset({"catalog_overview", "search_services", "find_providers", "find_locations"})

NAIVE_NOTE = """

You have no catalog search tools. The clinic's full catalog is below as JSON: its locations, providers, appointment types and booking policies. Wherever your instructions mention search_services, find_providers, find_locations or catalog_overview, look the answer up in this catalog instead, and record the IDs you find with update_booking.

CATALOG:
"""


@dataclass(frozen=True)
class Settings:
    scheduler_model: str
    caller_model: str
    max_turns: int = 20  # caller turns
    max_rounds: int = 10  # scheduler LLM calls in one assistant turn


@cache
def catalog_text() -> str:
    """The catalog file as compact JSON, as the naive scheduler receives it."""
    return json.dumps(json.loads(CATALOG_PATH.read_text()), separators=(",", ":"), ensure_ascii=False)


def mode_agent(mode: str, agent: dict) -> dict:
    """The scheduler document for an evaluation mode."""
    agent = copy.deepcopy(agent)
    if mode == "naive":
        agent["persona"] = agent["persona"] + NAIVE_NOTE + catalog_text()
        for node in agent["nodes"]:
            if "tools" in node:
                node["tools"] = [t for t in node["tools"] if t not in SEARCH_TOOLS]
    elif mode != "tools":
        raise ValueError(f"unknown mode {mode!r}")
    return agent


def _add(total: Usage, usage: Usage) -> Usage:
    return Usage(total.input + usage.input, total.output + usage.output, total.cached + usage.cached)


async def converse(
    scenario: Scenario,
    mode: str,
    agent: dict,
    settings: Settings,
    scheduler: LLM,
    caller_llm: LLM,
    repetition: int = 0,
) -> dict:
    """Run one conversation; returns its metrics, with the full trace under "trace"."""
    calendar = Calendar(default_catalog(), clock=lambda: scenario.now)
    prebook(scenario, calendar)
    builder = AgentBuilder.from_dict(mode_agent(mode, agent), build_registry(calendar=calendar))
    tool_names = set(builder.registry.tools)
    flow = TextFlow(builder)
    caller = SimulatedCaller(scenario.caller, caller_llm, settings.caller_model)

    transcript: list[dict] = []
    events: list[dict] = []
    counts = Counter()
    tool_calls = Counter()
    usage = Usage()
    prompt_tokens: list[int] = []
    latencies: list[float] = []
    models: set[str] = set()

    async def assistant_turn() -> Optional[str]:
        """Run the scheduler until it speaks; None if it never does within the limit."""
        spoken = []
        for _ in range(settings.max_rounds):
            reply = await scheduler.complete(settings.scheduler_model, flow.prompt(), flow.tools() or None)
            nonlocal usage
            usage = _add(usage, reply.usage)
            prompt_tokens.append(reply.usage.input)
            latencies.append(reply.latency_s)
            models.add(reply.model)
            events.append(
                {
                    "type": "llm",
                    "node": flow.node["name"],
                    "input": reply.usage.input,
                    "cached": reply.usage.cached,
                    "output": reply.usage.output,
                    "latency_s": round(reply.latency_s, 3),
                }
            )
            message: dict = {"role": "assistant", "content": reply.content}
            if reply.content:
                spoken.append(reply.content)
            if not reply.tool_calls:
                flow.messages.append(message)
                if flow.ending:
                    flow.ended = True
                return " ".join(spoken)
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in reply.tool_calls
            ]
            flow.messages.append(message)
            next_node = None
            for c in reply.tool_calls:
                node = flow.node["name"]
                kind = "unknown" if c.name not in flow.functions else "tool" if c.name in tool_names else "edge"
                if next_node is not None:
                    result = {"status": "error", "message": "A transition is already in progress."}
                else:
                    result, next_node = await flow.call(c.name, c.arguments)
                flow.messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)})
                tool_calls[c.name] += 1
                _count(counts, c.name, kind, result)
                events.append(
                    {
                        "type": "tool",
                        "node": node,
                        "name": c.name,
                        "args": c.arguments,
                        "result": result,
                        **({"to": next_node["name"]} if next_node else {}),
                    }
                )
            if next_node is not None:
                counts["transitions"] += 1
                flow.enter(next_node)
        return None

    ended_by, error = "turn_limit", None
    turns = 0
    try:
        flow.start()
        said = await assistant_turn()
        while said is not None:
            transcript.append({"role": "assistant", "text": said})
            if flow.ended:
                ended_by = "agent"
                break
            if turns >= settings.max_turns:
                break
            text, hung_up = await caller.respond(said)
            turns += 1
            transcript.append({"role": "caller", "text": text})
            if hung_up:
                ended_by = "caller"
                break
            flow.messages.append({"role": "user", "content": text})
            said = await assistant_turn()
        else:
            ended_by = "round_limit"
    except Exception as e:  # recorded, so one failed request doesn't lose the batch
        ended_by, error = "error", f"{type(e).__name__}: {e}"

    session = load_session(flow.state, default_policy())
    ok, why = check_outcome(scenario, session)
    reservation = session.state.reservation
    truth = PatientFacts(is_new_patient=scenario.caller.is_new_patient, has_referral=scenario.caller.has_referral)
    violations: list[str] = []
    booking = None
    if reservation is not None:
        slot = reservation.slot
        booking = {
            "service_id": slot.service_id,
            "provider_id": slot.provider_id,
            "location_id": slot.location_id,
            "start": slot.start.isoformat(),
        }
        draft = BookingDraft(service_id=slot.service_id, provider_id=slot.provider_id, location_id=slot.location_id)
        violations = sorted({v.code for v in default_policy().explain(draft, truth)})
        if ok and violations:
            ok, why = False, f"{why}, but the caller's true facts violate {', '.join(violations)}"
    recorded = session.state.patient
    fact_errors = [
        f for f in ("is_new_patient", "has_referral") if getattr(recorded, f) not in (None, getattr(truth, f))
    ]
    caller_usage = Usage()
    for r in caller.replies:
        caller_usage = _add(caller_usage, r.usage)

    return {
        "scenario": scenario.id,
        "mode": mode,
        "repetition": repetition,
        "ok": ok,
        "why": why,
        "ended_by": ended_by,
        "error": error,
        "path": flow.path,
        "end_node": flow.path[-1] if flow.path else None,
        "booking": booking,
        "policy_violations": violations,
        "fact_errors": fact_errors,
        "turns": turns,
        "llm_calls": len(prompt_tokens),
        "tool_calls": dict(tool_calls),
        "tool_errors": counts["tool_errors"],
        "invalid_booking_attempts": counts["invalid_booking_attempts"],
        "blocked_drafts": counts["blocked_drafts"],
        "guard_rejections": counts["guard_rejections"],
        "edge_errors": counts["edge_errors"],
        "unknown_calls": counts["unknown_calls"],
        "transitions": counts["transitions"],
        "state_updates": len(flow.state_updates),
        "usage": vars(usage),
        "prompt_tokens": prompt_tokens,
        "latencies_s": [round(x, 3) for x in latencies],
        "caller_usage": vars(caller_usage),
        "caller_calls": len(caller.replies),
        "models": {"scheduler": sorted(models), "caller": sorted({r.model for r in caller.replies})},
        "trace": {"system": flow.system, "transcript": transcript, "events": events, "final_state": flow.state},
    }


def _count(counts: Counter, name: str, kind: str, result: dict) -> None:
    if kind == "unknown":  # a function the active node doesn't have
        counts["unknown_calls"] += 1
    elif kind == "tool":
        if "error" in result:
            counts["tool_errors"] += 1
            if name == "book_appointment":
                counts["invalid_booking_attempts"] += 1
        elif name == "update_booking" and result.get("check", {}).get("status") == "blocked":
            counts["blocked_drafts"] += 1
    elif result.get("status") == "error":
        counts["guard_rejections" if "guard" in result else "edge_errors"] += 1
