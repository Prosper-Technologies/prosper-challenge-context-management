#
# Catalog API — the catalog screen's HTTP routes and the tool playground.
#
#   GET    /api/catalog                 the catalog, as loaded
#   GET    /api/catalog/lint            -> {issues}: catalog health checks
#   GET    /api/catalog/tools           -> {tools, guards}: tool schemas and guard metadata
#   POST   /api/catalog/sessions        -> {session_id, state, ttl_s}: a new playground session
#   DELETE /api/catalog/sessions/{id}   dispose of one (Reset is DELETE, then POST)
#   POST   /api/catalog/tools/{name}    {session_id, args} -> {result, state, approx_tokens}
#
# Tools run through the same registry as voice calls (catalog/tools.py), so argument
# validation and mutation rules can't drift; the playground's patient controls call
# update_booking like the model does. A tool's own failure (bad arguments, a
# rejected update, a booking error) is a 200 with an `{"error": ...}` result, which
# is exactly what the model would see. HTTP errors are about the request: an unknown
# tool or session (404), or a session that expired (410).
#
# Playground sessions live only here, each with its own calendar. A request can't
# reach a voice call's session (it lives in the call's flow state) or the voice
# calendar, and nothing here changes the cached catalog. A session expires after
# SESSION_TTL_S without a request; an expired or unknown id is refused rather than
# silently replaced by a new session. Everything is in memory.
#
# Importing this module creates no session, calendar or Pipecat service, so
# contract.py can build the schema from it.
#

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, create_model

from catalog import Calendar, Catalog, CatalogIssue, CatalogIndex, Policy, SchedulingSession, SessionState
from catalog import default_catalog, default_index, default_policy, lint
from catalog.tools import GUARDS, TOOLS, ToolContext, approx_tokens

SESSION_TTL_S = 30 * 60
MAX_SESSIONS = 100  # beyond this, the least recently used session expires early
MAX_TOMBSTONES = 1000  # expired ids remembered, to answer 410 rather than 404

ToolName = Literal[tuple(TOOLS)]
GuardName = Literal[tuple(GUARDS)]


