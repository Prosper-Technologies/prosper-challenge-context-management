"""The committed JSON Schema (frontend/src/generated/) matches the backend models.

`make types` regenerates it and the TypeScript types; `make check-types` also
checks the TypeScript output.
"""

from pathlib import Path

import contract

COMMITTED = (
    Path(__file__).parent.parent.parent / "frontend" / "src" / "generated" / "contract.schema.json"
)


def test_committed_schema_is_current():
    assert COMMITTED.read_text() == contract.render(), "Stale schema: run `make types`."


def test_schema_covers_the_document_and_api_shapes():
    defs = contract.json_schema()["$defs"]
    for name in ["AgentConfig", "Node", "Edge", "Message", "Position", "Issue", "AgentSummary"]:
        assert name in defs
    assert "ui" in defs["Node"]["properties"]
    assert defs["AgentConfig"]["additionalProperties"] is True  # unknown fields are kept
