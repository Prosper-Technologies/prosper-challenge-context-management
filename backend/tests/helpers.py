import copy
import json
from pathlib import Path

EXAMPLE_PATH = Path(__file__).parent.parent / "agents" / "example.json"
EXAMPLE = json.loads(EXAMPLE_PATH.read_text())


def draft(**changes) -> dict:
    """A copy of the example agent with top-level fields replaced."""
    agent = copy.deepcopy(EXAMPLE)
    agent.update(changes)
    return agent
