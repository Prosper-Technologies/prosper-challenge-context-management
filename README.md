# Prosper Challenge — Context Management

Voice AI for healthcare scheduling. An agent is a **graph of nodes** (Pipecat Flows), defined declaratively as JSON and compiled into a runnable voice pipeline.

- **Phase 1** — a UI to edit the node graph and place a test call.
- **Phase 2** — a context-management approach so a scheduling agent can reliably and cost-effectively navigate a large catalog of locations, doctors, and appointment types.

```
browser mic  ->  ElevenLabs STT  ->  OpenAI LLM  ->  ElevenLabs TTS  ->  browser
```

## Quickstart

Requires **Python 3.11+** (3.12 is what's tested) and **Node 20.19+ or 22.12+** (Vite 8's minimum). From the repo root:

```bash
make install   # backend venv + frontend packages; creates backend/.env from .env.example
```

Put your API keys in `backend/.env` (both are needed for a test call; editing works without them):

```bash
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=...
```

Then start everything:

```bash
make dev       # backend on :7860 + builder on :5173; Ctrl+C stops both
```

Open **http://localhost:5173**. The example agent opens on the canvas; click **Test call**, allow mic access and talk to it. The page waits for the backend if it's still starting. `make help` lists all targets; `make run` and `make web` start the two halves separately, and Pipecat's prebuilt client is still at `http://localhost:7860/client` (it calls the example agent).

## The builder (Phase 1)

Saved agents are on the left, the node graph is in the middle, and the inspector for the selected agent, node or edge is on the right.

