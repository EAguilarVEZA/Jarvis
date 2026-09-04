-- MARTIN Patient Experience Intelligence — canonical schema (Epic A)
-- Portable DDL: runs on SQLite/Postgres for local dev; the same shapes land in
-- BigQuery gold for prod. Per spec §6.3 (Gold entities) and §7 (canonical model).
--
-- Non-negotiable rules encoded here (spec §28):
--   - source text kept separate from normalized/redacted text (raw_text_ref vs normalized_text)
--   - every ML inference is versioned (model_version on inference-bearing rows)
--   - source data never overwritten by AI output (topic/sentiment live in separate fact tables)
--   - lineage + source metadata are first-class (see source_* and lineage tables)
--   - taxonomy is versioned data (topic tables), not hard-coded

-- ─────────────────────────────────────────────────────────────────────────────
-- Source metadata + lineage (spec §5, §28.6)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE source_system (
  source_system        VARCHAR(100) PRIMARY KEY,   -- e.g. 'google_reviews'
  connector_type       VARCHAR(50)  NOT NULL,      -- reputation | survey | ehr | ...
  source_owner         VARCHAR(200),
  phi_classification   VARCHAR(30)  NOT NULL,      -- public|internal|confidential|phi|restricted
  extraction_mode      VARCHAR(30),                -- full|incremental
  schedule_cron        VARCHAR(100),
  active               BOOLEAN      NOT NULL DEFAULT 1,
  created_at           TIMESTAMP    NOT NULL
);

CREATE TABLE ingest_batch (
  ingestion_batch_id   VARCHAR(64)  PRIMARY KEY,
  source_system        VARCHAR(100) NOT NULL REFERENCES source_system(source_system),
  extracted_at         TIMESTAMP,
  received_at          TIMESTAMP    NOT NULL,
  record_count         INTEGER      NOT NULL DEFAULT 0,
  schema_fingerprint   VARCHAR(64),                -- detects schema drift
  source_watermark     VARCHAR(250),
  status               VARCHAR(30)  NOT NULL,       -- ok|dq_failed|partial|dead_letter
  dq_report_json       TEXT
);

