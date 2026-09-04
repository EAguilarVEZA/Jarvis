# MARTIN Patient Experience Intelligence

An extension of the existing MARTIN (Jarvis / "Smart with Martin") platform that unifies
patient feedback, reputation, operational, clinical-context, and journey data into a
governed healthcare experience-intelligence layer.

Built per `MARTIN_MASTER_BUILD_SPEC.md`. **The LLM is the interaction/explanation layer,
never the statistical source of truth** — all numbers come from deterministic analytics.

## Build order (spec §29 — we do NOT start with a chatbot)
1. scaffold ✓
2. schemas + migrations ← `db/migrations/`
3. connector SDK ← `connectors/base.py`
4. mock connectors + fixtures ← `connectors/`
5. canonical normalization
6. identity / crosswalk service
7. taxonomy service ← `taxonomy/`
8. deterministic metric engine
9. NLP service interfaces
10. retrieval / evidence model
11. local LLM gateway interface
12. tool registry / orchestrator
13. anomaly / root-cause contracts
14. API → 15. minimal UI → 16. real connectors

## Layout
```
martin_experience/
  docs/          current-state, data-model, connector-contract, taxonomy, security, model-strategy, api, adr/
  db/migrations/ canonical DDL
  connectors/    connector SDK + mock connectors + fixtures/
  taxonomy/      versioned healthcare experience taxonomy (data, not code)
  schemas/       canonical pydantic contracts (later)
  services/      metric, nlp, anomaly, driver, investigation, retrieval, orchestrator (later)
  tests/         contract / analytics / ai / e2e (later)
```

## Extension, not rewrite
Reuse MARTIN's existing FastAPI shell, auth (`auth.py`), governed semantic layer, BigQuery
warehouse, Evaluations harness, scheduling, and the agent library. This package adds the
experience-intelligence data model, connectors, engines, and tools on top.
