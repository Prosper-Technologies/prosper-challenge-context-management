"""The naive vs tools scheduling benchmark (`make eval`).

Runs the scheduler agent's real compiled graph as text (flow.py) against a simulated
caller (caller.py) for each scenario in data/scenarios.json, once with the full
catalog in the prompt and once with the catalog tools (run.py), and writes the
comparison to results.md (report.py)."""
