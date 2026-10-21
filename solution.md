# Solution Overview

Phase 1 builds a voice agent builder on top of the existing Pipecat stack. [PLAN.md](PLAN.md) has the full plan; this file records the choices made while implementing it and what they cost. M1 proved the browser call path, M2 added the document contract and persistence, M3 built the graph editor, and M4 made it something you can clone and run.

## Milestone 1: browser integration

### One process for the API and the calls

The builder routes are registered on the Pipecat dev runner's own FastAPI `app` (`pipecat.runner.run.app`) before `main()` runs, so the API and the WebRTC signalling share port 7860.

- **Why:** no second server, no CORS setup, and the runner's `/start` → `/sessions/{id}/api/offer` flow works unchanged through the Vite proxy.
- **Tradeoff:** it relies on the runner creating `app` at module level and adding its routes inside `main()`. That holds in pipecat-ai 1.4.0 but isn't a documented extension point. If it changes, the fallback is a small FastAPI app of our own that mounts the runner's routes.

### Each call carries its agent

The UI sends the draft inline in the `/start` body (`{agent: <snapshot>}`), and `bot()` compiles it on every call.

- **Why:** you can test unsaved edits with no save and no restart, and the server validates exactly what will run.
- **Tradeoff:** the whole document travels with every call. That's negligible at this size.
- An explicit selection that's invalid is an error and never falls back to the example. A silent fallback would make a broken draft look like it works.

### A `/start` preflight

HTTP middleware checks the agent selection on `POST /start` and returns 422 before any WebRTC setup. `bot()` checks it again, because `/api/offer` can be called directly.

- **Why:** without the preflight, a bad draft would produce a peer connection that silently drops, instead of a readable error in the call panel.
- **Tradeoff:** the request body is parsed twice, once by the middleware and once by the runner, and the agent is compiled twice. Both are cheap.

### Node tracking hooks FlowManager internals

Pipecat Flows has no public node-change callback, and an edge handler returning `(result, next_node)` only *proposes* a transition. `TrackingFlowManager` overrides `_execute_transition`, `_set_node` and `_execute_actions`. It reports `node_entered` once the node's context and tools are in place, and before its post-actions run, so a terminal node is reported before `end_conversation` hangs up. A node whose activation fails is never reported.

- **Why:** reporting from the edge handler would highlight nodes that never actually activated.
- **Tradeoff:** these are private methods, so this is the most fragile part of the system. It is pinned to pipecat-ai-flows 1.4.0, and `tests/test_runtime.py` drives the real FlowManager transition path to catch drift. That package is also deprecated in favour of `pipecat.flows`, so migrating is a known future cost.
- The event carries `from`, `via` (the function called) and a JSON snapshot of `state`. `from` plus `via` identify the edge taken even when function names repeat across nodes. The snapshot stops later state changes from leaking into an event that was already sent.

### Holding messages until the client is ready

Flow initialization (on transport connect) and the RTVI client-ready handshake can finish in either order. `ClientReadyGate` queues server messages and errors until the client is ready, then sends them in order.

- **Why:** without it, the initial `node_entered` or a startup error could be sent before the client is listening, and lost.
- **Tradeoff:** a little coordination code, instead of relying on event ordering that isn't guaranteed.

### Failures end the call cleanly

Startup and transition failures are sent to the client as RTVI errors. The call ends only after that error is delivered, and the peer connection is released on every failure path.

- **Why:** the user sees why the call ended, and another call can start straight away.
- **Tradeoff:** a failed transition ends the call rather than retrying or staying on the current node. That's simpler, and the right default for a test tool.

### The frontend call client

There's a fresh `PipecatClient` for each call, and events from a previous client are ignored. A reducer holds the call state: status, transcript, path and microphone. Every exit (hang-up, failed start, server disconnect, leaving the page) goes through one idempotent `release()`.

