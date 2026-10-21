#
# Builder API — HTTP routes the frontend uses, registered on the Pipecat dev
# runner's FastAPI app (before `main()`), so one process serves both the API and
# the WebRTC calls.
#
#   GET    /api/agents            saved agents: [{id, name}]
#   GET    /api/agents/{id}       one saved agent document, as written
#   PUT    /api/agents/{id}       create or replace; -> {issues, malformed}
#   DELETE /api/agents/{id}
#   POST   /api/agents/validate   -> {issues, malformed} for a document, without saving it
#   POST   /start                 (runner route) preflighted here: an invalid agent
#                                 selection, or a missing API key, is rejected with 422
#                                 before any WebRTC setup
#
# Validation and calls resolve node tools and edge guards against the catalog
# registry (catalog_voice.py).
#
# Agents are stored one JSON file per agent in backend/agents/. A structurally
# valid document is saved even if its graph has errors (so drafts can be saved and
# repaired later); it just can't run. A malformed one is rejected (422) and the
# saved file is left untouched.
#

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_builder import AgentBuilder, AgentConfig, AgentValidationError, Issue, Registry, parse, validate
from catalog_voice import default_registry

AGENTS_DIR = Path(__file__).parent / "agents"
DEFAULT_AGENT_ID = "example"  # runs when a call selects no agent
AGENT_ID = re.compile(r"^[a-z0-9_-]{1,64}$")
# Read from backend/.env (see .env.example) by bot.py, when a call starts.
REQUIRED_ENV = ("OPENAI_API_KEY", "ELEVENLABS_API_KEY")


class AgentSummary(BaseModel):
    id: str
    name: Optional[str]  # None if the file isn't a readable agent


class IssuesResponse(BaseModel):
    issues: list[Issue]
    # The document isn't structurally an agent: `issues` are shape errors, and it
    # can't be saved. Otherwise any errors are graph errors (saveable, not runnable).
    malformed: bool


# ---- storage ---------------------------------------------------------------------


class InvalidAgentId(ValueError):
    pass


class AgentNotFound(LookupError):
    pass


class AgentStore:
    """One JSON file per agent, named by its id."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, agent_id: str) -> Path:
        # The id pattern has no dots or slashes, so it can't leave the directory.
        if not isinstance(agent_id, str) or not AGENT_ID.match(agent_id):
            raise InvalidAgentId(
                f"Invalid agent id {agent_id!r}: use 1-64 lowercase letters, digits, '_' or '-'."
            )
        return self.root / f"{agent_id}.json"

    def list(self) -> list[AgentSummary]:
        summaries = []
        for path in sorted(self.root.glob("*.json")):
            if not AGENT_ID.match(path.stem):
                continue
            try:
                name = json.loads(path.read_text()).get("name")
            except (OSError, ValueError, AttributeError):
                name = None
            summaries.append(
                AgentSummary(id=path.stem, name=name if isinstance(name, str) else None)
            )
        return summaries

    def load(self, agent_id: str) -> Any:
        """The saved document, as written (it may have graph errors)."""
        path = self.path(agent_id)
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise AgentNotFound(f"No saved agent '{agent_id}'.") from None
        return json.loads(text)

    def save(self, agent_id: str, config: AgentConfig) -> None:
        """Write atomically: a crash or failed write leaves the previous file intact."""
        path = self.path(agent_id)
        text = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{agent_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def delete(self, agent_id: str) -> None:
        try:
            self.path(agent_id).unlink()
        except FileNotFoundError:
            raise AgentNotFound(f"No saved agent '{agent_id}'.") from None


store = AgentStore(AGENTS_DIR)


# ---- per-call agent selection ------------------------------------------------------


class AgentSelectionError(ValueError):
    """The call's agent selection is invalid; the call must not start."""

    def __init__(self, message: str, issues: Optional[list[Issue]] = None):
        super().__init__(message)
        self.issues = issues or []