class _Response(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class LintResponse(_Response):
    issues: list[CatalogIssue]


class ToolSpec(_Response):
    name: ToolName
    description: str
    # The argument schema the LLM is given (inline, no titles); forms are built from it.
    parameters: dict[str, Any]


class GuardSpec(_Response):
    name: GuardName
    description: str
    # Agents exposing these tools must end only through this guard.
    completes: list[ToolName]


class ToolsResponse(_Response):
    tools: list[ToolSpec]
    guards: list[GuardSpec]


class PlaygroundSession(_Response):
    session_id: str
    state: SessionState
    ttl_s: int  # seconds of inactivity before it expires


class ToolRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    args: Optional[dict[str, Any]] = None


class ToolRunResponse(_Response):
    # The tool's JSON result (ToolResults[name]) or a ToolErrorResult.
    result: dict[str, Any]
    state: SessionState  # the session after the call
    approx_tokens: int = Field(
        description="Estimated size of `result` (about 4 characters a token), not a tokenizer count."
    )


# Per-tool argument and result types, for the frontend's types only.
ToolArgs = create_model("ToolArgs", **{name: (t.args, ...) for name, t in TOOLS.items()})
ToolResults = create_model("ToolResults", **{name: (t.result, ...) for name, t in TOOLS.items()})


# ---- playground sessions -------------------------------------------------------------


class SessionNotFound(LookupError):
    pass


class SessionExpired(LookupError):
    pass


@dataclass
class _Entry:
    ctx: ToolContext
    last_used: float


class PlaygroundSessions:
    """Sandbox scheduling sessions, each booking in its own calendar."""

    def __init__(
        self,
        ttl_s: float = SESSION_TTL_S,
        limit: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        calendar: Optional[Callable[[], Calendar]] = None,
        policy: Optional[Policy] = None,
        index: Optional[CatalogIndex] = None,
    ):
        self.ttl_s = ttl_s
        self.limit = limit
        self._clock = clock
        self._calendar = calendar or (lambda: Calendar(default_catalog()))
        self._policy = policy
        self._index = index
        self._lock = threading.Lock()
        self._sessions: dict[str, _Entry] = {}
        self._expired: OrderedDict[str, None] = OrderedDict()

    def create(self) -> SessionState:
        with self._lock:
            now = self._sweep()
            while len(self._sessions) >= self.limit:
                oldest = min(self._sessions, key=lambda sid: self._sessions[sid].last_used)
                self._expire(oldest)
            session_id = f"play-{uuid.uuid4().hex[:12]}"
            session = SchedulingSession(self._policy or default_policy(), session_id)
            ctx = ToolContext(session, self._calendar(), self._index or default_index())
            self._sessions[session_id] = _Entry(ctx, now)
            return session.state

    def dispose(self, session_id: str) -> None:
        """Remove a session. Disposing of an expired one is a no-op."""
        with self._lock:
            self._sweep()
            if self._sessions.pop(session_id, None) is None and session_id not in self._expired:
                raise SessionNotFound(f"No playground session '{session_id}'.")

    def run(self, session_id: str, name: str, args: Any) -> tuple[dict, SessionState]:
        """Run a tool on the session; -> (result, the session's state afterwards)."""
        with self._lock:
            now = self._sweep()
            entry = self._sessions.get(session_id)
            if entry is None:
                if session_id in self._expired:
                    raise SessionExpired(
                        f"Playground session '{session_id}' expired after {self.ttl_s / 60:g} minutes "
                        "without a request. Start a new one."
                    )
                raise SessionNotFound(f"No playground session '{session_id}'.")
            entry.last_used = now
            # Under the lock, so two requests can't interleave inside one session's mutation.
            return TOOLS[name].run(entry.ctx, args), entry.ctx.session.state

    def __len__(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._sessions)

    def _sweep(self) -> float:
        now = self._clock()
        for session_id in [s for s, e in self._sessions.items() if now - e.last_used >= self.ttl_s]:
            self._expire(session_id)
        return now

    def _expire(self, session_id: str) -> None:
        del self._sessions[session_id]
        self._expired[session_id] = None
        while len(self._expired) > MAX_TOMBSTONES:
            self._expired.popitem(last=False)


playground = PlaygroundSessions()


# ---- routes --------------------------------------------------------------------------


@cache
def _catalog_json() -> dict:
    return default_catalog().model_dump(mode="json")


@cache
def _lint_json() -> dict:
    return LintResponse(issues=lint(default_catalog(), default_policy())).model_dump(mode="json")


@cache
def _tools_json() -> dict:
    tools = [ToolSpec(name=t.name, description=t.description, parameters=t.parameters()) for t in TOOLS.values()]
    guards = [GuardSpec(name=g.name, description=g.description, completes=list(g.completes)) for g in GUARDS.values()]
    return ToolsResponse(tools=tools, guards=guards).model_dump(mode="json")


def build_router(sessions: PlaygroundSessions) -> APIRouter:
    router = APIRouter(prefix="/api/catalog")

    # The catalog and its derived data never change while the server runs.
    @router.get("", response_model=Catalog)
    def get_catalog():
        return JSONResponse(_catalog_json())

    @router.get("/lint", response_model=LintResponse)
    def get_lint():
        return JSONResponse(_lint_json())

    @router.get("/tools", response_model=ToolsResponse)
    def list_tools():
        return JSONResponse(_tools_json())

    @router.post("/sessions", response_model=PlaygroundSession, status_code=201)
    def create_session():
        state = sessions.create()
        return PlaygroundSession(session_id=state.id, state=state, ttl_s=int(sessions.ttl_s))

    @router.delete("/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str):
        try:
            sessions.dispose(session_id)
        except SessionNotFound as e:
            raise HTTPException(404, str(e)) from e
        return Response(status_code=204)

    @router.post("/tools/{name}", response_model=ToolRunResponse)
    def run_tool(name: str, body: ToolRunRequest):
        if name not in TOOLS:
            raise HTTPException(404, f"No catalog tool '{name}'.")
        try:
            result, state = sessions.run(body.session_id, name, body.args)
        except SessionNotFound as e:
            raise HTTPException(404, str(e)) from e
        except SessionExpired as e:
            raise HTTPException(410, str(e)) from e
        return ToolRunResponse(result=result, state=state, approx_tokens=approx_tokens(result))

    return router
