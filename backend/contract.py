#
# The backend/frontend contract, as one JSON Schema: the agent document, the
# catalog, the catalog tools' arguments and results, and the API's request and
# response shapes. `make types` writes it to frontend/src/generated/ and
# generates the TypeScript types from it; tests/test_contract.py fails if the
# committed schema is stale.
#
#   python contract.py > ../frontend/src/generated/contract.schema.json
#

import json

from pydantic.json_schema import GenerateJsonSchema, models_json_schema

from agent_builder import AgentConfig, Issue
from api import AgentSummary, IssuesResponse
from catalog import Catalog
from catalog_api import (
    LintResponse,
    PlaygroundSession,
    ToolArgs,
    ToolResults,
    ToolRunRequest,
    ToolRunResponse,
    ToolsResponse,
)
from catalog.tools import ToolErrorResult

TITLE = "AgentBuilderContract"


class _ContractSchema(GenerateJsonSchema):
    # Field titles ("Name", "Type", ...) would each become a TypeScript alias.
    def field_title_should_be_set(self, schema) -> bool:
        return False


def json_schema() -> dict:
    _, schema = models_json_schema(
        [
            (AgentConfig, "validation"),
            (Issue, "serialization"),
            (AgentSummary, "serialization"),
            (IssuesResponse, "serialization"),
            (Catalog, "serialization"),
            (LintResponse, "serialization"),
            (ToolsResponse, "serialization"),
            (PlaygroundSession, "serialization"),
            (ToolRunRequest, "validation"),
            (ToolRunResponse, "serialization"),
            # Tool results are sent without the fields at their defaults, which is
            # what the validation schema says: those fields are optional.
            (ToolArgs, "validation"),
            (ToolResults, "validation"),
            (ToolErrorResult, "validation"),
        ],
        title=TITLE,
        schema_generator=_ContractSchema,
    )
    return schema


def render() -> str:
    return json.dumps(json_schema(), indent=2) + "\n"


if __name__ == "__main__":
    print(render(), end="")