- **Build:** "+ Add node", then drag from a node's bottom handle to another node to connect them (the edge is named `go_to_<target>`). Select a node or edge to edit it; Delete/Backspace removes the selection. Renaming a node updates every edge and the initial node.
- **Fields commit on blur or Enter** (Escape reverts). Each commit, drag, rename, delete or tidy is one step for **Undo/Redo**.
- **Layout:** new nodes keep the position you give them. **Tidy layout** (top right of the canvas) re-arranges every node top to bottom as one undoable step. The minimap shows the start node, end nodes and, during a call, the active node.
- **Validation** runs as you edit. The top-bar badge lists issues, and clicking one selects what it points at. Saving works with graph errors; a test call doesn't.
- **JSON view** shows the whole document. Agents whose node or function names collide, or that aren't well-formed agents, open there (the canvas keys elements by name) until they're repaired.
- **Import / Export:** Export downloads the open agent as `<id>.json`, unknown fields and canvas positions included. Import opens a JSON file as a new, unsaved agent named after the file (`intake.json` → `intake`, or `intake-2` if that's taken); Save writes it to `backend/agents/`. A file that isn't a well-formed agent opens in the JSON view to repair.
- **Test call** sends the current draft inline with the call (`/start` body `{agent: ...}`), so you don't need to save or restart. Editing is locked until the call ends. The canvas highlights the active node and the edges taken (from `node_entered` RTVI server messages), and the panel shows the live transcript and the collected state.

### Keyboard shortcuts

| Keys (Ctrl instead of ⌘ off macOS) | |
| --- | --- |
| ⌘S | Save, committing the field you're typing in first. Works inside fields. |
| ⌘Z / ⇧⌘Z (also Ctrl+Y) | Undo / redo a document change. Inside a text field these are the browser's own text undo, so they don't undo the document behind what you're typing. |
| Delete / Backspace | Delete the selected node or edge (on the canvas). |
| Enter / Escape | Commit / revert a single-line field. |

### Troubleshooting

- **"Can't reach the backend"**: the Python side isn't running (or is still starting). `make dev` starts it; the page retries every few seconds.
- **"OPENAI_API_KEY ... not set"** when starting a call: fill in `backend/.env` and restart `make dev`. The keys are read when the backend starts.
- **The call connects but you hear nothing**: check that the browser has mic permission for `localhost:5173` and that the tab isn't muted.

### Builder API

Agents are saved one JSON file per agent in `backend/agents/` through the builder API:

| Route | |
| --- | --- |
| `GET /api/agents` | Saved agents, `[{id, name}]`. |
| `GET /api/agents/{id}` | One saved agent document. |
| `PUT /api/agents/{id}` | Create or replace; returns `{issues, malformed}`. A malformed document is rejected (422) and the saved file is left untouched. A well-formed one with graph errors (e.g. an unknown edge target) is saved, so drafts can be repaired later, but it can't run. |
| `DELETE /api/agents/{id}` | Delete a saved agent. |
| `POST /api/agents/validate` | `{issues, malformed}` for a document, without saving it. `malformed` means the issues are shape errors and it can't be saved. |
| `POST /start` | The Pipecat runner's call route, preflighted: an invalid agent selection or a missing API key is a 422 with a `detail` message, before any WebRTC setup. |

Ids are 1–64 lowercase letters, digits, `_` or `-`. Each issue is `{level, path, node, edge, message}`, where `path` locates the value (e.g. `["nodes", 2, "edges", 0, "target"]`).

### The scheduler agent (Phase 2)

`backend/agents/scheduler.json` books against the clinic catalog through tools instead of a catalog in the prompt: greeting → find_service → choose_provider_location → offer_times → confirm → success, with a `handoff` end node for anything it can't book. Open it in the builder and test-call it like any agent.

- **Node tools.** A node's `tools` lists the catalog tools it exposes besides its edges (`search_services`, `find_providers`, `find_locations`, `update_booking`, `check_booking`, `get_availability`, `book_appointment`, `catalog_overview`). A tool runs without leaving the node; when it changes the booking, the call panel's collected state updates from a `state_updated` server message (not a path step).
- **Edge guards.** An edge's optional `guard` names a check that must pass first (`service_chosen`, `booking_ready`, `slot_selected`, `booking_completed`). A failed guard keeps the node active and tells the model what's missing. `success` is entered only through `booking_completed`, i.e. after a real reservation.
- **Validation** knows the registry: unknown tools or guards, a tool that clashes with an edge function, and an agent that books without a guarded success end node are errors.
- **In the editor:** a node's inspector has a Tools section (one checkbox per catalog tool), an edge's inspector has a Guard selector, and canvas nodes show an "N tools" badge. Each change is one undo step.
- Bookings are held in memory in one calendar shared by voice calls, and reset when the backend restarts.

### The catalog screen (Phase 2)

**Catalog** in the top bar switches to a read-only view of `backend/data/catalog.json`; **Agents** switches back. The editor stays as you left it, unsaved or rejected field text included, and a test call keeps running across the switch. Editor shortcuts and the canvas's Delete key only act on the agents screen.

- **Tables** for locations, providers and appointment types, with a filter. Links cross the tables: a provider's location chips open that location, and the counts ("12 providers", "18 types") filter the other table by that record.
- **Lint** lists the booking rules and the catalog's health issues (unbookable types, new-patient dead ends, duplicate names, "Dr." on NPs and PAs, confusable locations, unused capabilities). Clicking an issue opens the record; rows with issues have a dot.
- **Tool playground** (right) runs the scheduler's tools in a sandbox session with its own calendar. Pick a tool, fill in the form generated from its schema (empty fields are left out; "null" sends an explicit null), and see the JSON result the model would get, with an approximate token count. Patient controls (Unknown/Yes/No) and the draft view sit above the form. Reset starts a fresh sandbox; an expired session keeps its last result on screen until you reset.

### Catalog API (Phase 2)

The catalog screen and its tool playground use these routes. The playground runs the same tools as a voice call, in a sandbox session with its own calendar:

| Route | |
| --- | --- |
| `GET /api/catalog` | The catalog, as loaded from `backend/data/catalog.json`. |
| `GET /api/catalog/lint` | `{issues}`: catalog health checks, each `{level, code, path, refs, message}`. |
| `GET /api/catalog/tools` | `{tools, guards}`: each tool's name, description and argument schema (the one the LLM gets), and each guard's name, description and the tools it `completes`. |
| `POST /api/catalog/sessions` | A new playground session: `{session_id, state, ttl_s}`. |
| `DELETE /api/catalog/sessions/{id}` | Dispose of a session. Reset is a delete followed by a new session. |
| `POST /api/catalog/tools/{name}` | Run a tool: `{session_id, args}` → `{result, state, approx_tokens}`. A tool's own failure (bad arguments, a rejected update) is a 200 whose `result` is `{error}`, as the model would see it. An unknown tool or session is a 404, and an expired session a 410. |

- Sessions expire after 30 minutes without a request (and the least recently used one expires early past 100 sessions). An expired id is refused, never replaced by a fresh session.
- Playground bookings stay in their sandbox: they never reach a voice call's session or the voice calendar, and reset on restart.
- `approx_tokens` is an estimate (about 4 characters a token) of the result's size, not a tokenizer count.

### Development

`make test` runs the backend (pytest) and frontend (vitest) tests. `make types` regenerates the frontend's TypeScript types (`frontend/src/generated/`) from the backend's JSON Schema, and `make check-types` fails if they're stale. Python dependencies install from `backend/requirements-dev.lock`; `make lock` re-resolves it with [uv](https://docs.astral.sh/uv/) after a deliberate change to `requirements*.txt`.

## Layout

| Path | Responsibility |
| --- | --- |
| `backend/bot.py` | The voice pipeline (WebRTC + ElevenLabs STT/TTS + OpenAI LLM). Loads an agent JSON via `AgentBuilder` and runs it. No graph logic lives here. |
| `backend/agent_builder/` | All agent-building code. `schema.py` = the declarative `AgentConfig` / `Node` / `Edge` contract (Pydantic models; unknown fields are preserved); `validation.py` = structural parsing and graph/runtime checks, reported as issues; `builder.py` = `AgentBuilder`, which refuses agents with errors and compiles the rest into a Pipecat Flows graph. |
| `backend/agent_builder/runtime.py` | `TrackingFlowManager` (reports confirmed node activations as `node_entered`, and tool state changes as `state_updated`) and `ClientReadyGate` (holds RTVI messages until the client is ready). |
| `backend/agent_builder/registry.py` | The `Registry` of tools and edge guards an agent may name. It is injected into the builder and validator, so the generic compiler doesn't import the catalog. |
| `backend/api.py` | Per-call agent selection, the `/start` preflight and the builder's `/api/agents` routes (file storage in `backend/agents/`), registered on the Pipecat runner's FastAPI app. |
| `backend/catalog/` | The Phase 2 catalog domain, with no Pipecat or HTTP dependency: `models.py` (the typed, immutable catalog and caller facts), `index.py` (free-text matching with aliases, ambiguity and paging), `policy.py` (the booking rules: feasible options, violations with fixes, the next question), `availability.py` (a deterministic mock calendar with atomic, idempotent reservations), `session.py` (one caller's facts, draft, offers and booking), `tools.py` (the scheduler's tools and edge guards: argument and result models, handlers and LLM schemas, shared by every adapter), `lint.py` (catalog health checks) and `scenarios.py` (fixtures for the tricky cases). |
| `backend/catalog_api.py` | The HTTP adapter: the `/api/catalog` routes, and the playground's sandbox sessions, each with its own calendar. |
| `backend/catalog_voice.py` | The voice adapter: binds the catalog tools and guards into a `Registry`, keeping each call's session in `flow_manager.state["patient"]` and `["booking"]`. |
| `backend/contract.py` | The backend/frontend JSON Schema contract (`make types`). |
| `backend/tests/` | pytest suite (`make test`). |
| `frontend/` | The builder UI (Vite + React + TS). `src/editor/` holds the document model: pure document actions, the undoable Zustand store, layout, validation, keyboard shortcuts and the agent flows (open, save, import/export, test call). `src/components/` holds the canvas (React Flow), inspector, JSON view, call panel and the catalog screen (tables, lint, tool playground). `src/catalog/` holds the catalog screen's logic: shared loading of the catalog routes, tool forms generated from tool schemas, and lint/cross-link navigation. Test calls use `@pipecat-ai/client-js` over SmallWebRTC through the Vite proxy. `src/generated/` holds the generated contract types. |
| `backend/agents/example.json` | The example agent **as data** — a clinic scheduler, and the agent a call runs when it selects none. The starting point for the Phase 2 context-management work. |
| `backend/agents/scheduler.json` | The Phase 2 scheduler: the catalog tools on each node and guarded transitions to a booking. |
| `backend/data/catalog.json` | A deliberately large, deliberately messy clinic catalog (locations, providers, appointment types, booking rules) for the Phase 2 work. See [`backend/data/README.md`](backend/data/README.md). |

Each call picks its agent from the `/start` body: `{"agent": {...}}` runs that document inline, `{"agent_id": "..."}` runs a saved agent, and with neither, `agents/example.json` runs. An invalid inline agent or an unknown/invalid saved one is rejected at `/start` (HTTP 422, with `issues`) rather than falling back to the example, and so is any call while an API key is missing.