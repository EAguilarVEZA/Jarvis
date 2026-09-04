# Current-State Architecture Map (existing MARTIN / Jarvis)

Per `CLAUDE.md` → "Before Coding": inventory the existing MARTIN repo so the experience
extension reuses rather than rebuilds. This maps what exists today in `~/Jarvis`.

## 1. Current-state architecture
MARTIN today is a **FastAPI modular monolith** (`server.py`) serving a single-file SPA
(`martin_app.html`) plus a floating voice assistant ("the pill"). It already includes:

- **Auth & tenancy** — `auth.py` (login, HMAC session tokens, opt-in gate), `tenancy.py`
  (tenant isolation), `embed_api.py` (RLS, SSO/SCIM), governance engine with `{{user.x}}`
  row-security.
- **Governed semantic layer** — `semantic/` (loader, resolver, dialects incl. **Snowflake**),
  metric registry (`metric_lab.py`), business-context/synonyms store, text-to-SQL planner
  behind `/api/semantic/ask` + `/research` + `/metric_ask`, over BigQuery (`oh-data-warehouse`).
- **Analytics / stats** — `experiments_api.py` (difference-in-differences, matched & synthetic
  control, CUPED, forecasting/Holt-Winters, anomaly detection, driver analysis, segment scan,
  "explain a metric"). This is a strong basis for the spec's metric/anomaly/driver engines.
- **Agent library** — `agents_api.py` (211 specialist agents, list/get/**chat with tools**),
  `run_agent_turn` bounded tool-use loop, workflows/Studio orchestrator.
- **Hierarchies & presets** — `hierarchies_api.py` (OH service-line tree L1–L6, saved presets)
  — reusable for the spec's location/provider/org-hierarchy dimensions.
- **Data brokerage** — `marketplace_core/api/mcp`, `datavant_client/pipeline` (tokenize →
  de-identify → certify → publish), `crypto_store.py` (encryption at rest).
- **CDP** — `cdp_core.py` (ingest, identity, profiles, segments, journeys), warehouse-native.
- **Ops** — daily brief engine, scheduled tasks, Evaluations harness (`evaluations`), settings,
  SQL reviews, notifications.
- **Multi-LLM** — `system_llm.py` gateway (Claude/OpenAI/Gemini/Ollama) — a starting point for
  the spec's `LLMGateway` local-first abstraction.

## 2. Components we can reuse directly
| Spec need | Reuse from existing MARTIN |
|---|---|
| §2.1 identity/authz/UI shell/logging | `auth.py`, `tenancy.py`, `embed_api.py`, `server.py`, `martin_app.html` |
| §11.7 metric engine | `metric_lab.py` + `semantic/` + `experiments_api` |
| §11.8 anomaly, §11.9 driver, §11.10 root-cause | `experiments_api.py` (anomaly, driver, DiD, segment scan) |
| §11.15 retrieval | semantic layer + business-context; add a vector index |
| §12.4 LLM gateway | `system_llm.py` (extend to local vLLM adapter) |
| §12.3 tool registry / orchestrator | `agents_api.run_agent_turn` tool loop + workflows orchestrator |
| §8 crosswalks / hierarchy | `hierarchies_api.py` (SCD hierarchy + presets) |
| §19 de-id / security | `datavant_*`, `crypto_store.py`, governance RLS |
| §13 eval sets | Evaluations harness |
| §14 conversational agent | the pill + voice + agent chat |

## 3. Gaps vs. the target architecture (what this extension must add)
- **Canonical experience data model** (§7) — `experience_event`, `feedback_item`, `topic_fact`,
  `operational_event`, `metric_observation` — does not exist yet. **New.**
- **Connector SDK + acceptance contract** (§5, §24) — the standard `MartinConnector` interface,
  idempotent load, checkpoints, DQ, schema fingerprinting, dead-letter. **New.**
- **Experience connectors** — surveys, HCAHPS/CAHPS, Google reviews, star ratings, complaints,
  call center, digital. **New** (marketplace/airbyte connectors are a different shape).
- **Healthcare experience taxonomy as versioned data** (§10). **New.**
- **Aspect-level NLP** — topic classification, aspect sentiment, journey-stage, severity (§11.1–6).
  The app has document-level LLM summarization but not governed multi-label aspect models. **New.**
- **Experience graph** (§9) — logical star-schema semantic layer over the canonical model. **New.**
- **Issue/intervention lifecycle + measurement** (§11.14, §16). Partly present in experiments
  rollout/measure loop; needs the issue-inbox + intervention workspace. **Extend.**
- **Local LLM inference + gateway adapters** (§12.4) — vLLM/OpenAI-compatible local endpoint. **New adapter.**
- **Vector index / semantic retrieval with mandatory metadata filtering** (§11.15). **New.**

## 4. Proposed integration points
- Mount a new `martin_experience` router set under `/v1/experience/*` in `server.py`
  (spec §15), behind the existing auth/tenancy middleware.
- Register experience tools into the existing agent tool loop so "Ask MARTIN" (spec §16)
  runs through the same orchestrator, but only over the new governed experience tools.
- Land canonical tables in BigQuery gold alongside the marketing warehouse; reuse the
  semantic loader to expose them as governed views.
- Reuse `hierarchies_api` for location/provider/org dimensions and minimum-cell policies.

## 5. Proposed technology choices (consistent with existing stack)
- Relational analytical store: **BigQuery** (existing) for gold; the migration DDL here is
  written portable (works against Postgres/SQLite for local dev + BigQuery for prod).
- API: **FastAPI** (existing). Jobs: existing scheduler; add a worker for batch inference.
- Vector index: start with a local FAISS/pgvector-style index behind the retrieval service.
- Local inference: OpenAI-compatible **vLLM** endpoint via a new `LLMGateway` adapter
  alongside the existing `system_llm.py`.
- Secrets: existing `crypto_store.py` / vault; de-id via `datavant_*`.

## 6. Migration plan (does not break existing MARTIN)
1. Add `martin_experience/` as an isolated package (this scaffold) — zero impact on the
   running app until its router is mounted.
2. Land canonical tables + fixtures; validate with mock connectors offline.
3. Mount `/v1/experience/*` behind a feature flag (default off), like other optional routers.
4. Wire experience tools into the agent loop only when the flag is on.
5. Swap mock connectors for real ones one at a time (spec §29 step 16).
