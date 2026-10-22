# Solution Overview

The deliverable is a voice agent builder on top of the existing Pipecat stack (Phase 1), plus a scheduling agent that handles the large catalog through tools and a deterministic policy engine instead of a prompt dump (Phase 2). A benchmark compares that agent with the naive "whole catalog in the prompt" baseline. [PLAN.md](PLAN.md) has the full plan, [README.md](README.md) explains how to run everything, and [`backend/eval/results.md`](backend/eval/results.md) has the benchmark results.

## Phase 1: Voice Agent Builder

A React Flow graph editor for agent documents: nodes, edges, a form inspector, a raw JSON view, undo/redo, import/export, save, validation, and a test call panel that highlights the active node and the edges taken during a live call.

Key decisions:

- **Each test call carries its draft.** The UI sends the unsaved document in the `/start` body and the backend compiles it for that call. Edits can be tested without saving or restarting, and the server validates exactly what will run. An invalid draft is refused before WebRTC setup with a readable error. It never quietly falls back to the example agent.
- **Saving a draft and running it are different.** Any well-formed document can be saved, so half-finished work isn't lost. A document with graph errors can't be run.
- **Lossless document handling.** The Pydantic models keep unknown fields, and every edit is a pure function on the document, so hand edits and fields the UI doesn't understand survive editing, saving and export. Each undo step is one user action.
- **The document is locked during a call**, so the canvas can't drift from what the call is running.
- **One process, one contract.** The builder API is registered on the Pipecat runner's own FastAPI app, so there is no second server and no CORS. One JSON Schema generates the frontend types, and the tests fail if they drift.

Tradeoffs:

- **Live node tracking hooks private Pipecat Flows methods**, because Flows has no public node-change callback. That makes it the most fragile part of the system. The dependency is pinned to 1.4.0, and a test drives the real FlowManager transition path to catch changes.
- **Storage is one JSON file per agent with atomic writes and no locking.** The last write wins, which is fine for a single-user local tool.

## Phase 2: Context Management

### The approach in one sentence

The model interprets what the caller means, and code decides what can be booked. The agent never sees the catalog. It calls a few search and booking tools that return small, capped projections, and a deterministic policy engine narrows the options, enforces the booking rules and tells the model what to ask next.

### What makes the catalog hard

The catalog analysis drove the design:

- **Duplicate and confusable names.** Seven providers match "Dr. Chen", 13 NPs/PAs are titled "Dr.", and two pairs of locations have similar names ("Mission Bay" / "Mission District").
- **Specialty labels mislead.** Whether a provider can perform a service comes from explicit appointment-type IDs plus the location's capabilities. Matching on specialty strings rejects valid combinations.
- **Policies interact.** Referrals, new-patient rules, capability-gated services and providers at several locations combine into 760 valid service/provider/location triples. Some drafts pass every pairwise rule and still have no valid completion, for example a cardiology consultation at a location with no cardiologist.
- **Some requests can never be booked.** A catalog lint found 8 appointment types no provider can deliver and 6 new-patient dead ends. The agent has to explain those, not invent a booking.

### Key decisions and tradeoffs

**1. Deterministic policy in code, interpretation in the model.**
Provider, service and location compatibility, referrals, new-patient eligibility and slot availability are pure Python (`backend/catalog/`). The model works out intent from the conversation.

- *Why:* checking constraints is exactly what an LLM does unreliably over a long list, and exactly what code does perfectly.
- *Tradeoff:* a booking that follows policy can still be the wrong appointment for the caller. Guardrails don't prove the request was understood, so the evaluation scores intent separately from policy violations.

**2. A guided narrowing loop instead of free-form search.**
`update_booking` records choices and returns `next_question` and at most three feasible options. The order is fixed: service first, then any patient fact a remaining option depends on (referral, then new-patient status), then provider, then location. For a knee MRI with an existing patient, the provider question lists exactly the two valid provider/location combinations.

The current scheduler also exposes `apply_request`, which combines name lookup and the draft update in one model tool call. It resolves all supplied names before applying a single update through the same session logic. Multiple matches and weak matches require clarification; nothing changes until the caller's choices are resolved. Identity lookup does not filter by booking policy, so a named but incompatible choice is recorded and explained rather than replaced. The existing automatic transition then fetches availability when the draft becomes ready. The benchmark below predates this optimization; a scripted graph test demonstrates two fewer model calls for one booking path, but live cost and accuracy still need measurement.

- Patient facts are **true, false or unknown**. An unknown fact becomes a *condition* on an option rather than a pass or a fail, and a fact is asked only when it changes the result. Treating a missing value as false would classify an unknown caller as an existing patient.
- The engine never picks for the caller, even when only one option is left.

