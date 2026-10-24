# Solution Overview

The solution has two parts: a voice agent builder on the existing Pipecat stack (Phase 1) and a scheduling agent that uses tools and code to search the catalog and enforce booking rules (Phase 2). A benchmark compares the scheduling agent with a baseline that puts the whole catalog in the prompt.

## Phase 1: Voice Agent Builder

The React Flow editor lets users build agent graphs, edit fields in a form or raw JSON, undo and redo changes, import, export, save, and validate. A test call panel highlights the active node and the edges taken during a live call.

Key decisions:

- **Test calls use the current draft.** The UI sends the unsaved document in the `/start` body. The backend validates and compiles it for that call, so users can test edits without saving or restarting. Invalid drafts get a clear error before WebRTC setup; the server never falls back to the example agent.
- **Unfinished drafts can be saved.** Any well-formed document can be saved, but graph errors must be fixed before it can run.
- **Edits preserve document data.** Pydantic models keep unknown fields, and edits use pure functions. Manual edits and fields the UI doesn't recognize survive editing, saving, and export. Each undo step reverses one user action.
- **The document is locked during a call**, keeping the canvas in sync with the running agent.
- **One server and a shared schema.** The builder API runs in the Pipecat runner's FastAPI app, avoiding a second server and CORS setup. Frontend types come from one JSON Schema, and tests catch any mismatch.

Tradeoffs:

- **Live node tracking uses private Pipecat Flows methods** because there is no public node-change callback. This is the most fragile part of the system. Flows is pinned to 1.4.0, and a test exercises real FlowManager transitions to catch changes.
- **Each agent is stored in a JSON file with atomic writes and no locking.** The last write wins, which is acceptable for a local, single-user tool.

## Phase 2: Context Management

### Approach

The model interprets the caller's request; code decides what can be booked. Search and booking tools return small results instead of the full catalog. A policy engine narrows the options, enforces booking rules, and tells the model what to ask next.

### What makes the catalog hard

- **Similar and duplicate names.** Seven providers match "Dr. Chen", 13 nurse practitioners or physician assistants are titled "Dr.", and two pairs of locations have similar names, including "Mission Bay" and "Mission District".
- **Misleading specialty labels.** A provider's appointment-type IDs and a location's capabilities determine which services are available. Matching specialty names rejects valid combinations.
- **Interacting rules.** Referrals, new-patient rules, service requirements, and providers working across locations yield 760 valid service/provider/location combinations. A draft can pass each pairwise check yet have no valid completion—for example, a cardiology consultation at a location with no cardiologist.
- **Requests that cannot be booked.** Catalog checks found 8 appointment types with no eligible provider and 6 cases with no booking path for new patients. The agent must explain these limits.

### Key decisions and tradeoffs

**1. Code checks booking rules; the model interprets intent.**
Python code in `backend/catalog/` checks provider, service, and location compatibility, referrals, new-patient eligibility, and slot availability. The model interprets the conversation.

- *Why:* explicit, testable rules are more reliable than asking an LLM to check a long list of constraints.
- *Tradeoff:* a valid booking can still be the wrong appointment. The evaluation therefore scores intent separately from policy violations.

**2. Guided choices.**
`update_booking` records choices and returns `next_question` with at most three valid options. It asks for the service, then any needed patient facts (referral, then new-patient status), then provider and location. For an existing patient requesting a knee MRI, the provider question lists the two valid provider/location combinations.

`apply_request` combines name lookup and a draft update in one tool call. It resolves all supplied names before updating the session. Multiple or weak matches require clarification, and nothing changes until they are resolved. Lookup does not filter by booking rules, so an incompatible named choice is recorded and explained. Once the draft is ready, an automatic transition fetches availability. This optimization is included in the benchmark below. Compared with the previous run, it reduced tools-mode input tokens per conversation from 41.9k to 26.1k and scheduler calls from 18.0 to 11.2, with no loss in accuracy.

- Patient facts are **true, false, or unknown**. Unknown facts make options conditional. The agent asks about a fact only when it affects the result. Treating a missing value as false would wrongly classify an unknown caller as an existing patient.
- The engine never chooses for the caller, even when only one option remains.