def select_agent(
    body: Optional[dict], agents: Optional[AgentStore] = None, registry: Optional[Registry] = None
) -> AgentBuilder:
    """Pick the agent a call runs, from the runner's per-call `body`.

    `body.agent` (an inline draft) wins over `body.agent_id` (a saved agent); with
    neither, the default saved agent runs. An explicit selection that is invalid
    raises instead of falling back to the default.
    """
    agents = agents or store
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise AgentSelectionError("The call body must be a JSON object.")

    if "agent" in body:
        source, data = "agent", body["agent"]
    else:
        agent_id = body.get("agent_id", DEFAULT_AGENT_ID)
        source = f"agent '{agent_id}'"
        try:
            data = agents.load(agent_id)
        except (InvalidAgentId, AgentNotFound) as e:
            raise AgentSelectionError(str(e)) from e
        except (OSError, ValueError) as e:
            raise AgentSelectionError(f"Saved {source} can't be read: {e}") from e

    try:
        return AgentBuilder.from_dict(data, registry or default_registry())
    except AgentValidationError as e:
        raise AgentSelectionError(f"Invalid {source}: {e}", e.issues) from e


# ---- routes --------------------------------------------------------------------------


def _issues_response(
    issues: list[Issue], malformed: bool = False, status_code: int = 200
) -> JSONResponse:
    return JSONResponse(
        IssuesResponse(issues=issues, malformed=malformed).model_dump(mode="json"),
        status_code=status_code,
    )


async def _json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError as e:
        raise HTTPException(422, f"The request body is not valid JSON: {e}") from e


def build_router(agents: AgentStore) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    def path_for(agent_id: str) -> Path:
        try:
            return agents.path(agent_id)
        except InvalidAgentId as e:
            raise HTTPException(400, str(e)) from e

    @router.get("", response_model=list[AgentSummary])
    async def list_agents():
        return agents.list()

    # Declared before /{agent_id}, which would otherwise match it.
    @router.post("/validate", response_model=IssuesResponse)
    async def validate_agent(request: Request):
        config, issues = parse(await _json_body(request))
        if config is None:
            return _issues_response(issues, malformed=True)
        return _issues_response(validate(config, default_registry()))

    @router.get("/{agent_id}")
    async def get_agent(agent_id: str) -> Any:
        path_for(agent_id)
        try:
            return agents.load(agent_id)
        except AgentNotFound as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(500, f"Saved agent '{agent_id}' is not valid JSON: {e}") from e

    @router.put("/{agent_id}", response_model=IssuesResponse)
    async def put_agent(agent_id: str, request: Request):
        path_for(agent_id)
        config, issues = parse(await _json_body(request))
        if config is None:
            return _issues_response(issues, malformed=True, status_code=422)
        agents.save(agent_id, config)
        return _issues_response(validate(config, default_registry()))

    @router.delete("/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str):
        path_for(agent_id)
        try:
            agents.delete(agent_id)
        except AgentNotFound as e:
            raise HTTPException(404, str(e)) from e
        return Response(status_code=204)

    return router


def missing_env() -> list[str]:
    """The API keys a call needs that aren't set."""
    return [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]


def install(app: FastAPI, agents: Optional[AgentStore] = None) -> None:
    """Register the builder routes and the /start preflight on the runner's app."""
    agents = agents or store
    app.include_router(build_router(agents))

    @app.middleware("http")
    async def preflight_start(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/start":
            try:
                data = await request.json()
            except ValueError:
                return await call_next(request)  # malformed JSON: the runner reports it
            body = data.get("body") if isinstance(data, dict) else None
            try:
                select_agent(body, agents)
            except AgentSelectionError as e:
                return reject(str(e), e.issues)
            # Without this, the call would connect and then drop with no explanation.
            if missing := missing_env():
                return reject(
                    f"{', '.join(missing)} not set. Add the keys to backend/.env "
                    "(see backend/.env.example) and restart the backend."
                )
        return await call_next(request)


def reject(detail: str, issues: Sequence[Issue] = ()) -> JSONResponse:
    """A /start refusal, shaped like the builder API's errors."""
    return JSONResponse(
        {"detail": detail, "issues": [i.model_dump(mode="json") for i in issues]},
        status_code=422,
    )
