#
# Builder API — HTTP routes the frontend uses, registered on the Pipecat dev
# runner's FastAPI app (before `main()`), so one process serves both the API and
# the WebRTC calls.
#
#   GET  /api/agents/example   the example agent, as JSON
#   POST /start                (runner route) preflighted here: an invalid agent
#                              selection is rejected with 422 before any WebRTC setup
#

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from agent_builder import AgentBuilder

EXAMPLE_AGENT = Path(__file__).parent / "example_flow.json"


class AgentSelectionError(ValueError):
    """The call's agent selection is invalid; the call must not start."""


def select_agent(body: Optional[dict]) -> AgentBuilder:
    """Pick the agent a call runs, from the runner's per-call `body`.

    `body.agent` (an inline draft) wins; with no selector, the example runs. An
    explicit selection that is invalid raises instead of falling back to the example.
    """
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise AgentSelectionError("The call body must be a JSON object.")
    if "agent" in body:
        try:
            return AgentBuilder.from_dict(body["agent"])
        except KeyError as e:
            raise AgentSelectionError(f"Invalid agent: missing field {e}") from e
        except (TypeError, ValueError, AttributeError) as e:
            raise AgentSelectionError(f"Invalid agent: {e}") from e
    if "agent_id" in body:
        # Saved agents arrive with the agents API (M2).
        raise AgentSelectionError("Saved agents (agent_id) are not supported yet.")
    return AgentBuilder.from_json(EXAMPLE_AGENT)


router = APIRouter(prefix="/api")


@router.get("/agents/example")
async def get_example_agent() -> dict[str, Any]:
    return json.loads(EXAMPLE_AGENT.read_text())


def install(app: FastAPI) -> None:
    """Register the builder routes and the /start preflight on the runner's app."""
    app.include_router(router)

    @app.middleware("http")
    async def preflight_start(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/start":
            try:
                data = await request.json()
            except ValueError:
                return await call_next(request)  # malformed JSON: the runner reports it
            body = data.get("body") if isinstance(data, dict) else None
            try:
                select_agent(body)
            except AgentSelectionError as e:
                return JSONResponse({"detail": str(e)}, status_code=422)
        return await call_next(request)