**3. Small search results with access to every match.**
Search matches text using curated aliases and ranked tiers: exact, alias, all words, then fuzzy. A minimum score allows a "no match" result. Each response has at most 5 cards, plus `total_matches`, an `ambiguous` flag, `clarify` (a field that distinguishes matches), `next_cursor` for paging, and `excluded` with reasons.

- *Why:* when seven providers are called Chen, a top-5 list can hide the intended provider. Paging keeps every match reachable. Filters and ambiguity checks run before the result limit.
- If a named provider cannot offer the service, the agent **explains why** ("doesn't offer MRI - Knee") and never silently substitutes another provider.
- *Tradeoff:* metadata and follow-up searches use more tokens and turns. Text matching can also miss names that sound alike but look different, such as "Win" for Nguyen. Aliases cover common cases without embeddings.

**4. A token limit for every tool result.**
Results include only IDs, fields that distinguish candidates, and labels the model can read aloud. Fields with default values are omitted. Tests enforce a 400-token limit. Most results use 50–200 tokens; the largest, the first "Dr. Chen" page, uses about 380.

- *Tradeoff:* repeating names beside IDs costs tokens but avoids making the model recall earlier IDs.

**5. The latest explicit choice wins.**
Choices follow this order: patient facts, service, provider, location, then slot. A correction clears only later choices that now conflict, and the result explains each cleared field. Changing a patient fact never clears the service. If the caller has no referral, the MRI stays selected and the draft is blocked with `REFERRAL_REQUIRED`.

- *Tradeoff:* this can clear a choice the caller still wants, but the agent always reports it. Refusing the correction would make the caller repeat themselves.

**6. Guards enforce confirmation and booking.**
A model can skip a booking tool or claim success without booking. Graph edges therefore use **guards** (`booking_ready`, `slot_selected`, `booking_completed`) that check conditions before each transition. `book_appointment` requires the current draft revision and explicit confirmation. The success node requires a booking receipt. Repeating the same booking request creates no extra reservation. Reservations are atomic: eight threads booking the same slot produce one receipt.

- Guard names are strings in the document, resolved through an injected registry. The graph compiler needs no scheduling logic.
- *Tradeoff:* a premature transition costs an extra round trip, but guards prevent the model from leaving confirmation for success without a booking.

**7. Each node gets only the tools it needs.**
`book_appointment` is available only in `confirm`, and search tools are hidden while choosing a time. This keeps tool schemas small and the booking step explicit. Every node after the greeting keeps `update_booking` and `check_booking` so callers can make corrections.

- *Tradeoff:* changing tools between nodes reduces prompt caching (see Results).

**8. Shared logic for calls, the playground, and evaluation.**
All three use the same `Tool.run`. A test checks that they return identical results and leave the same session state, keeping validation consistent across demos, benchmarks, and live calls.

### Alternatives considered

- **Whole catalog in the prompt (naive).** Used as the benchmark baseline with the same policy engine, so only catalog access changes.
- **Embeddings or RAG.** Unnecessary at this size. Text matching with aliases needs no extra dependency or index, and paging handles duplicates. The benchmark found no matching failure that justified embeddings.
- **LLM summaries between nodes.** Deferred. Context growth has not hurt latency yet, and summaries risk losing unresolved preferences. One tools-mode failure already involved reusing an outdated search result.
- **Rules only in the prompt.** Rejected. Nothing would enforce booking rules or provide a reference for measuring violations.

### What's mocked

- **Availability** comes from a SHA-256 hash of (provider, location, date), making it stable across runs and processes. It covers a 14-day window with weekday slots sized for each service. Reservations block the provider's time across all locations to prevent overlaps.
- **Patient facts** come from the caller. There is no patient database or referral verification.
- **The calendar** runs in memory in one process, without persistence, distributed locking, cancellation, or rescheduling.

### The catalog UI

The Catalog screen shows linked tables of locations, providers, and appointment types, catalog checks, and a **tool playground**. The playground runs the real tools in an isolated session and calendar, showing the model's exact results, including refusals. The editor's inspector lets users attach tools to nodes and guards to edges.

## Evaluation

`make eval` runs 20 scenarios with a `gpt-4o-mini` simulated caller in two modes:

