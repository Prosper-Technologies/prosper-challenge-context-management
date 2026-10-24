"""`python -m eval` (from backend/, or `make eval`): run the naive vs tools benchmark.

Writes results.md and results.json to --out (default: this folder) and the full
traces of every conversation to <out>/runs/<run id>/traces.jsonl.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from catalog.models import CATALOG_PATH
from catalog.scenarios import SCENARIOS_PATH, default_scenarios
from catalog.tools import approx_tokens

from .llm import OpenAIChat
from .report import render, summarize
from .run import MODES, Settings, catalog_text, converse

HERE = Path(__file__).parent
AGENT_PATH = HERE.parent / "agents" / "scheduler.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def parse_args(argv=None) -> argparse.Namespace:
    agent_model = json.loads(AGENT_PATH.read_text())["model"]
    p = argparse.ArgumentParser(prog="python -m eval", description=__doc__.splitlines()[0])
    p.add_argument("--modes", default=",".join(MODES), help="comma-separated: naive,tools")
    p.add_argument("--k", type=int, default=3, help="repetitions per scenario and mode")
    p.add_argument("--scenarios", default="", help="comma-separated scenario ids (default: all)")
    p.add_argument("--model", default=agent_model, help=f"scheduler model (default: the agent's, {agent_model})")
    p.add_argument("--caller-model", default="gpt-4o-mini", help="simulated caller model")
    p.add_argument("--concurrency", type=int, default=6, help="conversations in flight")
    p.add_argument("--max-turns", type=int, default=20, help="caller turns per conversation")
    p.add_argument("--out", type=Path, default=HERE, help="where results.md/results.json go")
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> int:
    load_dotenv(HERE.parent / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY isn't set (backend/.env); the evaluation calls the OpenAI API.", file=sys.stderr)
        return 2
    modes = [m for m in args.modes.split(",") if m]
    if unknown := set(modes) - set(MODES):
        print(f"unknown mode(s): {sorted(unknown)}", file=sys.stderr)
        return 2
    scenarios = list(default_scenarios())
    if args.scenarios:
        wanted = args.scenarios.split(",")
        scenarios = [s for s in scenarios if s.id in wanted]
        if missing := set(wanted) - {s.id for s in scenarios}:
            print(f"unknown scenario(s): {sorted(missing)}", file=sys.stderr)
            return 2

    agent = json.loads(AGENT_PATH.read_text())
    agent["model"] = args.model
    settings = Settings(args.model, args.caller_model, max_turns=args.max_turns)
    llm = OpenAIChat()
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    jobs = [(s, m, k) for k in range(args.k) for s in scenarios for m in modes]
    gate = asyncio.Semaphore(args.concurrency)
    done = 0

    async def job(scenario, mode, k):
        nonlocal done
        async with gate:
            record = await converse(scenario, mode, agent, settings, llm, llm, k)
        done += 1
        mark = "ok  " if record["ok"] else "MISS"
        print(
            f"[{done}/{len(jobs)}] {mark} {mode:5} {scenario.id} #{k}: {record['why']} "
            f"({record['turns']} turns, {record['usage']['input']:,} input tokens, ended by {record['ended_by']})",
            flush=True,
        )
        return record

    records = await asyncio.gather(*(job(*j) for j in jobs))
    order = {s.id: i for i, s in enumerate(scenarios)}
    records.sort(key=lambda r: (order[r["scenario"]], r["mode"], r["repetition"]))

    meta = {
        "run_id": run_id,
        "started": started.isoformat(timespec="seconds"),
        "scheduler_model": args.model,
        "caller_model": args.caller_model,
        "scheduler_models_reported": sorted({m for r in records for m in r["models"]["scheduler"]}),
        "caller_models_reported": sorted({m for r in records for m in r["models"]["caller"]}),
        "modes": modes,
        "scenarios": len(scenarios),
        "repetitions": args.k,
        "max_turns": settings.max_turns,
        "max_rounds": settings.max_rounds,
        "catalog_sha": _sha(CATALOG_PATH),
        "catalog_tokens_est": approx_tokens(json.loads(catalog_text())),
        "agent_sha": _sha(AGENT_PATH),
        "scenarios_sha": _sha(SCENARIOS_PATH),
    }
    runs_dir = args.out / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    with (runs_dir / "traces.jsonl").open("w") as f:
        for r in records:
            f.write(json.dumps({k: r[k] for k in ("scenario", "mode", "repetition", "trace")}) + "\n")
    plain = [{k: v for k, v in r.items() if k != "trace"} for r in records]
    summary = summarize(plain, args.model, args.caller_model)
    (args.out / "results.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "runs": plain}, indent=1) + "\n"
    )
    (args.out / "results.md").write_text(render(meta, plain))
    for mode in modes:
        s = summary[mode]
        print(f"{mode}: {s['correct']}/{s['runs']} correct, {s['input_tokens']:,.0f} input tokens/conversation")
    print(f"Wrote {args.out / 'results.md'} and traces in {runs_dir}")
    return 0


if __name__ == "__main__":
    logger.remove()  # the builder logs every tool call; keep warnings only
    logger.add(sys.stderr, level="WARNING")
    sys.exit(asyncio.run(main(parse_args())))
