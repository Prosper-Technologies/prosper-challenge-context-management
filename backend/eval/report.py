#
# Aggregates evaluation runs into results.json and the results.md comparison.
#
# Intended-outcome correctness, guardrail activity and actual policy violations are
# reported separately: the guardrails are the same in both modes, so zero
# violations alone says nothing about whether the right appointment was booked.
# Scheduler and simulated-caller usage are kept apart, and cost is an estimate from
# the PRICING assumptions below, not a bill.
#

import statistics
from collections import defaultdict
from typing import Optional

# USD per million tokens: (input, cached input, output). Assumed list prices, recorded
# with every report; check them before relying on the cost estimates.
PRICING: dict[str, tuple[float, float, float]] = {
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}


def price(model: str, usage: dict) -> Optional[float]:
    rates = PRICING.get(model)
    if rates is None:
        return None
    uncached = usage["input"] - usage["cached"]
    return (uncached * rates[0] + usage["cached"] * rates[1] + usage["output"] * rates[2]) / 1e6


def _mean(values) -> float:
    values = list(values)
    return statistics.fmean(values) if values else 0.0


def _pct(values, q: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def summarize(records: list[dict], scheduler_model: str, caller_model: str) -> dict:
    """Per-mode aggregates."""
    by_mode: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_mode[r["mode"]].append(r)
    out = {}
    for mode, runs in by_mode.items():
        latencies = [x for r in runs for x in r["latencies_s"]]
        per_scenario = defaultdict(list)
        for r in runs:
            per_scenario[r["scenario"]].append(r["ok"])
        cost = [price(scheduler_model, r["usage"]) for r in runs]
        caller_cost = [price(caller_model, r["caller_usage"]) for r in runs]
        input_tokens = sum(r["usage"]["input"] for r in runs)
        out[mode] = {
            "runs": len(runs),
            "correct": sum(r["ok"] for r in runs),
            "scenarios_all_correct": sum(all(v) for v in per_scenario.values()),
            "scenarios": len(per_scenario),
            "booked": sum(r["booking"] is not None for r in runs),
            "wrong_bookings": sum(r["booking"] is not None and not r["ok"] for r in runs),
            "policy_violations": sum(bool(r["policy_violations"]) for r in runs),
            "fact_errors": sum(bool(r["fact_errors"]) for r in runs),
            "invalid_booking_attempts": sum(r["invalid_booking_attempts"] for r in runs),
            "guard_rejections": sum(r["guard_rejections"] for r in runs),
            "tool_errors": sum(r["tool_errors"] for r in runs),
            "unknown_calls": sum(r["unknown_calls"] for r in runs),
            "blocked_drafts": sum(r["blocked_drafts"] for r in runs),
            "errors": sum(r["ended_by"] == "error" for r in runs),
            "unfinished": sum(r["ended_by"] in ("turn_limit", "round_limit") for r in runs),
            "turns": _mean(r["turns"] for r in runs),
            "llm_calls": _mean(r["llm_calls"] for r in runs),
            "tool_calls": _mean(sum(r["tool_calls"].values()) for r in runs),
            "input_tokens": _mean(r["usage"]["input"] for r in runs),
            "cached_share": (sum(r["usage"]["cached"] for r in runs) / input_tokens) if input_tokens else 0.0,
            "output_tokens": _mean(r["usage"]["output"] for r in runs),
            "first_prompt": _mean(r["prompt_tokens"][0] for r in runs if r["prompt_tokens"]),
            "last_prompt": _mean(r["prompt_tokens"][-1] for r in runs if r["prompt_tokens"]),
            "max_prompt": max((max(r["prompt_tokens"]) for r in runs if r["prompt_tokens"]), default=0),
            "latency_mean": _mean(latencies),
            "latency_p50": _pct(latencies, 0.5),
            "latency_p95": _pct(latencies, 0.95),
            "llm_time": _mean(sum(r["latencies_s"]) for r in runs),
            "cost": _mean(c for c in cost if c is not None) if None not in cost else None,
            "cost_total": sum(cost) if None not in cost else None,
            "caller_input_tokens": _mean(r["caller_usage"]["input"] for r in runs),
            "caller_output_tokens": _mean(r["caller_usage"]["output"] for r in runs),
            "caller_cost_total": sum(caller_cost) if None not in caller_cost else None,
        }
    return out


def per_scenario(records: list[dict]) -> dict[str, dict[str, dict]]:
    """scenario -> mode -> {correct, runs, input_tokens, turns, last_prompt}."""
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        grouped[r["scenario"]][r["mode"]].append(r)
    return {
        scenario: {
            mode: {
                "correct": sum(r["ok"] for r in runs),
                "runs": len(runs),
                "input_tokens": _mean(r["usage"]["input"] for r in runs),
                "turns": _mean(r["turns"] for r in runs),
                "last_prompt": _mean(r["prompt_tokens"][-1] for r in runs if r["prompt_tokens"]),
            }
            for mode, runs in modes.items()
        }
        for scenario, modes in grouped.items()
    }


def growth(records: list[dict], points=(1, 5, 10, 15, 20, 25, 30)) -> dict[str, dict[int, tuple[float, int]]]:
    """mode -> LLM call number -> (mean prompt tokens, conversations that got that far)."""
    out: dict[str, dict[int, tuple[float, int]]] = defaultdict(dict)
    for mode in sorted({r["mode"] for r in records}):
        runs = [r for r in records if r["mode"] == mode]
        for n in points:
            reached = [r["prompt_tokens"][n - 1] for r in runs if len(r["prompt_tokens"]) >= n]
            if reached:
                out[mode][n] = (_mean(reached), len(reached))
    return out


def _money(value: Optional[float]) -> str:
    return "n/a" if value is None else f"${value:.4f}" if value < 1 else f"${value:.2f}"


def render(meta: dict, records: list[dict]) -> str:
    summary = summarize(records, meta["scheduler_model"], meta["caller_model"])
    modes = [m for m in ("naive", "tools") if m in summary] + [m for m in summary if m not in ("naive", "tools")]
    lines = [
        "# Evaluation results: naive vs tools",
        "",
        "Generated by `make eval` (`backend/eval/`). Each scenario in `backend/data/scenarios.json` runs "
        f"{meta['repetitions']} time(s) per mode against a simulated caller, each run with a fresh session and "
        "calendar. Correctness is checked against the session state the tools left behind, never against "
        "what the agent said. See `solution.md` for the discussion.",
        "",
        "## Configuration",
        "",
        "| | |",
        "|---|---|",
        f"| Run | `{meta['run_id']}` ({meta['started']}) |",
        f"| Scheduler model | `{meta['scheduler_model']}` (provider reported: {', '.join(f'`{m}`' for m in meta['scheduler_models_reported']) or 'n/a'}), provider-default sampling |",
        f"| Simulated caller | `{meta['caller_model']}` (provider reported: {', '.join(f'`{m}`' for m in meta['caller_models_reported']) or 'n/a'}) |",
        f"| Scenarios × repetitions × modes | {meta['scenarios']} × {meta['repetitions']} × {len(modes)} = {len(records)} conversations |",
        f"| Limits | {meta['max_turns']} caller turns, {meta['max_rounds']} scheduler calls per assistant turn |",
        f"| Catalog | `catalog.json` sha256 `{meta['catalog_sha']}` ({meta['catalog_tokens_est']} tokens est. as compact JSON) |",
        f"| Agent | `agents/scheduler.json` sha256 `{meta['agent_sha']}`; scenarios sha256 `{meta['scenarios_sha']}` |",
        f"| Pricing assumed (USD / 1M tokens: input, cached input, output) | "
        + "; ".join(f"`{m}` {p[0]}, {p[1]}, {p[2]}" for m, p in PRICING.items())
        + " |",
        "",
        "## Summary",
        "",
        "| | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]

    def row(label: str, fmt):
        lines.append(f"| {label} | " + " | ".join(fmt(summary[m]) for m in modes) + " |")

    row("Intended outcome reached", lambda s: f"**{s['correct']}/{s['runs']}** ({s['correct'] / s['runs']:.0%})")
    row("Scenarios correct in every run", lambda s: f"{s['scenarios_all_correct']}/{s['scenarios']}")
    row("Completed bookings", lambda s: str(s["booked"]))
    row("Bookings that weren't the intended appointment", lambda s: str(s["wrong_bookings"]))
    row("Bookings violating policy for the caller's true facts", lambda s: str(s["policy_violations"]))
    row("Runs that recorded a wrong patient fact", lambda s: str(s["fact_errors"]))
    row("Invalid booking attempts (book_appointment refused)", lambda s: str(s["invalid_booking_attempts"]))
    row("Guard rejections (edge refused)", lambda s: str(s["guard_rejections"]))
    row("Tool errors (all tools)", lambda s: str(s["tool_errors"]))
    row("Calls to a function the current step doesn't have", lambda s: str(s["unknown_calls"]))
    row("Updates that left the draft blocked", lambda s: str(s["blocked_drafts"]))
    row("Unfinished (turn/round limit) / API errors", lambda s: f"{s['unfinished']} / {s['errors']}")
    row("Caller turns (mean)", lambda s: f"{s['turns']:.1f}")
    row("Scheduler LLM calls (mean)", lambda s: f"{s['llm_calls']:.1f}")
    row("Tool and edge calls (mean)", lambda s: f"{s['tool_calls']:.1f}")
    row("Scheduler input tokens / conversation (mean)", lambda s: f"{s['input_tokens']:,.0f}")
    row("Cached share of input", lambda s: f"{s['cached_share']:.0%}")
    row("Scheduler output tokens / conversation (mean)", lambda s: f"{s['output_tokens']:,.0f}")
    row("Prompt tokens: first call → last call (mean)", lambda s: f"{s['first_prompt']:,.0f} → {s['last_prompt']:,.0f}")
    row("Largest prompt", lambda s: f"{s['max_prompt']:,}")
    row("Scheduler latency per call: mean / p50 / p95", lambda s: f"{s['latency_mean']:.2f}s / {s['latency_p50']:.2f}s / {s['latency_p95']:.2f}s")
    row("Scheduler time / conversation (mean)", lambda s: f"{s['llm_time']:.1f}s")
    row("Est. scheduler cost / conversation", lambda s: _money(s["cost"]))
    row("Est. scheduler cost, all runs", lambda s: _money(s["cost_total"]))
    row("Simulated caller tokens / conversation (in / out)", lambda s: f"{s['caller_input_tokens']:,.0f} / {s['caller_output_tokens']:,.0f}")
    row("Est. simulated caller cost, all runs (not scheduler cost)", lambda s: _money(s["caller_cost_total"]))

    lines += ["", "## By scenario", "", "Correct runs, mean scheduler input tokens and mean caller turns per conversation.", ""]
    lines.append("| Scenario | " + " | ".join(f"{m} correct | {m} input | {m} turns" for m in modes) + " |")
    lines.append("|---|" + "---|---|---|" * len(modes))
    for scenario, by_mode in per_scenario(records).items():
        cells = []
        for m in modes:
            s = by_mode.get(m)
            cells += [f"{s['correct']}/{s['runs']}", f"{s['input_tokens']:,.0f}", f"{s['turns']:.1f}"] if s else ["", "", ""]
        lines.append(f"| `{scenario}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Context growth",
        "",
        "Mean prompt tokens at the Nth scheduler call (and how many conversations got that far). "
        "Pipecat Flows appends each node's messages and keeps every tool result, so the prompt grows with the conversation.",
        "",
    ]
    curve = growth(records)
    points = sorted({n for m in curve.values() for n in m})
    lines.append("| Call | " + " | ".join(modes) + " |")
    lines.append("|---|" + "---|" * len(modes))
    for n in points:
        cells = [f"{curve[m][n][0]:,.0f} ({curve[m][n][1]})" if n in curve.get(m, {}) else "" for m in modes]
        lines.append(f"| {n} | " + " | ".join(cells) + " |")

    failures = [r for r in records if not r["ok"]]
    lines += ["", "## Runs that missed the intended outcome", ""]
    if not failures:
        lines.append("None.")
    else:
        lines.append("| Mode | Scenario | Run | Ended | Why |")
        lines.append("|---|---|---|---|---|")
        for r in sorted(failures, key=lambda r: (r["mode"], r["scenario"], r["repetition"])):
            ended = f"{r['ended_by']} at `{r['end_node']}`"
            why = r["why"] + (f"; {r['error']}" if r["error"] else "")
            lines.append(f"| {r['mode']} | `{r['scenario']}` | {r['repetition']} | {ended} | {why} |")
    lines.append("")
    return "\n".join(lines)