CREATE TABLE lineage_edge (
  lineage_id           VARCHAR(64)  PRIMARY KEY,
  from_kind            VARCHAR(50)  NOT NULL,       -- source_record|raw|silver|gold|metric
  from_id              VARCHAR(250) NOT NULL,
  to_kind              VARCHAR(50)  NOT NULL,
  to_id                VARCHAR(250) NOT NULL,
  transform            VARCHAR(200),               -- which engine/step produced it
  transform_version    VARCHAR(50),
  created_at           TIMESTAMP    NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Raw / Bronze (spec §6.1) — immutable append of source payloads
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE raw_record (
  raw_id               VARCHAR(64)  PRIMARY KEY,
  ingestion_batch_id   VARCHAR(64)  NOT NULL REFERENCES ingest_batch(ingestion_batch_id),
  source_system        VARCHAR(100) NOT NULL,
  source_record_key    VARCHAR(250) NOT NULL,
  payload_json         TEXT         NOT NULL,
  payload_hash         VARCHAR(64)  NOT NULL,
  operation_type       VARCHAR(20)  NOT NULL DEFAULT 'upsert', -- upsert|delete|correction
  schema_version       VARCHAR(50),
  extracted_at         TIMESTAMP,
  received_at          TIMESTAMP    NOT NULL,
  UNIQUE(source_system, source_record_key, payload_hash)      -- idempotent replay (spec §28.5)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Taxonomy (spec §10) — versioned as DATA
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE taxonomy_version (
  taxonomy_version     VARCHAR(50)  PRIMARY KEY,
  effective_from       TIMESTAMP    NOT NULL,
  effective_to         TIMESTAMP,
  notes                TEXT
);

CREATE TABLE topic (
  topic_id             VARCHAR(100) NOT NULL,       -- e.g. 'WAIT.DELAY_COMMUNICATION'
  taxonomy_version     VARCHAR(50)  NOT NULL REFERENCES taxonomy_version(taxonomy_version),
  parent_topic_id      VARCHAR(100),
  domain               VARCHAR(50)  NOT NULL,       -- ACCESS|ARRIVAL|WAITING|...
  name                 VARCHAR(200) NOT NULL,
  description          TEXT,
  inclusion_examples   TEXT,
  exclusion_examples   TEXT,
  hcahps_mapping       VARCHAR(200),
  regulatory_mapping   VARCHAR(200),
  PRIMARY KEY (topic_id, taxonomy_version)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Dimensions (spec §7 entity_dimension, §8 crosswalks, §4.2 masters)
-- SCD-aware: every analytical record attributable using hierarchy valid at event time.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE entity_dimension (
  entity_key           VARCHAR(64)  PRIMARY KEY,    -- surrogate
  entity_type          VARCHAR(50)  NOT NULL,       -- provider|location|department|service_line|facility|org_unit
  entity_id            VARCHAR(100) NOT NULL,       -- natural id
  display_name         VARCHAR(300),
  npi                  VARCHAR(20),
  parent_entity_id     VARCHAR(100),
  specialty            VARCHAR(150),
  attributes_json      TEXT,
  valid_from           TIMESTAMP    NOT NULL,        -- SCD2
  valid_to             TIMESTAMP,
  is_current           BOOLEAN      NOT NULL DEFAULT 1
);

CREATE TABLE entity_crosswalk (
  crosswalk_id         VARCHAR(64)  PRIMARY KEY,
  external_system      VARCHAR(100) NOT NULL,        -- e.g. 'google'
  external_id          VARCHAR(250) NOT NULL,        -- e.g. google place_id
  internal_entity_type VARCHAR(50)  NOT NULL,
  internal_entity_id   VARCHAR(100) NOT NULL,
  resolution_level     VARCHAR(40)  NOT NULL,        -- exact|deterministic_composite|probabilistic|unresolved (spec §8)
  confidence           DECIMAL(8,5),
  created_at           TIMESTAMP    NOT NULL,
  UNIQUE(external_system, external_id, internal_entity_type)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Canonical experience event (spec §7.1) — the central fact
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE experience_event (
  experience_event_id        VARCHAR(64)  PRIMARY KEY,
  source_system              VARCHAR(100) NOT NULL,
  source_type                VARCHAR(50)  NOT NULL,
  source_record_id           VARCHAR(250) NOT NULL,

  patient_token              VARCHAR(250),           -- tokenized only (spec §8)
  encounter_id               VARCHAR(250),
  appointment_id             VARCHAR(250),

  provider_id                VARCHAR(100),
  location_id                VARCHAR(100),
  department_id              VARCHAR(100),
  service_line_id            VARCHAR(100),

  event_at                   TIMESTAMP,
  feedback_at                TIMESTAMP,
  journey_stage              VARCHAR(100),

  rating_value               DECIMAL(8,3),
  rating_scale_min           DECIMAL(8,3),
  rating_scale_max           DECIMAL(8,3),           -- never merge unlike scales (spec §4.1.D)
  likelihood_to_recommend    DECIMAL(8,3),
  source_metric_code         VARCHAR(100),

  raw_text_ref               VARCHAR(500),           -- pointer to source text (kept separate)
  normalized_text            TEXT,                   -- derived/redacted only (spec §28.11)
  language_code              VARCHAR(20),

  primary_topic_id           VARCHAR(100),           -- convenience; authoritative topics live in topic_fact
  sentiment_label            VARCHAR(30),
  sentiment_score            DECIMAL(8,5),
  emotion_json               TEXT,
  severity                   VARCHAR(30),
  action_required            BOOLEAN,

  classification_confidence  DECIMAL(8,5),
  model_version              VARCHAR(100),           -- versioned inference (spec §28.3)

  event_metadata             TEXT,
  created_at                 TIMESTAMP    NOT NULL,
  updated_at                 TIMESTAMP    NOT NULL,
  UNIQUE(source_system, source_record_id)            -- idempotent (spec §28.5)
);

-- Text segments kept separate from the event (spec §7.2, §28.11)
CREATE TABLE feedback_item (
  feedback_item_id     VARCHAR(64)  PRIMARY KEY,
  experience_event_id  VARCHAR(64)  NOT NULL REFERENCES experience_event(experience_event_id),
  segment_index        INTEGER      NOT NULL DEFAULT 0,
  original_text        TEXT,                          -- source, never mutated
  normalized_text      TEXT,                          -- derived/redacted
  language_code        VARCHAR(20),
  created_at           TIMESTAMP    NOT NULL
);

-- Multi-label topic assignment (spec §7.3) — AI output, separate from source
CREATE TABLE topic_fact (
  topic_fact_id        VARCHAR(64)  PRIMARY KEY,
  experience_event_id  VARCHAR(64)  NOT NULL REFERENCES experience_event(experience_event_id),
  feedback_item_id     VARCHAR(64),
  topic_id             VARCHAR(100) NOT NULL,
  taxonomy_version     VARCHAR(50)  NOT NULL,
  confidence           DECIMAL(8,5),
  span_start           INTEGER,
  span_end             INTEGER,
  evidence_quote_ref   VARCHAR(500),
  classifier_version   VARCHAR(100) NOT NULL,
  created_at           TIMESTAMP    NOT NULL
);

-- Aspect-level sentiment (spec §11.3) — one row per (event, aspect/topic)
CREATE TABLE sentiment_fact (
  sentiment_fact_id    VARCHAR(64)  PRIMARY KEY,
  experience_event_id  VARCHAR(64)  NOT NULL REFERENCES experience_event(experience_event_id),
  topic_id             VARCHAR(100),                 -- null = document-level
  sentiment_label      VARCHAR(30)  NOT NULL,        -- positive|neutral|negative|mixed
  sentiment_score      DECIMAL(8,5),
  model_version        VARCHAR(100) NOT NULL,
  created_at           TIMESTAMP    NOT NULL
);

-- Operational / journey events (spec §7.4, §11.6)
CREATE TABLE operational_event (
  operational_event_id VARCHAR(64)  PRIMARY KEY,
  source_system        VARCHAR(100) NOT NULL,
  source_record_id     VARCHAR(250) NOT NULL,
  patient_token        VARCHAR(250),
  encounter_id         VARCHAR(250),
  appointment_id       VARCHAR(250),
  provider_id          VARCHAR(100),
  location_id          VARCHAR(100),
  service_line_id      VARCHAR(100),
  event_type           VARCHAR(80)  NOT NULL,        -- scheduled|check_in|roomed|clinician_seen|discharge|call|referral|bill_contact
  event_at             TIMESTAMP    NOT NULL,
  journey_stage        VARCHAR(100),
  value_numeric        DECIMAL(14,4),                -- e.g. wait minutes
  metadata_json        TEXT,
  created_at           TIMESTAMP    NOT NULL,
  UNIQUE(source_system, source_record_id)
);

-- Generic analytics fact (spec §7.5) — the metric engine writes here
CREATE TABLE metric_observation (
  metric_observation_id VARCHAR(64) PRIMARY KEY,
  entity_type          VARCHAR(50)  NOT NULL,
  entity_id            VARCHAR(100) NOT NULL,
  metric_id            VARCHAR(100) NOT NULL,
  period_start         DATE         NOT NULL,
  period_end           DATE         NOT NULL,
  numerator            DECIMAL(18,4),
  denominator          DECIMAL(18,4),
  value                DECIMAL(18,6),
  comparison_value     DECIMAL(18,6),
  benchmark_value      DECIMAL(18,6),
  sample_n             INTEGER,
  calculation_version  VARCHAR(50)  NOT NULL,        -- governed definition version
  created_at           TIMESTAMP    NOT NULL,
  UNIQUE(entity_type, entity_id, metric_id, period_start, period_end, calculation_version)
);

-- Issues (spec §11.8) + interventions + measurement (spec §11.14)
CREATE TABLE issue (
  issue_id             VARCHAR(64)  PRIMARY KEY,
  entity_type          VARCHAR(50)  NOT NULL,
  entity_id            VARCHAR(100) NOT NULL,
  metric_id            VARCHAR(100) NOT NULL,
  topic_id             VARCHAR(100),
  current_value        DECIMAL(18,6),
  baseline_value       DECIMAL(18,6),
  relative_change      DECIMAL(18,6),
  sample_n             INTEGER,
  confidence           DECIMAL(8,5),
  severity             VARCHAR(30),
  status               VARCHAR(30)  NOT NULL DEFAULT 'open', -- open|triaged|assigned|resolved|dismissed
  detector_version     VARCHAR(50),
  started_at           DATE,
  created_at           TIMESTAMP    NOT NULL,
  evidence_refs_json   TEXT
);

CREATE TABLE intervention (
  intervention_id      VARCHAR(64)  PRIMARY KEY,
  issue_id             VARCHAR(64)  REFERENCES issue(issue_id),
  target_entity_type   VARCHAR(50),
  target_entity_id     VARCHAR(100),
  target_metric_id     VARCHAR(100),
  target_topic_id      VARCHAR(100),
  intervention_type    VARCHAR(100),
  owner_role           VARCHAR(100),
  hypothesis           TEXT,
  expected_effect      TEXT,
  start_date           DATE,
  end_date             DATE,
  status               VARCHAR(30)  NOT NULL DEFAULT 'planned', -- planned|active|measured|closed
  created_at           TIMESTAMP    NOT NULL
);

CREATE TABLE intervention_measurement (
  measurement_id       VARCHAR(64)  PRIMARY KEY,
  intervention_id      VARCHAR(64)  NOT NULL REFERENCES intervention(intervention_id),
  method               VARCHAR(50),                  -- pre_post|its|did|matched
  baseline_start       DATE, baseline_end DATE,
  post_start           DATE, post_end DATE,
  baseline_value       DECIMAL(18,6),
  post_value           DECIMAL(18,6),
  effect               DECIMAL(18,6),
  confidence           DECIMAL(8,5),
  analysis_version     VARCHAR(50),
  created_at           TIMESTAMP    NOT NULL
);

-- Every ML inference is recorded + versioned (spec §28.3, §12.5)
CREATE TABLE model_inference (
  inference_id         VARCHAR(64)  PRIMARY KEY,
  target_kind          VARCHAR(50)  NOT NULL,        -- experience_event|feedback_item
  target_id            VARCHAR(64)  NOT NULL,
  model_name           VARCHAR(100) NOT NULL,
  model_version        VARCHAR(100) NOT NULL,
  task                 VARCHAR(50)  NOT NULL,        -- topic|sentiment|severity|journey
  output_json          TEXT         NOT NULL,
  created_at           TIMESTAMP    NOT NULL
);

CREATE INDEX ix_event_entity   ON experience_event(location_id, service_line_id, event_at);
CREATE INDEX ix_event_provider ON experience_event(provider_id, event_at);
CREATE INDEX ix_topicfact_evt  ON topic_fact(experience_event_id);
CREATE INDEX ix_metric_lookup  ON metric_observation(metric_id, entity_type, entity_id, period_start);
CREATE INDEX ix_opevent_journey ON operational_event(encounter_id, event_at);
