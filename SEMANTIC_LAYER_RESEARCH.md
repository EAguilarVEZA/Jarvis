# Semantic Layer + Metric Lab — Research & Design

How Tableau (and dbt / Cube / Looker / Snowflake) build a governed semantic layer with metrics and an agent, and a concrete plan to bring the same concept to Smart With Martin.

---

## 0. The one idea that ties it all together

Every modern analytics platform separates **three layers**:

1. **Tables + relationships** (the physical/logical model) — you have this (Curate canvas: tables, drag-to-join, join suggestions).
2. **Metrics** (governed KPI definitions: "revenue = SUM(order_total) where status='completed', by order_date") — **you don't have this yet** (Metrics only *browses*). This is the Metric Lab.
3. **Topics / subject areas** (a base table + its safe joins + a curated set of metrics & dimensions) — the unit both humans and the AI agent navigate. **Missing.**

The AI agent then **never writes raw SQL**. It reads the model, **picks a metric + dimensions + filters from a menu**, and a deterministic compiler emits governed SQL. That is the entire anti-hallucination trick, and it's identical across Tableau VDS, dbt Semantic Layer, Cube, Looker, and Snowflake Cortex Analyst.

---

## 1. Tableau's semantic layer

**Two-layer data model (2020.2+).** A *logical layer* (the "noodle" / Relationships canvas) where tables stay normalized and keep their own grain, connected by **relationships** — and a *physical layer* (inside a logical table) where joins/unions flatten into one table.

**Relationships ≠ joins (the core mechanic).** A relationship is a *contract* (matching field pairs) with **no join type chosen up front**. Tableau **defers the join to query time and picks the join type per-viz** based on the fields used, using two settings:
- **Cardinality** (One/Many) → decides *aggregate-then-join* (Many, prevents fan-out double counting) vs *join-then-aggregate* (One).
- **Referential integrity** (Some/All records match) → outer vs inner join.
- Default when unknown: **Many-to-Many + Some records match** (safe).
Only tables whose fields are used get queried ("join culling").

**Calculated fields.** Row-level (per-row, wrapped in an aggregate on use) vs aggregate (view-level); LOD expressions `{FIXED/INCLUDE/EXCLUDE : agg}` declare a grain independent of the view. Defined once in a **Published Data Source**, inherited by every workbook.

**Governance.** Published Data Sources are versioned server artifacts (connection + calcs + roles + defaults). **Certification** = trust badge + search boost + curator note. **Tableau Catalog** = lineage/impact analysis + quality warnings via a GraphQL Metadata API.

**Field semantics.** Every field has a **role** (dimension→GROUP BY / measure→aggregate) and a display type (discrete/continuous), a **default aggregation**, format/color/sort defaults, **folders** (grouping) and **hierarchies** (drill paths), aliases, hide flags.

---

## 2. Tableau Pulse — the metric model to copy

Pulse splits **metric definition** (reusable spec) from **metric** (scoped instance). A definition captures:

| Component | Detail |
|---|---|
| Name / description / links | Governed business glossary entry |
| Data source | One published semantic model |
| Measure + aggregation | `sum/avg/median/min/max`, or a dimension via `count/count distinct` |
| Time dimension + granularity | day/week/month/quarter/year; fiscal-aware |
| Definition filters | *Fixed* predicates that change meaning (e.g. exclude returns) |
| Adjustable dimensions | Viewer slices; **first 20 become "insight dimensions"** for drill |
| Number format | currency/percent/custom |
| Time comparison | prior period / prior year (+ secondary) |
| Goals / thresholds | manual or data-driven |
| "Value up is" | favorable / unfavorable / neutral → drives color + language |
| Governance | certification + edit allow-list; inherits source row-level security |

A **metric** = definition + a scoped time period + filter values + optional goal. People follow metrics.

**Insights engine (2-stage, worth copying exactly):** (1) deterministic statistics compute *facts* (period-over-period, trend, expected-range **anomaly detection**, top/bottom contributors, concentration, outliers, correlation, forecast) → the ground truth; (2) facts ranked by impact; (3) an LLM **phrases** the top facts into a natural-language digest — *it never computes numbers*. Delivered daily via email/Slack; threshold alerts on crossing the unfavorable range.

