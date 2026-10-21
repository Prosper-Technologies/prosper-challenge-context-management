# Solution Overview

Phase 1 builds a voice agent builder on top of the existing Pipecat stack. [PLAN.md](PLAN.md) has the full plan; this file records the choices made while implementing it and what they cost. M1 proved the browser call path; M2 added the document contract and persistence.

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
