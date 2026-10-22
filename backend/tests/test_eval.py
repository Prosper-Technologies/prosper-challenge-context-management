"""The evaluation harness (backend/eval/), driven by scripted LLMs: no network."""

import asyncio
import json
from typing import Callable, Union

import pytest

from agent_builder import AgentBuilder
from catalog.scenarios import default_scenarios
from catalog_voice import build_registry
from eval.caller import END, SimulatedCaller, caller_prompt
from eval.flow import TextFlow
from eval.llm import Reply, ToolCall, Usage
from eval.report import price, render, summarize
from eval.run import SEARCH_TOOLS, Settings, catalog_text, converse, mode_agent
from tests.test_registry import SCHEDULER

SCENARIOS = {s.id: s for s in default_scenarios()}
SETTINGS = Settings("gpt-4o", "gpt-4o-mini", max_turns=12)

# A scripted reply: text, tool calls as (name, args) pairs, or a function of the
# conversation so far (to use ids from earlier tool results).
Step = Union[str, list, Callable[[list[dict]], Union[str, list]]]


class Script:
    """An LLM that replays fixed replies and records what it was sent."""

    def __init__(self, steps: list[Step]):
        self.steps = list(steps)
        self.requests: list[dict] = []

    async def complete(self, model, messages, tools=None) -> Reply:
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        usage = Usage(input=100 * len(messages), output=10, cached=50)
        if isinstance(step, str):
            return Reply(content=step, usage=usage, model=f"{model}-test", latency_s=0.5)
        calls = tuple(ToolCall(f"call_{len(self.requests)}_{i}", n, json.dumps(a)) for i, (n, a) in enumerate(step))
        return Reply(content=None, tool_calls=calls, usage=usage, model=f"{model}-test", latency_s=0.5)


def last_result(messages: list[dict], name_hint: str = "") -> dict:
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])


def flu_shot_booking(slot_index: int = 0) -> list[Step]:
    """The scheduler's side of booking a flu shot with Dr. Lucas Chen at Mission Bay."""
    return [
        "Hi, this is the clinic's scheduling assistant. How can I help?",
        [("start_booking", {})],
        [("search_services", {"query": "flu shot"})],
        [("update_booking", {"service_id": "appt_011", "patient": {"is_new_patient": False}})],
        [("update_booking", {"provider_id": "prov_047", "location_id": "loc_000"})],
        [("choices_complete", {})],
        [("get_availability", {})],
        "I have Monday at eight, eight ten or eight twenty. Which works?",
        lambda m: [("update_booking", {"slot_id": slot_from(m, slot_index)})],
        [("time_selected", {})],
        "A flu shot with Dr. Lucas Chen at Mission Bay on Monday at eight. Shall I book it?",
        lambda m: [("book_appointment", {"draft_revision": last_draft_revision(m), "confirmed": True})],
        [("booking_done", {})],
        "You're booked. Goodbye!",
    ]


def slot_from(messages: list[dict], index: int) -> str:
    offers = [json.loads(m["content"]) for m in messages if m["role"] == "tool" and '"slots"' in m["content"]]
    return offers[-1]["slots"][index]["id"]


def last_draft_revision(messages: list[dict]) -> int:
    drafts = [json.loads(m["content"]) for m in messages if m["role"] == "tool" and '"draft"' in m["content"]]
    return drafts[-1]["draft"]["revision"]


CALLER_LINES = ["Existing patient, yes.", "Lucas Chen at Mission Bay.", "The first one.", "Yes, please.", f"Bye! {END}"]


async def test_tools_run_books_through_the_real_graph():
    scenario = SCENARIOS["stale_confirmation"]
    scheduler = Script(flu_shot_booking())
    caller = Script(CALLER_LINES[2:])

    record = await converse(scenario, "tools", SCHEDULER, SETTINGS, scheduler, caller)

    assert record["ok"], record["why"]
    assert record["ended_by"] == "agent"
    assert record["path"] == ["greeting", "choose_appointment", "offer_times", "confirm", "success"]
    assert record["booking"]["provider_id"] == "prov_047"
    assert record["transitions"] == 4
    assert record["policy_violations"] == [] and record["fact_errors"] == []
    assert record["tool_errors"] == 0 and record["guard_rejections"] == 0
    assert record["state_updates"] == 5  # update_booking ×4 and book_appointment; searches change nothing
    assert record["tool_calls"]["update_booking"] == 3
    # Usage and latency come from the provider replies.
    assert record["llm_calls"] == len(scheduler.requests) == 14
    assert record["usage"]["input"] == sum(record["prompt_tokens"])
    assert record["usage"]["cached"] == 50 * 14
    assert record["models"] == {"scheduler": ["gpt-4o-test"], "caller": ["gpt-4o-mini-test"]}
    # The opening line is the scenario's, not a model call.
    assert record["trace"]["transcript"][1] == {"role": "caller", "text": scenario.caller.opening}
    assert record["caller_calls"] == 2
    assert record["turns"] == 3


