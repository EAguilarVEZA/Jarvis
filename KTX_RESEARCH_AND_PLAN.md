# ktx (Kaelio) — Deep Research & "Build Our Own, Better" Plan

What ktx is, exactly how it works, how we already use it, and a concrete plan to replace the dependency with a context layer we own — HIPAA-first and tighter to our stack.

> Sources: docs.kaelio.com/ktx (introduction, the-context-layer, semantic-layer-internals, writing/reviewing/building-context, wiki-retrieval, context-sources, cli-reference, telemetry), kaelio.com (+ the "open-source Anthropic internal data analytics engine" blog), github.com/Kaelio/ktx (Apache-2.0). Plus our own `ktx_client.py`, `jarvis_engine.py`, `kaelio_audit.py`, `compare_to_kaelio.py`.

---

## 1. What ktx is (in one paragraph)

ktx is an open-source (Apache-2.0), **self-improving context layer for data/analytics AI agents**. Its thesis: a database connection lets an agent *run* SQL, but the schema doesn't tell it which table is canonical for revenue, which join fans out, which rows are test accounts, or the board-approved definition of ARR — so "plausible SQL becomes wrong SQL fast." ktx turns your warehouse metadata + BI definitions + query history + docs into **two version-controlled, reviewable surfaces on disk**, serves them to agents (Claude Code, Cursor, Codex) over a **local MCP server + CLI**, and keeps them current by re-ingesting and **reconciling new evidence with the accepted state** (never a one-way sync). It is packaged as an npm CLI (`@kaelio/ktx`, Node 22+) with a Python query planner; every warehouse connection is **read-only**.

---

## 2. Architecture — the parts that matter

**Two committed pillars (plain files, git-reviewed):**

- **Semantic layer** — `semantic-layer/<connection>/*.yaml` (executable). One "semantic source" per table: `name`, physical `table:` *or* `sql:`, **`grain:`** (columns that uniquely identify a row — load-bearing for correctness), `columns` (typed, with `role: time`, `visibility: public/internal/hidden`), `measures` (`expr: SUM(...)` + optional per-measure `filter`), `segments` (named predicates), and **`joins`** (`to`, `on`, `relationship: many_to_one|one_to_many|one_to_one`). `descriptions` are keyed by origin (`user`/`dbt`/`ai`) so a human-authored definition always outranks an AI draft.
- **Wiki** — `wiki/global/*.md` and `wiki/user/<id>/*.md` (free-form business context). Markdown + frontmatter: `summary`, `tags`, **`sl_refs`** (links a page to the semantic sources it explains), **`refs`** (links to other pages), `usage_mode`. This is the knowledge graph that answers "what does this *mean*?"

The two pillars are **linked** — an agent that finds the "revenue" wiki page follows `sl_refs` straight to the executable `orders.total_revenue` measure. ktx validates and prunes stale links so a note can never route to a deleted definition.

**The semantic-query planner (the real engineering):** the agent does **not** hand ktx raw NL. It emits a small **declarative semantic query** (`{measures, dimensions, filters, segments, order_by, limit}` — fully qualified `source.column`, *no FROM/JOIN/GROUP BY*), and a **deterministic Python planner** compiles it:
1. resolve refs (qualify columns, look up measure formulas),
2. **pick an anchor + shortest-path over the typed join graph** (Dijkstra),
3. **detect fanout** by grouping measures per source (>1 source ⇒ chasm/fan-trap risk),
4. classify filters into WHERE vs HAVING,
5. generate SQL — **single-source aggregation when safe, per-source CTEs pre-aggregated at each grain when a fanout is present** (the "aggregate locality" trick that stops double-counting),
6. transpile to the warehouse dialect via **sqlglot**.
This is how ktx "resolves chasm and fan traps": grain + relationship come from the YAML (treated as fact, not inferred), and multi-fact queries are pre-aggregated in isolated CTEs before joining back to the shared dimension.

**The ingest / self-improving loop:** source connectors read each system → a context builder proposes updates → **reconciliation** merges new evidence with the *accepted* files (dedupes = `merged`/`subsumed`, flags contradictions for a human) → validation checks refs against the live schema. Every run is labeled with action types (`source_created`, `measure_added`, `join_added`, `merged`, `subsumed`, `wiki_written`, `skipped`) and full evidence/transcripts kept locally. **The ingest is literally a pull request** — parallel writers in git worktrees, a semantic gate, auto-commit clean patches, **always flag a semantic break (e.g. a metric contradicting an existing one) for a human**, squash-merge one reviewable commit.

