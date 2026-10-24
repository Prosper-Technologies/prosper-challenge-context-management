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

---

# Phase 2: Context management plan

Status: M5–M9 are implemented (see [solution.md](solution.md#phase-2-context-management)); the benchmark's results are in [backend/eval/results.md](backend/eval/results.md). The rationale and trade-offs are recorded in [solution.md](solution.md#phase-2-context-management). M9 merged the scheduler's `find_service` and `choose_provider_location` nodes into one `choose_appointment` node; the M6 description below is the original plan.

## 1. Problem

The scheduling agent must book against `backend/data/catalog.json`: 8 locations, 50 providers, 82 appointment types and 6 policies, about 10k tokens when minified. The JSON is clean, but its meaning is messy:

- **Repeated names.** Three pairs of different people share a full name (Maria Garcia, Linda Ramirez, Michael Sato). "Dr. Chen" matches 7 providers. 13 NPs and PAs carry "Dr." in their names.
- **Near-duplicate services and confusable locations.** There are five "physical" variants and the MRI family. Mission Bay and Mission District, and North Beach and North Gate, are easy to confuse.
- **Capability gating.** In 31 provider/service pairs, the provider works at a location where that service can't be done. Each Radiology imaging type has two valid (provider, location) pairs; Echocardiogram has four. These are catalog combinations, not calendar slots. There are 760 valid static (type, provider, location) triples overall.
- **Dead ends.** Eight types (ophthalmology, PT, urology) are offered by no provider. OB/GYN and Endocrinology new-patient visits fail because the only provider isn't accepting new patients.
- **Misleading `specialty` labels.** Appointment-type specialties don't match provider specialties (215 mismatched links), so filtering by specialty gives wrong answers.

## 2. Approach

**The tools-based agent never receives the full catalog.** It handles the conversation; deterministic code handles catalog matching, feasibility and booking. The naive evaluation baseline receives the full catalog for comparison.

1. **Policy engine.** The 6 rules are encoded as code and checked before reservation. The LLM still has to interpret the caller correctly; rejecting an invalid booking is separate from choosing the appointment the caller intended.
2. **Narrow, structured tools.** They return a few ranked candidates with stable IDs, ambiguity and truncation metadata, and a way to refine or continue every truncated search.
3. **Narrowing loop.** The engine considers the full set of matching, feasible (service, provider, location) options and tells the agent which single question to ask next. Unknown patient facts trigger questions rather than silently passing or failing policy checks.
4. **Self-correction.** Booking re-checks everything and returns structured violations with suggested fixes. Correcting patient facts or choices invalidates incompatible draft fields, offered slots and confirmation.
5. **Node-scoped tools.** Each graph node exposes only the tools it needs, which keeps tool schemas small too.
6. **Explicit session state.** Typed patient facts and the booking draft are serialized in `flow_manager.state` for the lifetime of a call. They do not depend on conversation history and are not persisted across process restarts.

Decisions:

- The catalog UI is read-only. Its tool playground has its own resettable scheduling session and calendar, isolated from voice calls.
- Evaluation is pytest scenarios plus a live LLM benchmark against the naive "dump the catalog" baseline. Both modes use the same booking validator and equivalent fresh calendars.
- Matching is lexical plus curated aliases, with no new dependencies.
- Cache immutable catalog data, normalized names, aliases, ID maps and static feasibility once. Filter the 760 triples directly before adding more elaborate indexes. Patient state and reservations never enter the shared catalog cache.
- Keep the domain independent of Pipecat: pure matching and policy code, a typed scheduling session, and one tool registry. Thin voice, HTTP and evaluation adapters use the same handlers and argument models. Inject the registry and named transition guards into the generic builder and validator.
- Measure total prompt growth, tool round trips and latency before changing context retention. Node-scoped tools and small results do not by themselves bound accumulated conversation history.

## 3. Milestones

### M5 — Catalog domain and policy engine (`backend/catalog/`)

- **`models.py`:** Pydantic `Location`, `Provider`, `AppointmentType`, `Catalog`, `PatientFacts` and `BookingDraft`; `load_catalog(path)` and a cached `default_catalog()`.
  - Validate unique IDs and cross-references when loading the catalog.
  - `is_new_patient` and `has_referral` are `bool | None`, with `None` meaning unknown. Missing status never means an existing patient or a referral on file. These are simulated caller facts, not an integration with patient records.
- **`session.py`: `SchedulingSession`**, holding typed patient facts, draft choices, draft revision, offered slots and any completed reservation.
  - A single update operation applies explicit caller choices and corrections. Omitted fields stay unchanged; explicit null clears a field. Searches and feasibility checks do not select the first match or silently replace a requested provider.
  - Recompute feasibility after every change. Preserve compatible choices and clear stale, incompatible downstream choices. Keep an explicitly requested but infeasible combination in the draft so `explain()` can report its violations; do not erase the request or silently choose a replacement. Reject malformed updates and unknown IDs atomically.
  - Increment the draft revision and clear confirmation for each effective change. Changing patient eligibility or service/provider/location invalidates offers and the selected slot. Selecting a slot from a current offer validates it and carries that offer forward to the new revision, so selection itself does not make the offer stale.
  - Corrections must work from every nonterminal scheduler node. Keep a completed reservation separate from an editable draft; cancellation and rescheduling are outside this phase.
- **`index.py`: `CatalogIndex`**, which matches free text to catalog entries.
  - Normalisation: lowercase, drop punctuation, titles and the words "clinic"/"center".
  - Ranked lookup in this order: exact name, alias, token overlap, then a `difflib` ratio.
  - Use a minimum score for a match and deterministic tie-breaking by ID. An unrelated query returns no match instead of the least-bad candidate.
  - Compute ambiguity over all matches before truncation. Return `total_matches`, `truncated`, a clarification field such as specialty/location, and a continuation cursor where needed. A five-result cap must not hide two of the seven Chen providers permanently.
  - Apply explicit filters before capping results. Keep provider specialty and service specialty distinct; never infer provider eligibility by comparing those labels. Explain infeasible named requests rather than substituting a different person.
  - Aliases live in `backend/data/catalog_aliases.json`: service phrases ("checkup", "colonoscopy", "teeth cleaning", "knee MRI"), location nicknames and spoken name variants.
- **`policy.py`:**
  - Precomputes every valid (type, provider, location) triple for rules 1–3.
  - `feasible()` applies known patient facts and reports unresolved eligibility separately. Unknown facts are not false; ask about a referral when the selected service requires one.
  - `explain()` returns violations, each with a code, a message and a fix. The codes are `PROVIDER_NOT_AT_LOCATION`, `PROVIDER_DOESNT_OFFER`, `LOCATION_LACKS_CAPABILITY`, `REFERRAL_REQUIRED`, `TYPE_NOT_FOR_NEW_PATIENTS`, `PROVIDER_NOT_ACCEPTING` and `NOT_OFFERED`.
  - `narrow()` returns `ready | needs_choice | blocked`, up to 3 options, truncation metadata and a `next_question` identifying the missing fact or choice. `ready` requires a resolved service, provider and location plus all facts needed to establish eligibility. It does not mean a time is selected or a booking exists.
- **`availability.py`:** a deterministic mock calendar.
  - Use a stable digest of (provider, location, date), an injectable clock and the clinic timezone (`America/Los_Angeles`). Slots fall Mon–Fri 08:00–17:00, are sized by service duration and cover 14 local calendar days. Do not use Python's process-randomized `hash()` as a seed.
  - A slot identifies the service, provider, location, start and end. Validate that it was offered for the current draft revision and remains in the valid time window.
  - Store occupied provider time intervals across services and locations. A 45-minute reservation blocks every overlapping candidate, even one with a different slot ID or at another clinic.
  - Make revalidation and reservation atomic within the single-process calendar. Derive an idempotency key from the session and confirmed draft revision so a retry returns the existing receipt rather than booking twice.
  - Share the voice calendar across calls in the server process; give playground sessions and evaluation runs independent calendars. All bookings are in memory and reset on restart.
- **`lint.py`:** catalog health checks, reported as `Issue`-shaped results.
  - Unbookable types and new-patient dead ends.
  - Duplicate names and "Dr." on NP/PA titles.
  - Confusable location names, and a PT capability with no PT provider.
- **Scenario fixtures:** establish caller facts, intended outcomes and deterministic assertions for the tricky catalog cases, ambiguity, corrections and overlapping reservations now. Reuse them in M9; policy tests are part of M5 rather than deferred until after the UI.

### M6 — Tools and builder integration

- **`catalog/tools.py`:** one registry of names, descriptions, Pydantic argument/result models and handlers accepting a scheduling session. It has no Pipecat or HTTP dependency. Generate tool schemas, API discovery and playground fields from these definitions; do not duplicate argument validation in adapters.
- Candidate collections are capped at 5 items (3 for narrowing and availability), with `ambiguous`, `total_matches`, `truncated`, continuation and `next_question` metadata where applicable. Count the actual serialized result size as well as the number of items. Tools return compact projections rather than nested catalog entities.

| Tool | Purpose |
|---|---|
| `catalog_overview(cursor?)` | Compact, paged overview of bookable specialties and locations, targeting about 200 tokens per response. |
| `search_services(query, specialty?, cursor?)` | Ranked types with referral/new-patient flags and distinguishing notes. |
| `find_providers(name?, service_id?, location_id?, language?, specialty?, cursor?)` | Candidates with what tells same-name providers apart; specialty filters the provider's own label. |
| `find_locations(query?, service_id?, provider_id?, cursor?)` | Feasible locations, preserving unresolved patient eligibility as an explicit condition. |
| `update_booking(patient?, service_id?, provider_id?, location_id?, slot_id?)` | Record explicit facts/choices or corrections, apply invalidation, and return the revised draft and next question. |
| `check_booking()` | Read-only `policy.narrow` on the current draft, with violations and fixes. |
| `get_availability(day?, part_of_day?)` | Up to 3 slots for the current draft; refuses unresolved or invalid eligibility and associates offers with its revision. |
| `book_appointment(draft_revision, confirmed)` | Require explicit caller confirmation of the current draft and selected slot; revalidate and atomically reserve, returning a booking receipt. |

- The voice adapter reads/writes the typed session through JSON-compatible `state["patient"]` and `state["booking"]`. Catalog tools own those fields; generic edge arguments cannot overwrite them. Searches are read-only, availability records offers, and explicit updates/booking own their respective mutations.
- **Schema:** add `Node.tools: list[str]` and an optional named `Edge.guard`. Registry metadata defines valid tool and guard names; scheduler guards include `booking_ready`, `slot_selected` and `booking_completed`.
- **Validation:** an unknown tool/guard, a tool that clashes with an edge function name, or a duplicate tool is an error. Agents without tools/guards keep their existing behavior. The scheduler's success terminal must have a `booking_completed` guard on every incoming edge.
- **Builder:** inject registry/guard dependencies rather than importing the catalog into the generic compiler. Each catalog tool becomes a `FlowsFunctionSchema` whose handler returns `(result, None)`. Check edge guards before state mutation or transition; a failed guard returns structured recovery information and leaves the node active.
- **New agent `agents/scheduler.json`:** greeting → find_service → choose_provider_location → offer_times → confirm → success, plus a `handoff` end node for blocked cases. `confirm` is nonterminal and exposes booking; `success` is reachable only with a receipt from a successful reservation. Corrections can return to earlier steps. `example.json` stays the default.
- Add a session-state RTVI update after tool mutations: the current `node_entered` snapshot alone cannot show changes from tools that stay in the same node. State updates must not add graph traversal events.

### M7 — API and contract

- **`catalog_api.py`:**
  - `GET /api/catalog`
  - `GET /api/catalog/lint`
  - `GET /api/catalog/tools` returns tool schemas and named guard metadata.
  - `POST /api/catalog/sessions` creates an isolated playground session with its own calendar and returns its ID and initial state.
  - `DELETE /api/catalog/sessions/{id}` disposes a playground session; Reset disposes it and creates a fresh one. Expire abandoned sessions after inactivity.
  - `POST /api/catalog/tools/{name}` with `{session_id, args}`, returning `{result, state, approx_tokens}`. Patient controls call `update_booking`, using the same mutation rules as voice calls. Reject unknown or expired session IDs rather than silently replacing their state.
- The HTTP adapter uses the shared registry, but can access only playground sessions/calendars. Requests never attach to a voice-call session or mutate the cached catalog. Keep token estimates clearly labeled; evaluation uses actual provider usage.
- **Contract:** add catalog, session, tool argument/result and lint models to `contract.py`, then run `make types`. Schema generation must not instantiate sessions, calendars or voice services.

### M8 — Frontend

- **`api.ts`:** add `getCatalog`, `getCatalogLint`, `listTools`, playground session creation/disposal, and `runTool`.
- **Screen switch:** an `agents | catalog` toggle in `TopBar`, using local shell state unless more consumers need it. Keep call ownership, audio and call controls above the switch. Preserve pending editor buffers on navigation (retain the editor subtree, or block unmount when a buffer cannot commit), and scope editor shortcuts to the agents screen.
- **`CatalogView`:**
  - Tabs for Locations, Providers and Appointment types, as filterable cross-linked tables.
  - `LintPanel`, reusing the presentation of `IssueList`. Extract a renderer with `onIssueClick(issue)`; keep agent-selection mapping in an editor wrapper and catalog navigation in the lint panel.
  - `ToolPlayground`: a form generated from each tool's schema, patient controls with explicit Unknown/Yes/No values, current draft/revision, JSON result and an approximate token count. Create a sandbox session on entry and expose Reset; explain session expiry without discarding the displayed result silently.
- **Inspector:**
  - A "Tools" section in `NodeInspector` with one checkbox per tool, saved through a new `setNodeTools` document action.
  - An "N tools" badge on the canvas node.
  - An optional guard selector in the edge inspector, populated from registry metadata and saved as one undoable document action.

### M9 — Evaluation and documentation

- **pytest:**
  - Extend the M5 fixtures: ambiguous names/locations, aliases, unrelated queries, stable ranking, and reaching all seven Chen matches despite the output cap.
  - A policy table of the tricky cases:
    - Ramirez + MRI → only prov_014 at loc_004;
    - Echo with David Chen at Richmond → a capability violation, with the fix;
    - dental at North Gate → blocked;
    - new patient + OB/GYN → not accepting;
    - new patient + X-ray → blocked, mammogram → ok;
    - ophthalmology → not offered;
    - a multi-location provider is never `ready` before the location is chosen.
  - Unknown patient facts never bypass eligibility; correcting facts/choices preserves compatible fields and clears stale slots/confirmation. Invalid updates cannot partially mutate state.
  - Cross-service and cross-location interval conflicts, stale offers, concurrent reservation attempts and idempotent booking retries, all with a fixed clock and fresh calendars.
  - Tool output caps and continuation, non-transition state updates, failed guards leaving the current node intact, and success-terminal entry requiring a booking receipt.
  - Lint, tool/guard validation and compilation, API contract, playground reset/expiry/isolation, and identical domain results through voice, HTTP and evaluation adapters.
- **vitest:** tool/guard document actions, tools/guards preserved through import/export, issue-click routing, and shortcut scoping. Verify screen switching with invalid buffers and an active call in the browser.
- **`make eval`** (`backend/eval/`, needs `OPENAI_API_KEY`):
  - About 20 scenarios, each with explicit caller facts, allowed outcomes and corrections where relevant. Use deterministic outcome checks against the actual reservation/session state; fluent confirmation text alone is not success. A simulated caller (initially gpt-4o-mini) follows the fixture and does not choose the expected result.
  - Two modes: `naive` (the full catalog in the prompt) and `tools`. Use the same scheduler model/settings, caller fixture, patient-state mutations, booking validator, simulated date and initial calendar availability; vary catalog access. Give every run a fresh session/calendar and the same turn limit.
  - With k=3 runs per scenario, record intended-outcome correctness, invalid booking attempts, guardrail rejections, completed bookings, actual policy violations, turns, tool calls, prompt growth and latency. Zero policy violations alone does not establish correct matching.
  - Record actual input/output/cached token usage where supplied, explicit model identifiers, catalog version and the pricing assumptions behind estimated cost. Report simulator usage separately from scheduler usage. Retain machine-readable traces and write the comparison to `backend/eval/results.md`.
  - Include repeated searches and late corrections to measure accumulated context. If growth materially hurts cost or latency, experiment with a deterministic state summary at node boundaries, preserving unresolved preferences and recent caller context. Compare outcome correctness before adopting context resets; do not add an LLM summarization call by default.
- **Docs:** extend the Phase 2 design rationale in `solution.md` with measured results, actual trade-offs and remaining work, and update `README.md`. Keep proposed optimizations distinct from implemented behavior and benchmark evidence.

## 4. Verification

1. `make test`, `make check-types` and `npm --prefix frontend run typecheck` pass.
2. In `make dev`:
   - The Catalog screen shows the tables and the lint panel (unbookable types, dead ends, duplicate names).
   - After setting the draft through `update_booking`, the playground's `check_booking` reports the Echo / David Chen / Richmond violation with its fix.
   - A playground booking affects only its sandbox; Reset restores it. Voice-call reservations remain independent.
   - Node tools and edge guards survive a save and a re-import. Catalog lint clicks navigate catalog records, and editor shortcuts do not affect a hidden editor.
   - Switching screens preserves invalid editor buffers, and call controls/audio remain available during an active call.
3. On a voice call with `scheduler`:
   - "New patient, Dr. Garcia, a checkup" leads to disambiguation and then a location question.
   - "MRI of my knee" leads to the referral and existing-patient checks, then only the two valid provider/location combinations before calendar times are offered.
   - A late correction invalidates incompatible choices and stale slots. Confirmation uses the current draft, and the success terminal is reached only after a reservation receipt exists.
4. `make eval` writes the naive vs tools comparison and traces using equivalent fresh calendars, separate intent/guardrail metrics, and measured context growth.