async def test_context_appends_and_tools_follow_the_node():
    scheduler = Script(flu_shot_booking())
    await converse(SCENARIOS["stale_confirmation"], "tools", SCHEDULER, SETTINGS, scheduler, Script(CALLER_LINES[2:]))

    first, later = scheduler.requests[0], scheduler.requests[-1]
    assert first["messages"][0] == {"role": "system", "content": SCHEDULER["persona"]}
    # Every earlier message is still in the last prompt (APPEND strategy).
    assert later["messages"][: len(first["messages"])] == first["messages"]
    tasks = [m["content"] for m in later["messages"] if m["role"] == "developer"]
    assert len(tasks) == 5  # one per node entered
    names = lambda r: {t["function"]["name"] for t in r["tools"] or []}  # noqa: E731
    assert "book_appointment" not in names(scheduler.requests[2])
    assert "book_appointment" in names(scheduler.requests[11])
    assert later["tools"] is None  # the success node has no functions


async def test_naive_mode_has_the_catalog_and_no_search_tools():
    agent = mode_agent("naive", SCHEDULER)
    assert agent["persona"].startswith(SCHEDULER["persona"])
    assert agent["persona"].endswith(catalog_text())
    for node, original in zip(agent["nodes"], SCHEDULER["nodes"]):
        assert not SEARCH_TOOLS & set(node.get("tools", []))
        assert set(node.get("tools", [])) == set(original.get("tools", [])) - SEARCH_TOOLS
        assert node["edges" if "edges" in node else "name"] == original["edges" if "edges" in original else "name"]
    assert mode_agent("tools", SCHEDULER) == SCHEDULER
    AgentBuilder.from_dict(agent, build_registry())  # still a valid agent

    steps = flu_shot_booking()
    steps.remove([("search_services", {"query": "flu shot"})])
    scheduler = Script(steps)
    record = await converse(SCENARIOS["stale_confirmation"], "naive", SCHEDULER, SETTINGS, scheduler, Script(CALLER_LINES[2:]))
    assert record["ok"], record["why"]
    assert catalog_text() in scheduler.requests[0]["messages"][0]["content"]


async def test_a_search_tool_isnt_available_in_naive_mode():
    scheduler = Script(flu_shot_booking()[:3] + ["Sorry."])
    record = await converse(SCENARIOS["stale_confirmation"], "naive", SCHEDULER, SETTINGS, scheduler, Script([f"Bye {END}"]))
    assert record["unknown_calls"] == 1  # not in this mode's node, so not a tool error
    assert record["tool_errors"] == 0
    assert record["ended_by"] == "caller"
    assert not record["ok"]


async def test_guard_rejections_and_tool_errors_are_counted():
    steps = flu_shot_booking()
    steps[4:4] = [
        [("time_selected", {})],  # not a function in choose_appointment
        [("update_booking", {"provider_id": "prov_999"})],  # unknown id
    ]
    steps[6:6] = [[("choices_complete", {})]]  # before the provider and location
    steps.insert(-3, [("book_appointment", {"draft_revision": 1, "confirmed": True})])  # stale revision
    settings = Settings("gpt-4o", "gpt-4o-mini", max_rounds=15)  # the detours are in one assistant turn
    record = await converse(SCENARIOS["stale_confirmation"], "tools", SCHEDULER, settings, Script(steps), Script(CALLER_LINES[2:]))
    assert record["ok"], record["why"]
    assert record["unknown_calls"] == 1
    assert record["guard_rejections"] == 1
    assert record["tool_errors"] == 2
    assert record["invalid_booking_attempts"] == 1


