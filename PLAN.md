# Phase 1: Voice Agent Builder plan

Pipecat 1.4's runner already passes a per-call `body` from `/start` and `/api/offer` through to `bot(runner_args)`, and it exposes its FastAPI `app` so we can add our own routes. RTVI is on by default and already streams transcripts and function calls to the client. Keep that integration, but prove a browser call and reliable node tracking before migrating the schema or building the editor.

## 1. Architecture

```
frontend/ (Vite + React + TS)                 backend/ (existing Pipecat dev runner, port 7860)
┌───────────────────────────────┐            ┌──────────────────────────────────────────┐
│ Agent list │ Graph canvas │ Inspector │ ──▶│ /api/agents       CRUD (JSON files)      │
│            │ (React Flow) │ / Test call│    │ /api/agents/validate                     │
│            └──────────────┘            │ ──▶│ /start, /sessions/*, /api/offer (runner) │
│ Pipecat client-js + SmallWebRTC        │◀──▶│ bot(): body.agent | body.agent_id        │
└───────────────────────────────┘  RTVI  └──────────────────────────────────────────┘
```

- **Keep the Pipecat dev runner.** `app` is created at module level in `pipecat/runner/run.py`, and the runner's own routes and CORS are only added inside `main()`. So we register the agent API routes on `pipecat.runner.run.app` before calling `main()`, and a single process serves both the API and the WebRTC calls.
- **Each call picks its agent.** The UI calls `/start` with `body: {agent: <full config>}` (a snapshot of the current editor draft) or `body: {agent_id}` (a saved agent). `bot()` compiles that agent on every call, so edit → call works with no save and no restart. Editing and agent switching are disabled from validation through call teardown, so the canvas continues to match the running agent.
- **Storage:** one JSON file per agent in `backend/agents/`, seeded with `example.json` (moved from `backend/example_flow.json`). This keeps "agent as data" intact, is easy to diff, and a Phase 2 Copilot can write to the same files or call the same API.
- **Reproducible dependencies:** start with the verified `pipecat-ai==1.4.0` and `pipecat-ai-flows==1.4.0`, declare directly used dependencies such as Pydantic explicitly, and commit Python and frontend dependency locks. Update these deliberately after the integration checks pass.

## 2. Backend changes

1. **Schema (`schema.py`) moves to pydantic**
   - Swap the dataclasses for pydantic models. `model_validate` and `model_dump` replace the hand-written schema `from_dict` methods and the planned `to_dict`; keep `AgentBuilder.from_dict` as the public loading entry point.
   - Type the nested contract explicitly: `list[Node]`, `list[Edge]`, `list[str]` for required properties, and models for messages, positions and supported actions. Bare `list`/`dict` annotations would leave validation and generated frontend types too weak.
   - Preserve JSON-compatible message fields and property-schema keywords that the forms do not edit. Define an explicit policy for additional fields so model parsing/dumping does not silently discard imported data. Preserving data does not imply the runtime supports it; execution validation remains separate.
   - Add an optional `ui: {x, y}` field on `Node` for canvas position. The compiler ignores it.
   - `model_json_schema()` is the document contract. It generates the frontend's TypeScript types (`json-schema-to-typescript`, run by `make types`). Phase 2 can derive a provider-compatible structured-output schema from it; compatibility with that provider's supported JSON Schema subset is a separate check.
2. **Validation (`validation.py`)**
   - Separate structural parsing from graph/runtime validation. Structurally valid documents may contain graph errors and remain saveable; malformed JSON or incorrect field shapes cannot be committed to the document.
   - Add a pure `validate(config) -> list[Issue]`, where an issue is `{level, path, node?, edge?, message}` and `edge` is the function name. `path` identifies array indices and fields, so duplicate names still produce unambiguous issues. Today `_validate` raises on the first problem. Normalize structural parsing errors into the same issue shape for the UI.
   - Errors:
     - an unknown edge target, or a missing initial node
     - duplicate node names, or duplicate function names within a node
     - an invalid function name (`^[a-zA-Z0-9_-]+$`)
     - `required` entries that aren't in `properties`
     - property types that aren't valid JSON-schema types
     - unsupported or malformed actions that would fail at runtime
     - self-loops (an explicit Phase 1 restriction until the canvas supports them)
   - Warnings:
     - unreachable nodes
     - a non-end node with no edges (the call gets stuck there)
     - an end node that has edges
     - a node with no task messages
     - a node where both the persona and `role_message` are empty
   - `AgentBuilder` calls `validate()` and raises if there are any errors. Saving a draft never bypasses this runtime check.