- **Why:** stale events from a torn-down call can't corrupt the next call, and the microphone is always released.
- **Tradeoff:** a client is recreated per call instead of reused. The cost is negligible.
- Bot transcript text uses sentence-level `bot-output` events, deduplicated by segment ID. Word-level events repeat the same text.

### Deliberately minimal UI

M1 is a raw JSON textarea plus the call panel: no React Flow, no saving. Editing is locked from the moment a call starts until it is torn down.

- **Why:** prove the risky integration (client-js, SmallWebRTC through the proxy, transition-event timing) before building the editor on top of it.
- **Tradeoff:** throwaway UI. The call hook (`testCall.ts`) is meant to carry over to M3.

## Milestone 2: contract and persistence

### Pydantic models that keep unknown data

`schema.py` uses typed Pydantic models. Every model accepts extra fields and keeps them (`extra="allow"`), and documents are written back with `exclude_unset`, so a saved file matches what was written (no defaults are injected).

- **Why:** imports, hand edits and fields the forms don't understand survive parsing and saving, as the plan requires.
- **Tradeoff:** a typo such as `taks_messages` is kept silently. To compensate, validation warns about extra fields on the agent, nodes, edges and positions that the runtime doesn't use. Extra fields on messages, actions and property schemas are passed to Pipecat or the LLM verbatim, so they aren't flagged.
- Strict mode is on, so JSON values aren't coerced (`"true"` is not a boolean). This is stricter than Pydantic's default, but bad field shapes can't be saved.

### Malformed actions still save

`tts_say` and `end_conversation` have their own models. Anything else, including a malformed supported action such as `tts_say` with no `text`, parses as a generic `Action`. A custom discriminator decides which model applies.

- **Why:** the plan treats unsupported or malformed actions as runtime errors, not shape errors, so drafts containing them stay saveable and repairable. The generated TypeScript type is still a useful union: `TtsSayAction | EndConversationAction | Action`.
- **Tradeoff:** `pipecat-ai-flows` also has a built-in `function` action, but it needs a Python callable, which a JSON document can't hold, so it's reported as unsupported.

### Shape vs. graph validation

`parse()` checks shape, and `validate()` checks the graph and runtime. Both return the same issue shape: `{level, path, node, edge, message}`.