async def test_a_booking_that_violates_the_true_facts_isnt_correct():
    # The caller is a new patient, but the agent records them as existing and books
    # Dr. Sarah Martinez, who isn't accepting new patients. The draft passes the
    # validator, and the booking even matches the outcome pattern.
    scenario = SCENARIOS["correction_new_patient"]
    steps = flu_shot_booking()
    steps[3] = [("update_booking", {"service_id": "appt_002", "patient": {"is_new_patient": False}})]
    steps[4] = [("update_booking", {"provider_id": "prov_043", "location_id": "loc_004"})]
    steps.remove([("search_services", {"query": "flu shot"})])
    record = await converse(scenario, "tools", SCHEDULER, SETTINGS, Script(steps), Script(CALLER_LINES[2:]))
    assert record["booking"]["provider_id"] == "prov_043"
    assert record["policy_violations"] == ["PROVIDER_NOT_ACCEPTING"]
    assert record["fact_errors"] == ["is_new_patient"]
    assert not record["ok"]
    assert "true facts" in record["why"]


async def test_prebooked_times_are_held_before_the_call():
    scenario = SCENARIOS["overlapping_reservations"]
    held = scenario.prebooked[0].start

    def offered_starts(messages):
        offer = last_result(messages)
        assert "slots" in offer, offer
        starts = [s["start"] for s in offer["slots"]]
        assert all(not s.startswith(held.strftime("%Y-%m-%dT08:45")) for s in starts)
        return "Which time?"

    scheduler = Script([
        "Hello!",
        [("start_booking", {})],
        [("update_booking", {"service_id": "appt_021", "patient": {"is_new_patient": False, "has_referral": True}})],
        [("service_chosen", {})],
        [("update_booking", {"provider_id": "prov_000", "location_id": "loc_001"})],
        [("choices_complete", {})],
        [("get_availability", {"day": "2026-10-19"})],
        offered_starts,
    ])  # fmt: skip
    record = await converse(scenario, "tools", SCHEDULER, SETTINGS, scheduler, Script([f"Never mind. {END}"]))
    assert record["ended_by"] == "caller"


async def test_limits_and_errors_end_the_run_with_a_record():
    talkative = Script(["Hello!"] + ["Could you say that again?"] * 3)
    record = await converse(
        SCENARIOS["obgyn_new_patient"], "tools", SCHEDULER, Settings("m", "c", max_turns=2), talkative, Script(["Hm?"] * 2)
    )
    assert record["ended_by"] == "turn_limit" and record["turns"] == 2

    looping = Script(["Hello!"] + [[("check_booking", {})]] * 3)
    record = await converse(
        SCENARIOS["obgyn_new_patient"], "tools", SCHEDULER, Settings("m", "c", max_rounds=3), looping, Script([])
    )
    assert record["ended_by"] == "round_limit"

    class Broken:
        async def complete(self, *a, **k):
            raise RuntimeError("rate limited")

    record = await converse(SCENARIOS["obgyn_new_patient"], "tools", SCHEDULER, SETTINGS, Broken(), Script([]))
    assert record["ended_by"] == "error" and "rate limited" in record["error"]
    assert not record["ok"]


async def test_the_caller_knows_its_facts_but_not_the_outcome():
    scenario = SCENARIOS["garcia_checkup_new_patient"]
    prompt = caller_prompt(scenario.caller)
    assert "new patient" in prompt and "don't have a referral" in prompt
    assert scenario.caller.opening in prompt
    assert all(d in prompt for d in scenario.caller.details)
    for pattern in scenario.outcome.booked:
        assert pattern.service_id not in prompt and pattern.provider_id not in prompt

    # Corrections are listed apart from other details, in order, to be said later.
    prompt = caller_prompt(SCENARIOS["correction_service"].caller)
    corrections = prompt[prompt.index("Corrections:") : prompt.index("How to behave")]
    assert corrections.index("just an EKG") < corrections.index("knee MRI")
    assert "Midtown is fine" not in corrections
    assert "Corrections:" not in caller_prompt(SCENARIOS["mission_blood_draw"].caller)

    llm = Script([f"Thanks, bye. {END}"])
    caller = SimulatedCaller(scenario.caller, llm, "gpt-4o-mini")
    assert await caller.respond("Hello, how can I help?") == (scenario.caller.opening, False)
    assert llm.requests == []
    assert await caller.respond("You're booked.") == ("Thanks, bye.", True)
    # The caller sees the assistant as "user" and itself as "assistant".
    assert [m["role"] for m in llm.requests[0]["messages"]] == ["system", "user", "assistant", "user"]