3. **Builder fix (`builder.py`)**
   - Today, explicit `post_actions` suppress the implicit `end_conversation`, so a node with `end: true` and only non-terminal post-actions never hangs up. Copy the action list and append `{"type": "end_conversation"}` only when `end` is true and no termination action exists. Preserve existing actions and their order without mutating the source config or adding duplicate termination actions on repeated compilation.
4. **API (`backend/api.py`)**
   - `GET /api/agents`, `GET/PUT/DELETE /api/agents/{id}`, `POST /api/agents/validate`.
   - `PUT` is an upsert, so there's no separate create endpoint. It saves structurally valid documents even when graph/runtime validation reports errors, and returns `{issues}`. Structural errors return HTTP 422 with issues and leave the existing file untouched.
   - `/validate` provides the debounced live badge and a preflight check of the exact test-call snapshot. Position-only changes do not trigger validation. The frontend ignores stale responses using a document revision or request token.
   - `id` must match `^[a-z0-9_-]+$` (no path traversal). Files are written atomically: write a temp file in the same directory, then `os.replace`.
5. **`bot.py`**
   - Take `runner_args.body["agent"]` (inline draft) if it's present. Otherwise load `body["agent_id"]` from `backend/agents/`; use the example only when neither selector is supplied. An invalid inline config or unknown saved ID is an error, not a reason to run the example. Validate before creating transport/services where possible.
   - Live node tracking uses one custom RTVI message shape: `{type: "node_entered", from, node, via, state}`, sent with `flow_manager.worker.rtvi.send_server_message(...)`.
     - `from` is the previous node and `via` is the function that was called; both are null for the initial node. Together they identify the traversed edge even when function names repeat across nodes.
     - `state` is a JSON snapshot of `flow_manager.state`, containing collected arguments at that point.
   - Emit from a narrow runtime adapter that observes confirmed node activation. The edge handler only supplies transition metadata: returning `(result, next_node)` proposes a transition, and Pipecat applies it afterward. Do not report entry when activation fails. M1 must prove a compatible integration point after activation and before terminal shutdown; do not assume a public node-change callback exists.
   - Send the initial notification once flow initialization has succeeded and the RTVI client is ready. Coordinate these conditions explicitly instead of relying on transport connection event ordering.
   - Transcripts and function calls use the built-in `RTVIObserver`. Surface startup/transition failures in the call panel, release call resources on failure or disconnect, and verify that another call can start afterward.
6. **No production static serving.** It's a local tool, so the frontend runs on Vite in dev. If that's ever needed: mount `frontend/dist` at `/builder` only when it exists, and build with `base: '/builder/'`.

## 3. Frontend

**Stack:**

- Vite, React and TypeScript, with Tailwind for styling.
- `@xyflow/react` (React Flow) for the graph, and `@dagrejs/dagre` (the maintained fork) for auto-layout.
- Zustand with `zundo` for editor state and undo/redo.
- `@pipecat-ai/client-js` and `@pipecat-ai/small-webrtc-transport` for the call.

**Dev proxy:** Vite proxies `/api`, `/start` and `/sessions` to `:7860`. The SmallWebRTC flow is `/start`, then `/sessions/{id}/api/offer`.

**Data model:** `AgentConfig` in the Zustand store is the single persisted document. Types are generated from the backend's JSON Schema. Transient UI state lives alongside it, outside the document and its undo history.

- Transient state includes selection, viewport, field/raw-JSON buffers, validation status, connection status, transcript and active-node highlighting. These changes neither dirty the document nor create undo entries.
- `toFlow(config)` is a memoized selector that derives React Flow nodes and edges from the config.
  - Each node becomes a React Flow node, and each `node.edges[i]` becomes a React Flow edge with the ID `${node}::${function}`, labelled with its function name. IDs remain stable until a rename; rename transactions update references and selection together.
  - Run dagre once on load/import when positions are missing, preserving existing positions, or explicitly through "tidy layout". Materialize the resulting positions outside the selector. New nodes receive a position when created; editing prompts does not rerun layout.