**3. Small results that still reach every candidate.**
Search uses lexical matching with curated aliases and tiered ranking (exact, alias, all words, fuzzy), with a minimum match score so it can say "no match". It returns at most 5 cards, plus `total_matches`, an `ambiguous` flag, `clarify` (the attribute that tells the matches apart), `next_cursor` for paging, and `excluded` with reasons.

- *Why:* a top-5 list without paging can make the intended provider unreachable when seven of them are called Chen. Ambiguity is computed before truncation, and filters run before the cap.
- A named provider who can't work is **explained, never silently replaced** ("doesn't offer MRI - Knee").
- *Tradeoff:* the metadata and follow-up searches cost some tokens and turns, in exchange for full recall. Lexical matching misses spellings that sound alike but look different ("Win" for Nguyen). The aliases cover common cases without an embedding dependency.

**4. A token budget on every tool result.**
Results are projections, not catalog entities: IDs, the fields that tell candidates apart, and labels the model can read aloud. Fields at their defaults are omitted. Tests enforce a 400-token cap on every result. Most results are 50–200 tokens. The largest is about 380 tokens, the first "Dr. Chen" page.

- *Tradeoff:* names are repeated next to IDs in each result, so the model never has to recall an ID from earlier in the context.

**5. Corrections: the newest explicit choice wins, and only downstream choices are cleared.**
The order is facts, then service, then provider, then location, then slot. A correction clears a later choice only if it now conflicts, and every cleared field comes back with the reason, so the agent can explain it. A fact change never clears the service. If the caller turns out to have no referral, the MRI stays and the draft is blocked with `REFERRAL_REQUIRED`.

- *Tradeoff:* "newest wins" sometimes drops a choice the caller still wanted. It's predictable and always reported. The alternative, refusing the correction, makes the caller repeat themselves.

**6. Booking only after a confirmed draft revision, enforced by graph guards.**
Exposing a booking tool doesn't mean the model calls it, and reaching the "success" node doesn't mean anything was booked. Edges can name a **guard** (`booking_ready`, `slot_selected`, `booking_completed`) that the builder runs before the transition. `book_appointment` needs the current draft revision and an explicit confirmation, and the success node can only be entered after a receipt exists. Booking is idempotent, and the reservation is atomic: eight threads booking the same slot produce one receipt.

- The guards are generic. The document names them as strings, and the builder gets them from an injected registry, so the graph compiler knows nothing about scheduling.
- *Tradeoff:* a transition attempted too early costs a round trip. That's cheaper than a confirmation step the model can leave without booking.

**7. Each node gets only the tools it needs.**
`book_appointment` exists only in the `confirm` node, and searches aren't offered once a time is being chosen. This keeps the tool schemas small and the booking step explicit. Every node after the greeting keeps `update_booking` and `check_booking`, so corrections work anywhere.

- *Tradeoff:* the tool list changes between nodes, which breaks prompt caching (see Results).

**8. One domain core behind three adapters.**
The voice call, the HTTP playground in the UI and the evaluation harness all call the same `Tool.run`. A test checks that all three give identical results and leave the same session state. Validation can't drift between what is demoed, what is measured and what runs on a call.

### Alternatives considered

- **Whole catalog in the prompt (the naive approach).** Built as the benchmark baseline, with the same policy engine behind it, so catalog access is the only variable.
- **Embeddings or RAG.** Not needed at this size. Lexical matching with aliases is deterministic, needs no dependency or index, and recovers duplicates through paging. Embeddings would be justified by a measured matching failure, which the benchmark didn't show.
- **An LLM summary of the context at node boundaries.** Deferred. Growth is measured, but it doesn't hurt latency yet, and a summary risks dropping unresolved preferences. One tools-mode failure was already caused by a stale earlier search being reused.
- **Prompt-only policy.** Rejected. There would be nothing to refuse a bad booking, and nothing to measure violations against.

### What's mocked

- **Availability.** It's generated from a SHA-256 hash of (provider, location, date), so it's stable across runs and processes. The window is 14 days of weekday slots at each service's duration. Reservations take up the provider's time across all locations, so overlapping bookings are refused.
- **Patient facts** are values the caller states. There's no patient database or referral check.
- **The calendar is in memory, in one process.** There's no persistence, distributed locking, cancellation or rescheduling.

### The catalog UI

The Catalog screen has tables of locations, providers and appointment types with cross-links, the catalog lint, and a **tool playground**. The playground runs the real tools against a sandboxed session and calendar and shows exactly what the model would see, refusals included. The editor's inspector can attach tools to nodes and guards to edges.