def test_text_flow_matches_the_compiled_node():
    flow = TextFlow(AgentBuilder.from_dict(SCHEDULER, build_registry()))
    flow.start()
    assert flow.path == ["greeting"] and flow.system == SCHEDULER["persona"]
    assert [t["function"]["name"] for t in flow.tools()] == ["start_booking", "handoff_to_staff"]
    assert flow.tools()[1]["function"]["parameters"]["required"] == ["reason"]


async def test_bad_arguments_are_errors_not_exceptions():
    flow = TextFlow(AgentBuilder.from_dict(SCHEDULER, build_registry()))
    flow.start()
    assert (await flow.call("start_booking", "{not json"))[0]["status"] == "error"
    assert (await flow.call("start_booking", "[1]"))[0]["status"] == "error"
    assert (await flow.call("book_appointment", "{}"))[0]["status"] == "error"


def test_report_summarizes_and_renders():
    base = {
        "scenario": "s1", "mode": "tools", "repetition": 0, "ok": True, "why": "booked as intended",
        "ended_by": "agent", "error": None, "path": ["greeting", "success"], "end_node": "success",
        "booking": {"service_id": "appt_011"}, "policy_violations": [], "fact_errors": [], "turns": 4,
        "llm_calls": 3, "tool_calls": {"update_booking": 2}, "tool_errors": 1, "invalid_booking_attempts": 0,
        "blocked_drafts": 0, "guard_rejections": 1, "edge_errors": 0, "unknown_calls": 0, "transitions": 1, "state_updates": 2,
        "usage": {"input": 3000, "output": 100, "cached": 1000}, "prompt_tokens": [500, 1000, 1500],
        "latencies_s": [1.0, 2.0, 3.0], "caller_usage": {"input": 400, "output": 40, "cached": 0},
        "caller_calls": 3, "models": {"scheduler": ["gpt-4o-2024"], "caller": ["gpt-4o-mini-2024"]},
    }  # fmt: skip
    records = [base, {**base, "mode": "naive", "ok": False, "why": "no booking was made", "booking": None}]
    summary = summarize(records, "gpt-4o", "gpt-4o-mini")
    assert summary["tools"]["correct"] == 1 and summary["naive"]["correct"] == 0
    assert summary["tools"]["cached_share"] == pytest.approx(1 / 3)
    assert summary["tools"]["latency_p50"] == 2.0
    assert summary["tools"]["cost"] == pytest.approx(price("gpt-4o", base["usage"]))
    assert price("gpt-4o", base["usage"]) == pytest.approx((2000 * 2.5 + 1000 * 1.25 + 100 * 10) / 1e6)
    assert price("unknown-model", base["usage"]) is None

    meta = {
        "run_id": "r", "started": "now", "scheduler_model": "gpt-4o", "caller_model": "gpt-4o-mini",
        "scheduler_models_reported": ["gpt-4o-2024"], "caller_models_reported": ["gpt-4o-mini-2024"],
        "modes": ["naive", "tools"], "scenarios": 1, "repetitions": 1, "max_turns": 20, "max_rounds": 10,
        "catalog_sha": "abc", "catalog_tokens_est": 10000, "agent_sha": "def", "scenarios_sha": "ghi",
    }  # fmt: skip
    text = render(meta, records)
    assert "| Intended outcome reached | **0/1** (0%) | **1/1** (100%) |" in text
    assert "| naive | `s1` | 0 | agent at `success` | no booking was made |" in text
    assert "| 1 | 500 (1) | 500 (1) |" in text  # the first call's prompt size in each mode


def test_cli_refuses_bad_options_before_calling_the_api(monkeypatch, capsys):
    import eval.__main__ as cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cli, "OpenAIChat", lambda: pytest.fail("no API client should be created"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(cli.main(cli.parse_args([]))) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert asyncio.run(cli.main(cli.parse_args(["--modes", "naive,bogus"]))) == 2
    assert asyncio.run(cli.main(cli.parse_args(["--scenarios", "chen_paging,nope"]))) == 2
    assert cli.parse_args([]).model == SCHEDULER["model"]