- **naive:** the catalog JSON (about 10k tokens) in the system prompt, without search tools.
- **tools:** the checked-in scheduler.

Both modes use the same graph, guards, booking validator, scheduler model (`gpt-4o`), caller, and calendar. The harness runs the compiled agent graph through text, testing the same logic used in voice calls.

A run is scored on its **final session state**. It passes if the intended reservation exists or the draft is blocked for the intended reason. Any booking must also follow policy for the caller's *true* facts. Transcript scoring would have missed four runs where the model said "you're confirmed" without booking: two per mode, all in `upstream_conflict_kept`.

### Results (20 scenarios × 3 repetitions × 2 modes)

| | naive | tools |
|---|---|---|
| Intended outcome reached | 53/60 (88%) | 54/60 (90%) |
| Scenarios correct in every run | 14/20 | 17/20 |
| Wrong bookings | 3 | 2 |
| Scheduler LLM calls per conversation (mean) | 10.3 | 11.2 |
| Scheduler input tokens per conversation (mean) | 104,190 (87% cached) | 26,136 (34% cached) |
| Prompt size, first → last call (mean) | 9,171 → 10,541 | 1,552 → 2,438 |
| Largest prompt | 13,441 | 5,654 |
| Est. scheduler cost per conversation | $0.15 | $0.06 |
| Latency per model call, p50 / p95 | 0.85s / 1.24s | 0.85s / 1.14s |

What the numbers show:

- **No clear accuracy winner after three repetitions.** Tools leads slightly: 54 vs 53 successful runs, and 17 vs 14 scenarios correct in every run. The gap is too small to draw a firm conclusion. Both modes share the policy engine, and neither booked against policy or recorded an incorrect patient fact.
- **Failures differ by mode.** Naive made three bookings with the right service but the wrong provider or location, reached the turn limit once, and handed off once without booking. Tools struggled with conversation state. In `correction_service`, two runs lost the caller's choice of Midtown after two service corrections and booked Downtown. In `upstream_conflict_kept`, the model reused old slot offers after the caller explored another location and returned. The validator rejected those slots, after which the model claimed a booking or handed off. In `ophthalmology_not_offered`, one run offered staff and handed off as "staff requested". This bypassed `request_recorded`, so the `NOT_OFFERED` block was never recorded.
- **Invalid tool calls were rejected.** Tools logged 11 errors: 8 `apply_request` calls supplied both text and an ID for one field, and 3 used stale slot IDs. Each returned "Nothing was changed"; none caused a bad booking.
- **4.0× fewer input tokens, but only 2.6× lower cost.** OpenAI caches naive's stable catalog prefix. Tools therefore uses more uncached input: 17.4k vs 13.4k tokens per conversation. Its changing tool list limits caching to 34%.
- **The size advantage grows with the catalog.** Naive starts at about 9.2k tokens and grows with the catalog. Tools' largest observed prompt was 5.7k tokens, and each tool result stays under 400 tokens regardless of catalog size.

### What the benchmark changed

Early test runs found a graph design problem. Separate nodes handled "find service" and "choose provider and location", but `update_booking` asked for a provider in the service node, which had no provider search tool. Rewording the prompt left only 5/12 runs correct. Merging the nodes into `choose_appointment` raised that to 11/12. **Each node needs tools for every question the engine can ask there.**

- *Caveat:* 6 of the 20 scenarios informed these fixes, so the evaluation set was not fully held out.

## Scalability

- **Tool results stay small as the catalog grows.** Results are capped and paged. A catalog ten times larger may require more pages for ambiguous searches, but each result stays within the same limit.
- **Filtering comes before ranking.** Service, location, and specialty filters narrow the candidates. Cached, read-only indexes support in-memory filtering at this size. A much larger catalog could use a search index behind the same tool interface.
- **Context growth during a call remains a cost.** Pipecat Flows appends each node's instructions and every tool result. The longest call with repeated corrections used about 92k input tokens across all model calls, versus 240k in naive mode. Better caching is the first option to test: keep the tool list stable across nodes and use guards to control access. This gives up node-specific tool lists but may reduce cost without adding summaries.
