# ADR 0001 — Build Patient Experience Intelligence as an extension of MARTIN

- Status: accepted
- Date: 2026-08-24

## Context
The MARTIN Master Build Spec calls for a healthcare patient-experience intelligence
platform. MARTIN (Jarvis / "Smart with Martin") already exists as a FastAPI modular
monolith with auth, a governed semantic layer over BigQuery, a statistics/experimentation
engine, an agent tool-loop, hierarchies + presets, de-identification (Datavant), and an
evaluations harness (see `docs/current-state.md`).

## Decision
Build the experience platform as a **new package (`martin_experience/`) inside the existing
MARTIN repo**, not a separate service, and reuse MARTIN's identity, authorization, UI shell,
semantic layer, stats engine, agent orchestrator, and de-identification. Package boundaries
follow the spec's service decomposition (§21) so any module can be extracted later.

## Consequences
- Zero impact on the running app until the `/v1/experience/*` router is mounted behind a
  feature flag (default off) — see migration plan in `docs/current-state.md`.
- The LLM remains the interaction/explanation layer only; all numbers come from the
  deterministic metric/anomaly/driver engines (reusing `experiments_api`).
- Canonical tables land in BigQuery gold alongside the marketing warehouse; the DDL is
  written portable so local dev can run on SQLite/Postgres.

## Alternatives considered
- **Standalone `martin-experience/` repo** (spec §22 layout): rejected for now — duplicates
  auth/semantic/Uin and slows reuse. We mirror that layout *inside* the monolith instead.
- **Graph database from day one** (spec §9): deferred — start with relational star schema +
  semantic views; migrate to a graph DB only if traversal proves materially beneficial.