## Evaluation

`make eval` runs each of 20 scenarios against a `gpt-4o-mini` simulated caller, in two modes:

- **naive:** the catalog JSON (about 10k tokens) in the system prompt, with the search tools removed.
- **tools:** the checked-in scheduler.

Both modes use the same graph, guards, booking validator, model (`gpt-4o`), caller and calendar. The harness drives the compiled agent graph as text, so it measures what a call runs.

A run is scored on the **final session state, not the transcript**. It passes if the intended reservation exists, or the draft is blocked for the intended reason, and the booking doesn't violate policy for the caller's *true* facts. Scoring the transcript would have missed two runs where the model told the caller "you're confirmed" without booking.

### Results (20 scenarios × 3 repetitions × 2 modes)

| | naive | tools |
|---|---|---|
| Intended outcome reached | 48/60 (80%) | 47/60 (78%) |
| Wrong bookings | 9 | 6 |
| Scheduler input tokens per conversation (mean) | 171,763 (84% cached) | 41,862 (25% cached) |
| Prompt size, first → last call (mean) | 8,703 → 11,105 | 382 → 2,813 |
| Est. scheduler cost per conversation | $0.25 | $0.10 |
| Latency per model call, p50 / p95 | 0.80s / 1.24s | 0.76s / 1.14s |

What the numbers show:

- **Correctness is a tie at k=3**, because both modes share the policy engine. Neither mode made a booking that violated policy, which is the point of the guardrails.
- **The two modes fail differently.** Naive's misses are catalog-reading errors: an MRI - Spine booked for a knee, "Sunset has no dermatology" when a dermatologist practises there, a wrong patient fact recorded. Its updates hit a blocked draft 27 times, against 8 for tools, so the validator caught many bad IDs before they became bookings. Tools' misses are about conversation policy: handing off after a real block without recording the request, and reusing an earlier search result.
- **Cost: 4.1× fewer input tokens, but only 2.6× cheaper.** OpenAI caches naive's stable catalog prefix, so the uncached input of the two modes is similar (26.6k vs 31.2k). Tools caches only 25%, because the tool list changes between nodes and that invalidates the prefix.
- **Scale is where the gap opens.** Naive's prompt never drops below 8.7k tokens and grows with the catalog. Each tools result stays under 400 tokens whatever the catalog size.

### What the benchmark changed

The smoke runs found a structural defect. The original graph split "find service" from "choose provider and location", but `update_booking`'s next question pulled the model into collecting the provider in a node that had no provider search. Rewording the prompt didn't fix it (5/12 correct). Merging the two into one `choose_appointment` node did (11/12). The lesson: **a node's tools have to cover the questions the engine will ask in that node.**

- *Caveat:* the fixes were made with 6 of the 20 scenarios in view, so the reported set isn't held out.

## Scalability

- **Prompt size doesn't depend on catalog size.** Results are capped and paged. A catalog ten times larger means more pages in the rare ambiguous case, not a larger prompt.
- **Matching scales with filtering first.** Explicit filters (service, location, specialty) narrow candidates before ranking. At this size, in-memory filtering over cached immutable indexes is enough. A much larger catalog would put the same tool contract in front of a search index. The model-facing tool interface wouldn't change.
- **Context growth within a call is the remaining cost.** Pipecat Flows appends every node's instructions and every tool result, so long correction-heavy calls reach about 158k input tokens in total. The cheaper fix is caching: keep a stable tool list across nodes and enforce what can run where with guards rather than by hiding tools. That trades away per-node tool scoping and should be measured before adding a summary step.

## Scoping

**Built:** the graph editor and test calls; the catalog domain (policy engine, matching, lint, mock calendar); the tools and guards; the scheduler agent; the catalog UI and playground; the naive vs tools benchmark with traces.

**Mocked:** availability, patient records and referral verification.

**Left out on purpose:** a real EHR or calendar integration, persistence across restarts, multi-user locking, rescheduling and cancellation, embeddings, and LLM-based context summarization. None of these is needed to show the context-management argument.

## Remaining work

- A held-out scenario set, and more repetitions. k=3 can't separate 80% from 78%.
- Enforce "record the request before handing off" with a guard on `handoff`, rather than a prompt rule.
- Stop false "you're booked" claims with a post-check on the confirm node's reply.
- Try caching-friendly tool exposure, and add a state summary only if longer calls make it necessary.
- Place a live voice call with the merged scheduler. It's tested through the real FlowManager and the text benchmark, but speech recognition errors and interruptions aren't covered.
