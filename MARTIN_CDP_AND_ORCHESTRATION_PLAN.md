# Martin — Becoming a True CDP + The Agent-Orchestration Brain

Two connected moves: (1) turn the platform into a best-in-class, **warehouse-native, HIPAA-first CDP**, and (2) make **Martin the orchestration brain** that coordinates specialist agents to act on that data. The CDP is the body; Martin is the brain.

---

## Part A — A true, best-in-class CDP inside Martin

### What a CDP actually is (the six pillars)
A CDP is judged on: **(1) collection** (events + batch + warehouse), **(2) identity resolution** (stitch signals into one person), **(3) unified profiles** (traits + behavior + computed metrics), **(4) segmentation/audiences**, **(5) activation** (send to ads/CRM/ESP/warehouse), and **(6) governance** (consent, privacy, audit). Legacy CDPs (Segment, mParticle) bundle all six into a black box that copies your data out. The modern winner is the **composable / warehouse-native CDP** (Hightouch, Census, RudderStack): it runs *on your warehouse*, never creating a second copy.

### Our unfair advantages (why we can be best-in-class, not me-too)
We already sit on the hardest, most defensible pieces:
- **Warehouse-native by construction** — our semantic layer + Metric Lab run *on the customer's BigQuery*. We are composable from day one; PHI never has to leave the warehouse.
- **A governed semantic layer** — most CDPs have no shared definition of "revenue" or "active patient." We do. Audiences built on governed metrics are trustworthy by default.
- **HIPAA-first** — de-identify-first (Datavant), row-level security, consent-aware governance, encryption, audit. This is a moat in healthcare where Segment/Freshpaint struggle.
- **AI + automation already inside** — Ask, forecasting, experiments, and the workflow engine. A CDP that can *reason about and act on* its own audiences, not just store them.

### What we already have vs. what to build

| CDP pillar | Have | Build |
|---|---|---|
| Event collection | Ours-style ingest API + PHI/consent gate; Airbyte 600+ batch | **Martin JS SDK + first-party server event API** (`track`/`identify`/`batch`) landing raw events in the warehouse — generalize `ours_api` ingest into a product-wide "Martin CDP" ingest |
| Identity resolution | Ours identity concepts (external_id/email merge) | **Profile/identity store**: deterministic stitching (email/external_id) + optional probabilistic; household support; all in-warehouse SQL, governed |
| Unified profiles | Semantic layer + governed metrics | **Customer 360 profile**: traits + event history + computed metric values (LTV, last-seen, risk) as a governed table |
| Segmentation | Metric Lab; `ours_activation` audience compose (consent + k-anon) | **Visual segment builder** over profiles + metrics (rules + behaviors), with live counts and consent gating |
| Activation | Workflow connectors; audience push-back | **Destinations framework**: reverse-ETL syncs + streaming to ads/CRM/ESP/webhook/warehouse; the audience→activation loop |
| Real-time / journeys | Workflow triggers + graph engine | **Journey builder** (already the n8n graph): event → decision → action, entry/exit rules |
| Governance | Consent-aware RLS, Datavant de-id, audit, encryption | Consent center UI + retention policies; mostly done |

### Build sequence (CDP)
- **CDP-1 Ingestion** — Martin JS SDK + `/api/cdp/track|identify|batch`, landing governed raw events (reuse the Ours ingest + PHI/consent guard, made product-wide).
- **CDP-2 Identity + Profiles** — in-warehouse identity resolution + a governed **Customer 360** profile table (traits + computed metrics + recent events).
- **CDP-3 Segments** — visual segment builder over profiles/metrics with live counts, saved audiences, consent + k-anonymity floors.
- **CDP-4 Activation** — destinations framework (reverse-ETL + streaming) reusing our connector layer; audience → ad platforms / CRM / ESP / warehouse / webhook.
- **CDP-5 Journeys** — event-triggered journeys on the existing graph engine (entry rules, waits, branches, actions).
- **CDP-6 Governance polish** — consent center, retention, per-destination de-id salting, full audit.

**Positioning:** *the only warehouse-native, HIPAA-first CDP with a governed semantic layer and AI agents built in.* We don't just store and pipe data — we understand it, reason about it, and act on it.

---