- `path` uses array indices (`["nodes", 2, "edges", 0, "target"]`), so issues stay unambiguous even with duplicate names.
- Pydantic error locations include union tags (such as `"other"`). These are removed by matching each location against the actual document, and duplicate messages from union alternatives are merged ("Expected a string or an array").
- **Tradeoff:** the location mapping is heuristic code that depends on how Pydantic reports errors. Tests pin the expected paths.
- Additions not in the plan:
  - empty node names are an error
  - function names are limited to 64 characters (OpenAI's limit)
  - a `tts_say` with empty text gets a warning
- Unreachable-node warnings are skipped when the initial node is missing, because that error already explains the problem.

### Saving a draft and running it are different

`PUT /api/agents/{id}` saves any well-formed document and returns its issues. `AgentBuilder` refuses to run anything with an error.

- **Why:** you can save half-finished work, but you can't run it.
- **Tradeoff:** the saved-agents folder can hold agents that won't run. The list endpoint doesn't show validity yet; the UI calls `/validate` after loading an agent.

### File storage

There's one JSON file per agent in `backend/agents/`, seeded with `example.json` (moved from `backend/example_flow.json`).

- IDs must match `^[a-z0-9_-]{1,64}$`, which rules out dots and slashes, so a path can't escape the folder.
- Writes are atomic: write a temporary file in the same folder, `fsync`, then `os.replace`. A failed write leaves the previous file and no temp file behind. A malformed document is rejected with 422 before anything is written.
- Saved files use a fixed format (2-space indent, field order: identity, then the graph). The checked-in example was rewritten in that format, so saving it unchanged produces no git diff.
- **Tradeoff:** no locking or conflict detection, so the last write wins. That's fine for a local, single-user tool, but it would matter if the Phase 2 Copilot and the UI wrote to the same agent at once.
- The example is also the default agent for calls that select none. Because it's an ordinary saved agent, it can be edited or deleted through the API. After a delete, calls with no selection fail with a clear error instead of running something else.

### Call agent selection

The order is: `body.agent` (inline draft), then `body.agent_id` (saved), then the example. An unknown or invalid ID, or a saved file that can't be read, is an error. The `/start` rejection now includes the issues as well as the message.

### One schema file for frontend types

`backend/contract.py` builds one JSON Schema that covers the agent document and the API response shapes (`Issue`, `AgentSummary`, `IssuesResponse`). `make types` writes it and generates `frontend/src/generated/contract.ts` with json-schema-to-typescript.

- Field titles are removed so the generator doesn't create an alias type for every field.
- Response models mark every field as required, so the TypeScript types don't make always-present fields optional.
- Agent models produce an index signature (`[k: string]: unknown`) because they keep unknown fields. Known fields are still typed.
- Drift is caught twice: a pytest test compares the committed schema (no Node needed), and `make check-types` regenerates both files in a temp folder and diffs them.
- **Tradeoff:** both the schema and the generated TypeScript are committed, so they need regenerating after every model change. The tests enforce it.

### Dependency locks

Pipecat is pinned to `==1.4.0`, because the runtime adapter depends on its internals. Pydantic, FastAPI, loguru and python-dotenv are now declared directly, since the code imports them.

- `backend/requirements-dev.lock` is a universal uv lock, built using the venv's existing packages as preferred versions. For Python 3.12+ it matches the tested environment exactly, and installing it changed nothing.
- **Tradeoff:** the lock also covers Python 3.11, which the README still lists, but there numpy, scipy and pyee resolve to older, untested versions. Requiring 3.12+ would remove that gap.
- `make install` uses the lock with plain pip. Only `make lock` needs uv.
- The frontend already had `package-lock.json`, and json-schema-to-typescript is pinned exactly, like the other packages.

### Out of scope for M2

The frontend is unchanged apart from the generated types. The M1 page still loads `/api/agents/example` through the new generic route. A UI for saved agents, save, the validation badge and the repair view all come with the M3 editor.

## Milestone 3: graph editor

### Pure document actions, one store

Every edit is a pure function from one `AgentConfig` to the next (`editor/document.ts`). Each one copies only what it changes and returns the same object when nothing changes. A Zustand store holds the document, and zundo keeps undo history for the document only (`partialize` to `doc`, compared by reference).

- **Why:** an undo step is exactly one action (a field commit, a rename with its reference updates, a whole drag, a delete). Selection, validation, view and call telemetry live in the same store but never create history or mark the document dirty.
- Form edits replace only the fields they show. Message roles, extra fields, other actions and their order, and schema keywords such as `minimum` all survive. Tests pin each of these.
- **Tradeoff:** actions address nodes and edges by index, which is simple and makes undoing a rename keep the selection. The cost is that a stale index would write to the wrong node, so the inspector is keyed by selection and every commit runs against the current document.

### Selection by index, canvas ids by name

The selection is `{node}` or `{node, edge}` by array index, the same addressing as issue paths, so clicking an issue selects its target directly. React Flow ids are names (`node`, `node::function`), so they stay stable across unrelated edits.

- Documents whose node or function names collide can't have unique canvas ids. They open in the JSON view, which doubles as the repair view, until the names are unique. You can still save them, which matches the M2 rule that structurally valid drafts are saveable.
- Dangling edges and self-loops are left off the canvas. The issue list and the node's transition list still reach them.

### The canvas is a controlled view

`toFlow(doc)` derives nodes and edges. Canvas events become document actions or transient state; nothing serializes the React Flow graph back.

- In-progress drag positions and measured node sizes are local canvas state. The drop commits all dragged positions in one `moveNodes`, so a drag is one undo step, and validation ignores it (it keys on the document without `ui`).
- **Tradeoff:** React Flow v12 needs `measured` sizes on controlled nodes, so the canvas keeps a size map. All flow nodes are rebuilt on each document change. That's fine at tens of nodes, and node cards read their own data through narrow store selectors.

### Layout

dagre lays out a document on open, or after a JSON apply, only for nodes without a `ui` position. If some nodes have positions, the new ones are placed below them. The laid-out document becomes the dirty baseline, so opening an agent never shows unsaved changes. New nodes go to the centre of the view. Tidy layout and the minimap are M4.

### Field buffers

Inspector fields edit a local buffer and commit it on blur or Enter (Escape reverts). A rejected commit, such as a conflicting rename or malformed JSON, keeps the text and shows the reason. Pending buffers register in `editor/buffers.ts`, and Save and Test call commit them all first and stop on any rejection.

- **Why:** a keystroke is never an undo step, and nothing silently runs or saves an older document than the one on screen.
- Uncommitted text counts as unsaved changes. Without that, an unapplied JSON edit on a clean document left Save disabled; the browser smoke test caught it.
- **Tradeoff:** a rejected buffer is discarded if you select something else. The error was shown, and keeping per-item drafts across selection changes wasn't worth the complexity.

### Raw JSON stays honest

Raw editors (pre/post actions, an unsupported property schema, the whole document) check shape before committing. Actions only need to be objects with a string `type`; whether they can run is the validator's job. The whole-document view asks the backend, because only Pydantic knows the full shape.

- That needed one backend change: `/validate` and `PUT` responses now carry `malformed`, so the client can tell shape errors (can't apply or save) from graph errors (fine to save, not to run). Types were regenerated.
- The property table covers `string`/`number`/`integer`/`boolean`, a comma-separated enum, a description and required. Unions, arrays, objects, combinators and enum values that wouldn't round-trip through a comma list use the raw editor instead.

### Test-call lock

`startTestCall` commits buffers, locks the store, validates a deep-cloned snapshot and starts the call only if there are no errors. Any refusal releases the lock. Once the call starts, `useCallLock` releases it when the call status returns to idle, which covers hang-up, failure and server disconnect.

- The store refuses `edit`, `replace`, undo and redo while locked, and the UI disables those controls. Selection, inspection and canvas navigation stay available.
- Because the document is locked from snapshot to teardown, the canvas and the running call can't drift apart. The canvas highlights the active node and the traversed edges (`from::via`) from `node_entered`. The path stays visible after the call and resets when the next one starts.

### Testing

vitest covers the document actions and what they preserve, undo grouping, transient changes creating no history, the lock, dirty tracking, `toFlow` id stability, layout, stale or position-only validation, and the call-start lock path. These run in Node with no DOM. The components weren't unit-tested. They were checked in headless Chrome against the real backend:

- a new 5-node agent built, connected and test-called before saving, with a live transcript and the start node highlighted
- rename, drag and delete each undone in one step
- repair and malformed-JSON refusal
- unknown fields preserved through form edits and saving
- conflicting-rename rejection blocking Save
- a refused call releasing the lock

### Out of scope for M3

Keyboard shortcuts (Save, Undo/Redo), import/export, tidy layout, the minimap, `make dev` and the README walkthrough are M4.

## Milestone 4: delivery

### Import and export are files, not API calls

Export downloads the open document as `<id>.json`. Import reads a file in the browser and opens it as a new, unsaved agent named after the file (deduplicated against saved ids), so nothing reaches `backend/agents/` until you save.

- **Why:** an import never overwrites a saved agent by accident, and it goes through the same open path as everything else (dirty tracking, history reset, the repair view).
- Content is taken as is. The only change is that nodes without a `ui` position get one, as on any open. A test pins that export → import gives back the same document, unknown fields included.
- A file that isn't a well-formed agent, or isn't valid JSON, opens in the JSON repair view with its original text instead of being refused. A trailing comma is easier to fix there than in another editor.
- Export of a malformed (raw) document exports its text unchanged. Like Save, Export commits pending field buffers first and stops if one is rejected.
- **Tradeoff:** import is "new agent from file". There's no "replace this agent with a file". The JSON view's paste and apply covers that.

### Shortcuts leave text undo alone

Mod+S saves (it commits the field being typed in first), and Mod+Z / Shift+Mod+Z (or Ctrl+Y) undo and redo the document. Mod is Cmd on macOS and Ctrl elsewhere. Inside a text field, undo and redo are left to the browser.

- **Why:** while you're typing, the field's text is an uncommitted buffer. Cmd+Z there should undo your typing, not the document change behind it. Save is the exception: it's safe everywhere, and the browser's own "Save page" is never wanted here.
- The key mapping is a pure function (`editor/shortcuts.ts`) with tests. The listener is one `keydown` handler in `App`. Undo and redo still go through the store, so the call lock applies to them too.

### Tidy layout and the minimap

"Tidy layout" sets every node to its dagre position in one `moveNodes` action, so it's one undo step. It keeps other `ui` fields, does nothing on an already tidy graph (no empty history entry), and fits the view afterwards. React Flow's `MiniMap` colours the start node, end nodes and the active node during a call.

- **Tradeoff:** tidy throws away hand-placed positions. It's an explicit button and undoable, and layout still never runs on its own except for unplaced nodes.

### Empty and error states

- **Backend down:** the first load shows "Can't reach the backend" and retries every 3 seconds. `make dev` starts Vite well before Python has finished importing Pipecat, so a browser opened straight away recovers by itself. The Vite proxy answers 502 when nothing is listening on :7860, and that's reported as "unreachable" rather than as a bare status code.
- **No agent open, or none saved:** an empty state with "New agent" and "Import". An agent with no nodes gets a hint on the canvas.
- **Notices:** info notices clear after 6 seconds. Errors stay until dismissed or replaced, so a failed save isn't missed.
- **Missing API keys:** the `/start` preflight now also checks `OPENAI_API_KEY` and `ELEVENLABS_API_KEY` and returns a 422 that names the missing keys and the file to fix. Before, the call connected and then dropped with no explanation (`bot()` failed on `os.environ[...]` after WebRTC setup). Editing, saving and validation don't need the keys.

### `make dev` and a fresh clone

`make dev` runs `make -j2 run web`, so one Ctrl+C stops both servers (make passes the interrupt to both jobs). `make install` also creates `backend/.env` from `.env.example` if it doesn't exist and says so. It never overwrites an existing one.

- **Tradeoff:** the two servers' logs are interleaved in one terminal. `make run` and `make web` still exist for separate terminals.
- Checked from a copy of the tracked files in an empty directory: `make install`, then `make dev` brought up both servers, the proxy reached the backend, and SIGINT to the process group stopped both. With the empty keys that `make install` writes, a test call showed the missing-keys message.

### Testing

vitest adds the shortcut mapping (both platforms, inside and outside fields), tidy as one step that keeps extra `ui` fields, and import/export: the round trip, id deduplication, positions added only where missing, malformed files and invalid JSON going to the repair view, refusal during a call, and pending buffers committed or blocking. pytest adds the missing-key preflight.

Also checked in headless Chromium against the real backend: import, then tidy, then Cmd+Z and Shift+Cmd+Z as single steps. Cmd+Z inside a field left the document alone, and Cmd+S from inside a field saved the typed value. The exported file kept unknown fields. A malformed import opened the repair view, and deleting the open agent showed the empty state. The backend-down page recovered on its own when the backend started.

### Not done

- I didn't place a real voice call in M4, because none of the call code changed. The M3 browser checks covered the call itself.
- Stretch goals (node templates, a custom edge for parallel edges and self-loops, call history, a catalog demo agent) are not done.
