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

---

## Follow-up pass (#2 real tool execution + #3 warehouse-native)

**#2 — Orchestrator now executes for real** (`orchestrator._act`): specialists call live capabilities and hand off via the blackboard.
- **Analyst** reads `cdp.stats()` (and an optional LLM summary grounded on those numbers).
- **Marketer** builds an actual CDP segment (`cdp.upsert_segment`) with rules derived from the goal, and writes `segment_id` + count to the blackboard.
- **Compliance** loads that segment's real consented audience and **blocks** if it's below the k-anonymity floor.
- **Ops** dry-run activates the real segment (`cdp.activate`, always dry-run from the orchestrator).
- Every tool call is wrapped so a missing dependency degrades gracefully — still testable offline, still safe. Tested end-to-end: goal → analyst → segment(30) → compliance pass → approve → dry-run activate(30); small cohort → compliance **blocked**.

**#3 — CDP is warehouse-native** (`cdp_core`): 
- Pluggable **event sink** — JSONL by default; streams to BigQuery (`martin_cdp.events`) when `JARVIS_CDP_WAREHOUSE=1` and BQ is configured (reuses the semantic executor's client). Wired into `ingest()` best-effort.
- **Governed SQL compilers** — `_profiles_sql()` builds Customer 360 in-warehouse; `segment_sql(rules)` compiles a segment to BigQuery SQL that runs where the data lives (PHI never leaves). `trait:`/`event:` rules are correctly flagged as needing an identify sub-model.
- New endpoints: `/api/cdp/warehouse/status`, `/warehouse/profiles_sql`, `/warehouse/segment_sql`.

Changed this pass: `orchestrator.py`, `cdp_core.py`, `cdp_api.py`. All compile; CDP + orchestrator + warehouse SQL tests pass. **Uncommitted** (3 files) — safe to `git add -A && git commit`.

---

## Pass 3 — traits/events sub-model, journey runtime, CDP-as-MCP, journeys UI

- **Warehouse identify/traits sub-model** (`cdp_core.segment_sql`): `trait:` rules compile to `JSON_VALUE(traits,'$.key')` (numeric via `SAFE_CAST`) off a `latest_traits` CTE (latest identify wins), and `event:` rules become per-event `COUNTIF` columns. `unsupported_rules` is now empty — full segment expressiveness in BigQuery. Local + warehouse engines are at **parity** (a segment means the same thing both places).
- **Journey runtime** (`cdp_core`): event-triggered enrollment on ingest → a step engine (**wait / condition / action**), `tick_journeys()` scheduler to resume after waits, condition-based exits, and guarded (dry-run) actions; `tag` writes a first-party trait. Endpoints `/api/cdp/journeys/enrollments` + `/journeys/tick`. Tested: trigger → condition → tag → wait → tick → dry-run action → complete; failing condition exits.
- **CDP as MCP** (`cdp_mcp.py`, registered in `mcp_servers.json`): exposes `cdp_stats`, `list_segments`, `preview_segment`, `list_destinations`, `warehouse_segment_sql`, `profile` — so the orchestrator's Marketer/Ops agents (and Claude/ChatGPT/Cursor) can build and reason about audiences through governed tools.
- **Journeys UI**: a 4th tab in the CDP admin — build a journey (name + trigger + wait/condition/action steps), enable/disable, see enrollment counts, and run a scheduler tick.

Changed this pass: `cdp_core.py`, `cdp_api.py`, `cdp_mcp.py` (new), `mcp_servers.json`, `martin_app.html` (synced). 30 CDP+orchestrator API routes total; all compile + tests pass; JS parses. Still uncommitted since `f6d71cb`.

---

## Pass 4 — automatic journey scheduler + orchestrator-as-MCP

- **Journey scheduler** — a background task in the server lifespan calls `tick_journeys()` every 60s, so journeys advance past their waits automatically; ticks that resume enrollments are logged to the admin **Jobs** dashboard (`jobs.record`). (`server.py`)
- **Orchestrator MCP** (`orchestrator_mcp.py`, registered) — the supervisor brain is now MCP-exposed **read/plan-only**: `list_agents`, `plan_goal` (plan without executing), `list_runs`, `get_run`. External AI clients can see how Martin would decompose a goal across agents (the A2A surface) — but **cannot start or advance runs**; execution stays inside the app behind the Compliance gate + human approval.

MCP servers now: marketplace, metrics, **cdp**, **orchestrator** (4). Changed: `server.py`, `orchestrator_mcp.py` (new), `mcp_servers.json`. All compile.

## Where it stands
Complete, coherent, tested platform: SDK → consent/PHI-gated ingest → identity-resolved profiles (local **or** BigQuery) → segments (traits/events) → journeys (auto-scheduled) + guarded activation, all drivable by the orchestration brain and exposed as governed MCP tools (CDP + orchestrator). Remaining is the non-code critical path (**SOC 2 + BAA**) and optional depth (answer-correctness evals for the brain; a real ad-spend source to light up ROAS/CAC). Everything since `f6d71cb` is uncommitted — safe to `git add -A && git commit`.
