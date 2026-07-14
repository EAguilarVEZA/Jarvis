# Overnight Build — Martin CDP + Agent-Orchestration Brain

Built autonomously against `MARTIN_CDP_AND_ORCHESTRATION_PLAN.md`. Everything compiles, unit tests pass, routers are registered, and the frontend surfaces are wired and synced.

## Part A — Martin CDP (warehouse-native, HIPAA-first)

**`cdp_core.py`** — the six-pillar engine (pure, unit-tested):
- **Collection** — `ingest(payload, kind)` normalizes Segment-style `track`/`identify`/`batch`, with a **PHI guard** (`scan_phi`) and **consent gate** (analytics required to store, marketing required to activate). Emails are hashed on ingest, never stored raw.
- **Identity resolution** — deterministic stitching on `external_id` / `email_hash` / `anonymous_id` via an identity graph; the same identifiers always merge to the same `person_id`.
- **Profiles (Customer 360)** — `apply_event` folds events into traits + computed metrics (event_count, first/last seen, revenue_total, per-event counters) + recent events.
- **Segmentation** — a rule engine (`profile_matches`, `evaluate_segment`) over `trait:` / `metric:` / `event:` / `consent:` fields with ops eq/neq/gt/lt/gte/lte/contains/exists/did/did_not; k-anonymity floor (20).
- **Activation** — destinations framework (`build_audience`, `activate`): consent-gated, k-anon enforced, **dry-run by default**, opt-in real send.
- **Journeys** — event-triggered journey definitions store (hooks to the graph engine).

**`cdp_api.py`** (`/api/cdp/*`, registered) — track/identify/batch, profiles, stats, segments (+ live preview), destinations (+ guarded activate), journeys.

**`martin-sdk.js`** — first-party JS collection SDK (`martin.identify/track/page`), served at `/martin-sdk.js`. Uses `sendBeacon`, anonymous-id persistence, auto page views.

**Frontend** — a **CDP** button in the Schema Layer opens an admin with Overview (stats + SDK snippet), a **segment builder** (rules + live count + save), and **destinations** (add + dry-run activate against a segment).

Tested: identity merge (email+external_id → one person), profile/revenue computation, consent gate, PHI drop, segment counts, k-anon activation.

## Part B — Martin orchestration brain

**`orchestrator.py`** — supervisor + specialists (pure, unit-tested):
- **Agent registry** — 6 seeded specialists (Analyst, Data Engineer, Marketer, Compliance, Ops, Researcher) each with skills, tools (MCP), data scope, guardrail/side-effect flags.
- **Planner** — `plan(goal)`: LLM planner (via `llm_router`) with a deterministic keyword-router fallback; analyse→build→act ordering.
- **Guardrail** — a **Compliance gate is auto-inserted before any side-effect (Ops) step**, and side-effect steps require human approval.
- **Run lifecycle** — blackboard (shared state), handoffs (each step consumes prior + writes result), `advance_run` / `run_to_completion`, `approve_step`, and a full **audit trace** (who/what/when/why). A blocking compliance result halts the run.

**`orchestrator_api.py`** (`/api/orchestrator/*`, registered) — agents, plan, run, advance, approve, run status/trace, runs list.

**Frontend** — an **Orchestrate** button opens a goal box → **Plan** (shows the multi-agent plan) and **Plan & Run** (executes, shows each step's agent + status, the trace, and **Approve** buttons on paused side-effect steps).

Tested: plan places `compliance` before `ops`; run pauses at Ops approval; approval → completion; compliance-flagged run → blocked.

## How they compound
The CDP gives agents a governed body of data + profiles + segments + activation rails; the orchestrator gives it initiative: Analyst finds the cohort → Marketer builds the segment → Compliance gate de-identifies + checks consent → Ops activates through a CDP destination (dry-run until approved) → Martin reports the trace. Fully governed, human-approved at the money moment.

## Files added / changed
Added: `cdp_core.py`, `cdp_api.py`, `martin-sdk.js`, `orchestrator.py`, `orchestrator_api.py`, this log.
Changed: `server.py` (register cdp + orchestrator routers, serve SDK), `martin_app.html` (CDP + Orchestrate surfaces; synced to `~/Downloads`).

## Verification
`py_compile` clean on all modules; JS parses; CDP + orchestrator unit tests pass. New API surface: 25 routes.

## Honest notes / next
- Specialist step execution in `orchestrator._act` returns structured, side-effect-free results (dry-run) so the orchestration is testable offline; in the live server each step should call its real MCP tools (metric_ask, cdp, workflows). That wiring is the next step.
- CDP reference store is local JSON (events/profiles); production should land events in the customer's BigQuery (warehouse-native) — the interfaces are written to swap the store cleanly.
- Real audience send and any activation remain **dry-run/opt-in**; a configured endpoint + explicit action is required to go live.
- Critical path unchanged and non-code: **SOC 2 Type II + BAA**.
- Not committed to git (left your working tree as-is per the standing "commit tomorrow" note).
