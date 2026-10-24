import copy

import pytest

from api import AgentStore
from tests.helpers import EXAMPLE, EXAMPLE_PATH


@pytest.fixture
def example() -> dict:
    return copy.deepcopy(EXAMPLE)


@pytest.fixture
def store(tmp_path) -> AgentStore:
    """An agent store in a temp directory, seeded with the example."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "example.json").write_text(EXAMPLE_PATH.read_text())
    return AgentStore(agents)