**Retrieval:** hybrid search — semantic (vectors) + lexical (FTS5 BM25) + token overlap + a **value dictionary** that resolves a literal like "enterprise" to the exact column without scanning the warehouse — fused with Reciprocal Rank Fusion.

**Serving:** a local MCP daemon (`ktx mcp start`, loopback `127.0.0.1:7878`) exposes tools — confirmed names `sql_execution` (read-only SQL) and `sl_query` (compile/run a semantic query), plus wiki search, discovery, and `memory_ingest`. Two **skills** (`SKILL.md`) teach the agent the workflow: search context → compile → execute → capture learnings.

**The framing:** ktx says it packages the same **four layers** as Anthropic's internal analytics engine — (1) data foundations, (2) sources of truth (semantic + query corpus + business knowledge, "a human owns the definition"), (3) skills (a knowledge router + an analyst-process skill), (4) validation — and openly admits **validation is its thinnest layer** (no answer-correctness evals / adversarial review yet).

---

## 3. How WE use ktx today (the dependency to replace)

- `ktx_client.py` is the *only* module that talks to ktx — it shells out: `ktx sql -c <conn> --json` (execute), `ktx sl <q> --json` (search), `ktx sl query --measure … --format sql` (compile). Results are coerced to our `semantic.executor.QueryResult` so it drops in behind a flag.
- `jarvis_engine.py`: `JARVIS_SQL_ENGINE=bigquery|ktx`. `use_ktx()` routes execution through ktx when set. **`KAELIO_LOCKDOWN=1`** hard-forces `use_ktx()` False and makes `ktx_client` refuse to spawn the binary; `kaelio_audit.py` refuses to boot if Kaelio domains resolve off-loopback under lockdown.
- We force `KTX_TELEMETRY_DISABLED`/`DO_NOT_TRACK` into the child env.

**Read:** we already run ktx as an *optional execution backend*, and we were careful enough about its telemetry to build a kill switch. That caution is the whole argument for owning this layer — under HIPAA/PHI you don't want a third-party binary with PostHog telemetry anywhere near the query path.

---

## 4. Us vs ktx — what we already have (Phases A–D just shipped)

| ktx capability | Our status |
|---|---|
| Executable semantic layer (sources, measures, joins) | ✅ semantic layer + Metric Lab (governed metrics, BigQuery compiler) |
| Grain, visibility, segments on sources | ◐ have tables/fields/roles; add grain + visibility + segments |
| Typed join graph + cardinality | ✅ relationships + cardinality (Phase B) — **but not yet the fanout-safe CTE compiler** |
| Chasm/fan-trap-safe SQL (aggregate-locality CTEs) | ✗ **the key gap** — our metric compiler is single-table today |
| Declarative semantic query → SQL | ✅ metric compiler + `/metric_ask` (agent selects, compiler runs) |
| Wiki / business-context knowledge graph (`sl_refs`/`refs`) | ◐ we have a Knowledge surface; not yet linked to metrics as a graph |
| Ingest + **self-improving reconciliation** loop | ✗ we curate manually + autocurate tables; no evidence-reconciliation loop |
| Provenance / evidence per definition | ◐ versioning/snapshots; no per-definition evidence trail |
| Git-based review (ingest = PR) | ◐ draft→publish + versions; not diff-based review |
| Hybrid retrieval + value dictionary | ◐ semantic search exists; add value dictionary + RRF fusion |
| MCP serving to any agent | ✅ `semantic_mcp.py` (list/query/ask metrics) — shipped Phase C |
| Pulse-style monitoring | ✅ Phase D (facts + digest) — *beyond* ktx |
| Answer-correctness evals | ◐ we have an Evaluations surface — **the layer ktx admits is thin, we can win here** |

We are surprisingly close: we have the executable semantic layer, metrics, cardinality, a grounded agent, and our own MCP. The **three real gaps** are (a) the fanout-safe join compiler, (b) the wiki/business-context knowledge graph linked to metrics, and (c) the ingest/reconciliation self-improving loop.

---

## 5. "Build our own, better" — the plan

