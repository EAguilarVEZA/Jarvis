# Data Model — Canonical Experience Layer

Implements spec §6 (bronze/silver/gold) and §7 (canonical model). DDL:
`db/migrations/0001_canonical.sql` (18 tables, portable across SQLite/Postgres/BigQuery).

## Zones
- **Raw / bronze** (`raw_record`, `ingest_batch`) — immutable append of source payloads with
  `payload_hash` for idempotent replay (spec §28.5) and `schema_fingerprint` for drift.
- **Silver** — source-specific cleanup (timestamps, encoding, dedupe, canonical IDs). Modeled
  as transforms + `entity_crosswalk` rather than a separate physical table set for MVP.
- **Gold / experience** — the canonical entities below.

## Central fact: `experience_event` (§7.1)
One row per feedback/experience signal (a survey response, a Google review, a complaint…).
Key rules baked in:
- **Source text vs. derived text are separate** — `raw_text_ref` points at the source; only
  `normalized_text` is redacted/derived (spec §28.11). Full segments live in `feedback_item`.
- **AI output never overwrites source** — topics/sentiment are in `topic_fact` / `sentiment_fact`,
  not written back over source columns (spec §28.2). The event's own `primary_topic_id` /
  `sentiment_*` are convenience denormalizations produced by versioned inference.
- **Unlike rating scales preserved** — `rating_scale_min/max` travel with `rating_value`
  (spec §4.1.D).
- **Every inference is versioned** — `model_version`; full record in `model_inference` (§28.3).
- **Idempotent** — `UNIQUE(source_system, source_record_id)`.

## Supporting facts & dimensions
- `feedback_item` — text segments, original vs. normalized (§7.2).
- `topic_fact` — multi-label topics with confidence, span offsets, evidence ref, classifier
  version, taxonomy version (§7.3).
- `sentiment_fact` — **aspect-level** sentiment: one row per (event, topic) (§11.3).
- `operational_event` — journey/operational events: scheduled, check-in, roomed, discharge,
  call, referral, bill-contact, with `value_numeric` (e.g. wait minutes) (§7.4, §11.6).
- `metric_observation` — generic analytics fact the metric engine writes (§7.5) with
  numerator/denominator/value/comparison/benchmark, `sample_n`, `calculation_version`.
- `entity_dimension` — SCD2 golden dimensions (provider/location/department/service_line/
  facility/org_unit) so records attribute to the hierarchy **valid at event time** (§8, §28.12).
- `entity_crosswalk` — external→internal mapping with `resolution_level`
  (exact | deterministic_composite | probabilistic | unresolved) + confidence (§8).
- `topic` / `taxonomy_version` — the healthcare taxonomy as versioned data (§10, §28.7).
- `issue` / `intervention` / `intervention_measurement` — early-warning + closed loop
  (§11.8, §11.14).
- `source_system` / `ingest_batch` / `raw_record` / `lineage_edge` — source metadata + lineage
  so every claim traces to evidence (§28.4, §28.6).

## Experience graph (§9)
A logical layer, not a graph DB on day one: nodes = the dimensions/facts above; edges are
expressed as foreign keys + semantic views (FEEDBACK_ABOUT_ENCOUNTER, ENCOUNTER_WITH_PROVIDER,
FEEDBACK_HAS_TOPIC, …). Migrate to a graph DB only if traversal proves beneficial.