- Drag/connect/delete events map to document actions; selection and viewport events update transient state. There is no reverse serialization of the whole React Flow graph (`fromFlow`). Use narrow selectors and memoized components so transcript or selection updates do not rebuild the document view.
- Use `zundo` for document history only. Group a drag into one undo operation and commit field edits as transactions rather than one entry per keystroke. Rename updates all references atomically; tidy layout is one transaction. Clear history on agent load/switch and track dirty state against the last saved document.
- Keep uncommitted field edits and malformed JSON in transient buffers with visible errors. Save, export and test-call actions must first commit valid buffers or show the blocking error; never silently use an older document instead of visible edits.
- Reject conflicting node/function rename commits while preserving the user's field buffer. Load/import documents with duplicate identifiers into a raw JSON repair view until identifiers are unique, rather than passing colliding IDs to React Flow. Structurally valid repair documents can still be saved with issues. Other graph errors remain visible in the inspector/issue list; dangling edges are omitted from the canvas until repaired.
- Imports and form edits preserve fields the UI does not understand. Raw JSON is the fallback for unsupported structures, not a reason to normalize or discard them.

**Layout (similar to Retell / ElevenLabs):**

- **Left:** the agent list (new, duplicate, delete) and a "Add node" button.
- **Center:** the canvas.
  - The start node gets a badge and end nodes are styled differently.
  - Dragging between handles creates an edge with an auto-generated name like `go_to_<target>`.
  - Delete removes the selection.
  - It uses React Flow's default edges. Self-loops are a validation error, and parallel edges between the same pair of nodes are allowed but may overlap (a custom edge component is a stretch goal).
  - There's a minimap and a "tidy layout" button.
- **Right inspector, which depends on the selection:**
  - **Agent settings:** name, persona (global role message), voice ID, model, initial node.
  - **Node:**
    - Name. Committing a unique name updates every edge target, `initial_node` and the current selection in one transaction.
    - Task messages: textareas for supported text `content`. New messages default to `role: "developer"`; editing existing messages preserves their roles and additional fields. Unsupported content structures use the raw JSON fallback.
    - A role message override.
    - An end-call toggle.
    - An "on entry, say…" field, which edits a `tts_say` pre-action without replacing other actions or changing their order.
    - A raw JSON editor for other pre/post actions, with structural and runtime issues surfaced before a call.
  - **Edge:**
    - Function name, description (when the LLM should call it) and target dropdown.
    - A table of the variables to collect: name, type (string/number/boolean/integer), description, optional enum and a required checkbox. This edits `properties`/`required` without rebuilding them from only visible fields. Nested objects, arrays, unions and other unsupported schema structures use raw JSON; unrelated constraints survive form edits.
- **Top bar:**
  - Save (Cmd/Ctrl+S, with an unsaved-changes dot).
  - Undo/redo (Cmd/Ctrl+Z, Shift+Cmd/Ctrl+Z).
  - A validation badge. Clicking an issue selects the offending node/edge or opens the corresponding field in the repair view using its `path`.
  - Import/export JSON.
  - A "Test call" button.
- **Test call panel (drawer):**
  - Clicking "Test call" commits valid field buffers, captures a draft snapshot and locks document editing and agent switching. Validate that exact snapshot, then connect with `body: {agent: <snapshot>}`. Errors block connection and release the lock; saving isn't required.
  - Keep the lock through connecting, connected and disconnecting states. Disable all document mutations, including undo/redo, rename, delete, import and layout. Selection, inspection and canvas navigation remain available.
  - While connected:
    - mic status and a hang-up button
    - a live transcript of user and bot turns, from the built-in RTVI transcription events
    - the active node highlighted on the canvas, with the path taken traced, from `node_entered`
    - collected state as key/value pairs, from `node_entered.state`
  - On normal hang-up, connection failure or server disconnect, release microphone/connection resources and the editing lock. Show the outcome, clear active highlighting, and reset transcript/path/state when starting the next call.

## 4. Milestones

Prove client-js, SmallWebRTC through the proxy and transition-event timing before schema migration, persistence or graph editing. M1 uses the existing example/schema and a minimal browser page; it does not require the agents API or React Flow.

