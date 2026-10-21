# Prosper Challenge — Context Management

Voice AI for healthcare scheduling. An agent is a **graph of nodes** (Pipecat Flows), defined declaratively as JSON and compiled into a runnable voice pipeline.

- **Phase 1** — a UI to edit the node graph and place a test call.
- **Phase 2** — a context-management approach so a scheduling agent can reliably and cost-effectively navigate a large catalog of locations, doctors, and appointment types.

```
browser mic  ->  ElevenLabs STT  ->  OpenAI LLM  ->  ElevenLabs TTS  ->  browser
```

Pipecat's dev runner ships a **prebuilt browser client**, so the test-call UI comes for free — no frontend code to write yet.

## Quickstart

Requires **Python 3.11+**. Run from the repo root:

```bash
make install
make run
```

Open the URL it prints (default `http://localhost:7860/client`), click **Connect**, allow mic access, and talk to the agent. `Ctrl+C` to stop. (`make help` lists all targets.)\
\
Remember to update the `.env` file accordingly.

### Builder UI (Phase 1, in progress)

Requires **Node 20+**. With `make run` going, start the frontend in a second terminal:

```bash
make web    # http://localhost:5173
```

Edit the agent JSON and click **Test call**: the unsaved draft is sent inline with the call (`/start` body `{agent: ...}`), so there's no save or restart. The panel shows the live transcript, the active node and path (from `node_entered` RTVI server messages), and the collected state.

Agents are saved one JSON file per agent in `backend/agents/` through the builder API:

| Route | |
| --- | --- |
| `GET /api/agents` | Saved agents, `[{id, name}]`. |
| `GET /api/agents/{id}` | One saved agent document. |
| `PUT /api/agents/{id}` | Create or replace; returns `{issues}`. A malformed document is rejected (422) and the saved file is left untouched. A well-formed one with graph errors (e.g. an unknown edge target) is saved, so drafts can be repaired later, but it can't run. |
| `DELETE /api/agents/{id}` | Delete a saved agent. |
| `POST /api/agents/validate` | `{issues}` for a document, without saving it. |

Ids are 1–64 lowercase letters, digits, `_` or `-`. Each issue is `{level, path, node, edge, message}`, where `path` locates the value (e.g. `["nodes", 2, "edges", 0, "target"]`).

`make test` runs the backend tests. `make types` regenerates the frontend's TypeScript types (`frontend/src/generated/`) from the backend's JSON Schema, and `make check-types` fails if they're stale. Python dependencies install from `backend/requirements-dev.lock`; `make lock` re-resolves it with [uv](https://docs.astral.sh/uv/) after a deliberate change to `requirements*.txt`.

## Layout

| Path | Responsibility |
| --- | --- |
| `backend/bot.py` | The voice pipeline (WebRTC + ElevenLabs STT/TTS + OpenAI LLM). Loads an agent JSON via `AgentBuilder` and runs it. No graph logic lives here. |
| `backend/agent_builder/` | All agent-building code. `schema.py` = the declarative `AgentConfig` / `Node` / `Edge` contract (Pydantic models; unknown fields are preserved); `validation.py` = structural parsing and graph/runtime checks, reported as issues; `builder.py` = `AgentBuilder`, which refuses agents with errors and compiles the rest into a Pipecat Flows graph. |
| `backend/agent_builder/runtime.py` | `TrackingFlowManager` (reports confirmed node activations as `node_entered`) and `ClientReadyGate` (holds RTVI messages until the client is ready). |
| `backend/api.py` | Per-call agent selection, the `/start` preflight and the builder's `/api/agents` routes (file storage in `backend/agents/`), registered on the Pipecat runner's FastAPI app. |
| `backend/contract.py` | The backend/frontend JSON Schema contract (`make types`). |
| `backend/tests/` | pytest suite (`make test`). |
| `frontend/` | The builder UI (Vite + React + TS). Test calls use `@pipecat-ai/client-js` over SmallWebRTC through the Vite proxy. `src/generated/` holds the generated contract types. |
| `backend/agents/example.json` | The example agent **as data** — a clinic scheduler, and the agent a call runs when it selects none. The starting point for the Phase 2 context-management work. |
| `backend/data/catalog.json` | A deliberately large, deliberately messy clinic catalog (locations, providers, appointment types, booking rules) for the Phase 2 work. See [`backend/data/README.md`](backend/data/README.md). |

Each call picks its agent from the `/start` body: `{"agent": {...}}` runs that document inline, `{"agent_id": "..."}` runs a saved agent, and with neither, `agents/example.json` runs. An invalid inline agent or an unknown/invalid saved one is rejected at `/start` (HTTP 422, with `issues`) rather than falling back to the example.