*(Legacy "Tableau Metrics" KPI tiles were retired Feb 2024 / Server 2024.2 — Pulse replaced them.)*

---

## 3. Tableau Agentic Analytics (2026)

Tableau's thesis: **agents need "knowledge" = data + human-defined meaning** (metrics, relationships, definitions). Platform pillars: Knowledge Engine (semantic models + the Open Semantic Interchange standard, co-led with Snowflake & dbt), Conversational Analytics, **Headless Analytics via an open MCP server** (delivers to Slack, Salesforce, Teams, **Claude, ChatGPT**), Decision Engine (insight→action), Command Center (governance/audit).

**Tableau Agent** (formerly Einstein Copilot): scoped to its data source, indexes field captions/descriptions/**up to 1000 sample values**, matches on **business captions not raw column names**, enforces row/column security, human-in-the-loop. It builds vizzes, writes/explains calc fields, all in NL.

**VizQL Data Service (VDS) — the linchpin.** A headless REST API returning *data, not vizzes* from a published source:
- `/read-metadata` → the queryable governed fields (caption, dataType, fieldRole, defaultAggregation, formula, description) — how an agent **discovers vocabulary**.
- `/query-datasource` → a **constrained JSON query** (fields + aggregation function + filters), referencing governed calcs by caption; RLS enforced server-side; returns JSON.

The agent emits a constrained JSON query against *named governed fields* — never SQL, never the raw DB. That's the trust boundary.

---

## 4. Cross-industry confirmation (dbt / Cube / Looker / Snowflake)

All four use the same shape:

| Concept | dbt/MetricFlow | Cube | LookML | Ours (target) |
|---|---|---|---|---|
| Table wrapper | semantic model | cube | view | table (have) |
| Aggregatable primitive | measure | measure | measure | field role=measure |
| Group-by attribute | dimension/entity | dimension | dimension | field role=dimension |
| Join key + cardinality | entity type | `relationship` | `relationship` | **add to joins** |
| Named calculation | **metric (typed)** | measure | measure | **Metric object (new)** |
| **Subject area / topic** | saved query | **view** | **explore** | **Topic object (new)** |

**Universal principles:** metrics = aggregation + expression + filter, defined once; relationships declared with cardinality, engine auto-resolves the join path (cap ~2 hops); **fan-out safety** is the hard part (dbt bans joins onto the "many" side; Looker uses symmetric aggregates); a **topic = base table + safe joins + curated metrics/dimensions** = the NL grounding unit.

**Grounding the agent (Cube/dbt/Looker/Snowflake all agree):** Discover → Select → Execute-under-governance. Expose tools like `list_metrics`, `get_dimensions(metric)`, `query_metrics(metrics, dimensions, filters)` — return *descriptions + synonyms, not DDL*. The LLM's only jobs: normalize the question and pick the best-matching metric/dimension. Snowflake reports a semantic model gives **~20% text-to-SQL accuracy lift**; Looker reports **up to 2/3 fewer gen-AI data errors**.

---

## 5. Where we are + the gaps

**Have:** semantic layer API (`/api/semantic/*`), Curate canvas (tables, drag-field-to-join, join suggestions, draft/publish), calculated fields, an `/api/semantic/ask` agent, auto-insights, row-security governance.

**Gaps (in priority order):**
1. **Metric Lab** — a governed metric-definition object + create/edit UI + deterministic SQL compilation + browse. (Metrics only browses today.)
2. **Topics / subject areas** — group tables into a base + safe joins + curated metrics/dimensions for navigation and grounding.
3. **Cardinality on relationships** + fan-out-safe join compilation.
4. **Agent grounding upgrade** — have `/ask` select from *defined metrics/topics* (discover→select→execute) rather than open text-to-SQL.
5. **Metric monitoring** — reuse our existing forecasting/anomaly/driver engines to produce Pulse-style insight facts on any metric.

---

## 6. Proposed design (concrete objects)

**Metric definition** (new `metrics_semantic.json` / `/api/semantic/metrics`):
```
{ id, name, label, description, synonyms[],
  base_table, measure_field, aggregation (sum|avg|median|min|max|count|count_distinct),
  expression (optional SQL), filters[ {field, op, value} ],   # fixed = definition meaning
  time_dimension, default_grain (day|week|month|quarter|year),
  dimensions[]  # adjustable / breakdown; first N flagged insight dimensions
  format {type, currency, decimals}, direction (favorable_up|unfavorable_up|neutral),
  comparison (prior_period|prior_year), goal {mode, value},
  topic_id, certified, created_by }
```
Compiles deterministically:
```sql
SELECT DATE_TRUNC('{grain}', {time_dimension}) AS period,
       {aggregation}({measure})               AS value
FROM {base_table}
WHERE {definition_filters} AND {scoped_filters} AND {time_range}
GROUP BY 1 ORDER BY 1;
```
Breakdown = add `GROUP BY {dimension}` + rank; comparison = same query shifted a period; cumulative = window sum. Always inject the viewer's row-security predicate.

**Topic / subject area** (new): `{ id, name, label, description, base_table, joins[relationship_ids], metrics[], dimensions[] }` — the bounded universe handed to the agent.

**Relationship cardinality** (extend joins): add `cardinality (one_to_one|one_to_many|many_to_one|many_to_many)`; compile fan-out-safe (pre-aggregate the "many" side before joining, à la Tableau's Many setting / dbt's ban).

**Agent tools** (upgrade `/ask`): `list_metrics`, `get_dimensions(metric)`, `query_metric(metric, dimensions, filters, grain, time_range)` — LLM selects, compiler emits SQL, governance server-side.

---

## 7. Phased build plan

- **Phase A — Metric Lab (foundation).** Metric object + store + `/api/semantic/metrics` CRUD + SQL compiler + a **create/edit UI** (pick base table → measure → aggregation → time dimension → filters → format/direction → save) + live preview. Turns the Metrics page from browse into build.
- **Phase B — Topics + relationship cardinality.** Topic object + UI to bundle a base table with its safe joins and a curated metric/dimension list; add cardinality to joins + fan-out-safe compilation; easier table navigation/search in Curate.
- **Phase C — Agent grounding.** Rewire `/ask` to discover→select→execute over metrics/topics (constrained query, not free SQL); expose the same as MCP tools so Claude/ChatGPT can query governed metrics.
- **Phase D — Metric monitoring (Pulse-style).** Run our forecasting/anomaly/driver engines on any metric to produce ranked insight facts + an LLM-phrased digest; email/Slack + threshold alerts. (We already have the stats engines.)

---

## Sources

Tableau data model & relationships: help.tableau.com/current/pro/desktop/en-us/datasource_datamodel.htm · datasource_relationships_learnmorepage.htm · datasource_relationships_perfoptions.htm · calculations_calculatedfields_lod_overview.htm · datafields_typesandroles.htm · datasource_certified.htm · dm_catalog_overview.htm.
Tableau Pulse: help.tableau.com/current/online/en-us/pulse_create_metrics.htm · pulse_insights_platform_insight_types.htm · tableau.com/blog/what-is-tableau-pulse-metrics-layer.
Agentic / VDS / MCP: tableau.com/agentic-analytics · tableau.com/products/tableau-next · tableau.com/products/tableau-semantics · help.tableau.com/current/online/en-us/web_author_einstein.htm · help.tableau.com/current/api/vizql-data-service/en-us/index.html (+ vds_create_queries.html, vds_get_data_source_info.html) · github.com/tableau/tableau-mcp.
Industry: docs.getdbt.com/docs/build/metrics-overview · /semantic-models · /join-logic · docs.getdbt.com/blog/introducing-dbt-mcp-server · docs.cube.dev/docs/data-modeling/overview · cube.dev/articles/semantic-layer-for-ai-agents-2026 · docs.cloud.google.com/looker/docs/reference/param-explore-explore · cloud.google.com/blog/products/data-analytics/grounding-analytical-ai-agents-with-lookers-trusted-metrics · docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst · snowflake.com/en/blog/engineering/agentic-semantic-model-text-to-sql.