| # | Deliverable | Done when |
|---|---|---|
| M1 | Browser integration: frontend scaffold, Vite proxy, inline draft selection, transcript, confirmed `node_entered` events and call cleanup | The browser runs an inline-modified example without saving; initial and transition events arrive after activation, failed activation reports no entry, and hang-up/failure permits another call |
| M2 | Contract and persistence: typed Pydantic models with `ui`, structural/graph validation, safe end-action fix, agents API, saved-agent selection, generated TS types and dependency locks | The example round-trips through the API; a structurally valid draft with graph errors saves but cannot run; malformed input leaves saved data intact; generated types match the schema |
| M3 | Graph editor: canvas, initial layout, inspector, add/delete/connect/rename, settings, save/load, validation and repair view, transactional undo, draft-call locking and live highlighting | A new 5-node agent can be built and tested before saving; graph and call stay aligned; rename/drag undo as single operations; forms preserve unsupported imported fields |
| M4 | Delivery: import/export workflow, shortcuts, layout controls/minimap, empty and error states, `make dev` (runs both servers with `make -j2`), README | A fresh clone works with `make install && make dev` after documented API-key setup; import/export preserves content and the manual end-to-end checks pass |

Stretch goals:

- node templates (greeting, collect info, confirm, transfer)
- a custom edge component for parallel edges and self-loops
- a call history log
- a demo agent built around `catalog.json`, as a bridge to Phase 2

## 5. Testing

- **pytest:**
  - structural parsing and graph/runtime validation, including issue paths for duplicate identifiers
  - end nodes preserve action order and append termination only when absent; repeated compilation does not mutate the source or duplicate actions
  - API CRUD, bad IDs, failed-write preservation, malformed input rejection, and an invalid graph that saves successfully but is rejected by the runtime
  - agent selection precedence (`agent` before `agent_id` before the default), with invalid explicit selections failing instead of silently choosing the example
  - runtime adapter integration through the real FlowManager transition path, using fake services/transport: initial readiness in either order, event only after activation, correct `from`/`via`/state, no entry on failed activation, and terminal entry before shutdown
  - worker cleanup on startup/transition failure or disconnect; a subsequent call remains possible
  - compilation of the checked-in example and known-valid fixtures, not every file in the user-writable drafts directory
- **vitest:**
  - document actions: atomic rename/reference updates, duplicate-name rejection, connect name generation and deletion of incoming edges
  - undo groups a drag or field commit into one operation; transient selection/call updates create no history entries; history resets when switching agents
  - `toFlow` keeps IDs stable across unrelated edits; prompt edits do not rerun layout; invalid identifiers open repair mode
  - form edits preserve existing message roles, additional fields, nested property schemas and unrelated actions
  - stale validation responses are ignored; position changes skip validation; invalid buffers block save/export/test rather than silently using stale data
  - call state locks document mutations from validation through teardown and releases the lock on failure/disconnect
- **Contract check:** regenerate frontend types and fail the check if committed output differs.
- **Manual end-to-end:** build an agent from scratch, test before saving, confirm transcript and node/edge/state tracking, hang up and call again. Exercise connection failure, invalid-draft save/repair, import/export preservation and keyboard shortcuts inside form fields.

## 6. Decisions (easy to change)

- **Test calls use a draft snapshot, sent inline.** You can test unsaved edits without writing to disk. The server validates independently. Document editing and agent switching stay locked until teardown; supporting concurrent editing against a separate call snapshot is deferred. `agent_id` remains available for saved agents and the Copilot.
- **One persisted document, separate transient state.** `AgentConfig` is the source of truth for save/export/compile, and the canvas is a view of it. Selection, field buffers and call telemetry remain outside document history.
- **Draft validity and run validity differ.** Structurally valid drafts can be saved with graph/runtime issues. Running requires no errors; malformed JSON stays in a visible repair buffer.
- **Node names stay as IDs**, as the current schema has them. Rename commits enforce uniqueness and update all references atomically. Imports with duplicate identifiers require repair before canvas rendering. This avoids a schema migration.
- **No database or auth.** It's a local tool, so files are enough.
- **The schema keeps Pipecat vocabulary**, with declarative edge targets and optional `ui` positions. The forms preserve data they do not expose. Pydantic JSON Schema is shared by the backend and frontend types; Phase 2 derives and verifies its structured-output contract from that source.