## Part B — Martin: the orchestration brain for multi-agent work

The one-pager brain is literal: **Martin is the supervisor that coordinates specialist agents to accomplish a goal end-to-end**, safely, with a human in the loop.

### What we already have (the building blocks)
- **Graph workflow engine** (`workflows_api`) — nodes, branching, retries, sub-workflows, triggers, versioning.
- **Agent nodes + agent library** (`agents_api`) with **Postgres memory**, tool execution, and a **live reasoning trace**.
- **MCP everywhere** — governed metrics MCP, marketplace MCP, MCP client/nodes — the standard way agents call tools.
- **Multi-LLM harness**, **approval / human-in-the-loop node**, and governed data access (RLS).

We're already ~70% of a multi-agent platform. What's missing is the **orchestrator layer** and the **coordination protocol** that make agents work *together* rather than one at a time.

### The orchestration model (supervisor + specialists)
Martin (the supervisor/planner) receives a goal, **decomposes it into a plan**, **routes sub-tasks to specialist agents**, monitors progress, **re-plans on failure**, and **aggregates** the result — the classic hierarchical/supervisor pattern, plus handoffs and a shared blackboard.

**Specialist agents (each with declared skills + tools + data scope):**
- **Analyst** — answers via governed metrics, forecasts, explains changes.
- **Data Engineer** — curates tables, builds metrics, validates the model.
- **Marketer** — builds segments, designs campaigns/journeys, proposes audiences.
- **Compliance** — de-identification + consent + policy gate (a *guardrail agent* every side-effect passes through).
- **Ops** — executes approved actions into connected systems.
- **Researcher** — web/deep research and enrichment.

### What to build (orchestration)
- **ORCH-1 Orchestrator/Planner** — give Martin a goal ("find at-risk patients and launch a compliant win-back"), it emits a **multi-agent workflow graph** and runs it. Extends the existing AI workflow builder to plan across agents, not just nodes.
- **ORCH-2 Agent registry + capability scoping** — each agent declares its skills, allowed **tools (MCP)**, and **data scope (RLS)**, so routing is automatic and permissions are enforced.
- **ORCH-3 Shared context + handoffs** — a **blackboard** (shared memory via `agent_memory`) + governed semantic context so agents share state and ground on the same truth; a handoff protocol to pass tasks/results between agents.
- **ORCH-4 Guardrails + human-in-the-loop** — the **Compliance agent as a mandatory gate** before any activation; approval nodes for side-effects; **every agent action audited** (who/what/when/why + sources).
- **ORCH-5 Orchestration surface + observability** — a UI where you state a goal and **watch the Martin brain delegate to agents on the canvas** (the hub-and-spoke made live), with a cross-agent reasoning trace, run history, and answer-correctness evals.

**Standards to ride:** **MCP** for tools (already in), and the emerging **agent-to-agent (A2A)** interop so Martin can coordinate external agents too. Grounding stays governed — agents answer from the semantic layer, so a swarm of agents can't collectively hallucinate.

### Why this compounds with the CDP
The CDP gives agents a **governed body of data + profiles + audiences + activation rails**; the orchestrator gives that data **initiative**. Concretely: Martin notices a cohort drifting (Monitoring) → asks the Analyst why (driver analysis) → has the Marketer draft a segment + journey → the Compliance agent de-identifies + checks consent → Ops activates it through the CDP destinations → Martin reports back and measures lift. One goal, many agents, fully governed, human-approved at the money moment.

---

## Sequencing both
1. **CDP-1/2 + ORCH-1** first — ingestion + Customer 360 + a working orchestrator over the agents we already have. This alone is demo-able and differentiated.
2. **CDP-3/4 + ORCH-2/3** — segments + activation + agent registry + shared context. Now the loop closes.
3. **CDP-5/6 + ORCH-4/5** — journeys + governance polish + guardrails + the live orchestration UI.

The critical path remains **SOC 2 Type II + BAA** (non-code), which everything above is designed to sit behind.

---
*Companion to the executive one-pager. Grounded in the current codebase: `workflows_api.py`, `agents_api.py`, `agent_memory.py`, `semantic_api.py`, `metric_lab.py`, `ours_*` (ingest/identity/activation), `sql_security.py`, `datavant_*`, MCP servers.*