Design principle: **HIPAA-first, on-prem, no third-party telemetry, integrated with our stack** (Datavant de-id, marketplace, Studio agents, governance). Everything below is code we own — no ktx binary in the path.

**D1 — Fanout-safe join compiler (highest technical value).** Extend the Metric Lab compiler to accept multiple measures across related tables. Using our cardinality (Phase B): build the typed join graph, pick an anchor, shortest-path to required tables, **group measures by source and pre-aggregate each in its own CTE at its grain before joining back** (exactly ktx's aggregate-locality). This is what makes cross-table metrics *correct* and is the single biggest correctness upgrade. Keep BigQuery-dialect (add a transpile shim later if needed).

**D2 — Business-context knowledge graph.** Give each metric/table an optional wiki page (markdown + `summary`, `synonyms`, `sl_refs`→metric/table ids, `refs`→other pages), stored in our repo and surfaced in Knowledge + the Metric Lab. Feed these descriptions + synonyms to the grounding agent (Phase C) so NL maps to the right metric — this is the accuracy lever every vendor cites. Reuse our existing Knowledge surface as the home.

**D3 — Ingest + reconciliation loop (self-improving).** A scanner that reads the warehouse (grain/keys/samples), dbt/BI definitions, and query history, **proposes** context changes, **reconciles** against the accepted semantic layer/metrics (dedupe, flag contradictions), and routes them to a **human review queue** — never overwriting a human-owned definition. Reuse our draft→publish + versioning as the review/merge mechanism; add a per-change **evidence trail** (what source produced it).

**D4 — Retrieval + value dictionary.** Add a sampled value dictionary (resolve "enterprise" → the exact column/value) and RRF-fuse it with our semantic search, so the agent grounds literals without scanning the warehouse.

**D5 — Answer-correctness evals (beat ktx here).** Wire our **Evaluations** surface into the metric-ask agent: a golden set of question→expected-metric/SQL pairs, scored automatically, with regression tracking. This is the layer ktx concedes is its gap — owning it is our differentiation.

**D6 — Decommission the ktx dependency.** Once D1–D3 land, flip the default to our own engine, keep `JARVIS_SQL_ENGINE=ktx` only as an optional A/B backend, and keep `KAELIO_LOCKDOWN` as the belt-and-suspenders guarantee that nothing Kaelio touches the PHI path.

**Why ours can be better than ktx:** (1) HIPAA/on-prem-first with **zero external telemetry** and **de-identification before any LLM call** (our Datavant pipeline) — ktx sends prompts to whatever LLM you configure and ships PostHog telemetry by default; (2) one integrated product — the same governed metrics power Studio agents, the Data Marketplace, dashboards, and monitoring, not a separate CLI; (3) we already ship **Pulse-style monitoring** and an **Evaluations** harness, the two things ktx doesn't have yet.

---

## 6. Honest caveats

- ktx is **Apache-2.0** — legally we *could* fork or vendor pieces (e.g. study their planner) rather than reimplement from scratch; a clean-room reimplementation avoids license/attribution entanglement and keeps us telemetry-free.
- ktx is **new** (v0.x, modest GitHub traction, first blog May 2026), so its "self-improving" and validation maturity is unproven — we shouldn't over-index on its marketing, but the *architecture* (two pillars + planner + reconciliation + review) is sound and well-documented.
- The biggest lift is **D1 (fanout-safe compiler)** and **D3 (ingest/reconciliation)** — real engineering, multi-week. D2/D4/D5 are lighter and reuse surfaces we already have.

---

## Sources
docs.kaelio.com/ktx: /docs/getting-started/introduction · /docs/concepts/the-context-layer · /docs/concepts/semantic-layer-internals · /docs/concepts/wiki-retrieval · /docs/guides/writing-context · /docs/guides/reviewing-context · /docs/guides/building-context · /docs/integrations/context-sources · /docs/integrations/agent-clients · /docs/cli-reference/ktx-sl · /ktx-sql · /ktx-mcp · /ktx-ingest · /docs/community/telemetry.
kaelio.com · kaelio.com/blog/open-source-anthropic-internal-data-analytics-engine · github.com/Kaelio/ktx (Apache-2.0).
Internal: ktx_client.py · jarvis_engine.py · kaelio_audit.py · compare_to_kaelio.py.
