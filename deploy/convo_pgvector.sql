-- MARTIN conversational memory schema (pgvector). Auto-applied on first container start.
-- Matches jarvis_convo/vectors.py PGVECTOR_DDL (dim 256 = the LocalEmbedding default;
-- change the vector size here AND the EmbeddingClient.dim if you swap in a real embedder).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS convo_memory (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  scope         TEXT NOT NULL DEFAULT 'global',
  text          TEXT NOT NULL,
  provenance    TEXT,
  confidence    REAL,
  ts            DOUBLE PRECISION,
  version       INT DEFAULT 1,
  deleted       BOOLEAN DEFAULT FALSE,
  embedding     vector(256)
);
CREATE INDEX IF NOT EXISTS convo_memory_scope ON convo_memory(scope) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS convo_memory_ann ON convo_memory
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Audit trail (blueprint §13). The app also writes JSONL; this is the durable, queryable copy.
CREATE TABLE IF NOT EXISTS convo_audit (
  action_id       TEXT PRIMARY KEY,
  ts              DOUBLE PRECISION,
  type            TEXT,
  conversation_id TEXT,
  data            JSONB
);
CREATE INDEX IF NOT EXISTS convo_audit_conv ON convo_audit(conversation_id, ts);

-- Connector permission scopes (blueprint §13) — durable grants per principal.
CREATE TABLE IF NOT EXISTS convo_scope (
  principal   TEXT NOT NULL,
  scope       TEXT NOT NULL,
  granted_at  DOUBLE PRECISION,
  PRIMARY KEY (principal, scope)
);
