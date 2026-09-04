"""
Vector memory (blueprint §14 vector retrieval, Phase 4). Semantic retrieval over memory items,
with a clean embedding interface and two backends:

  - In-process VectorIndex (default) — works everywhere, no DB.
  - PgVectorBackend — Postgres + pgvector KNN, used when DATABASE_URL is set (production scale).

Embeddings come from a pluggable EmbeddingClient. The default LocalEmbedding is a deterministic
hashed bag-of-tokens vector so retrieval runs offline and in CI; swap in a real local/remote
embedding model by passing a different EmbeddingClient (documented at the bottom).
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass


def _tok(s: str):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


class EmbeddingClient:
    dim = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class LocalEmbedding(EmbeddingClient):
    """Deterministic hashed bag-of-tokens embedding (L2-normalized). No model required.
    Good enough to demonstrate + test the vector path; replace for production semantics."""
    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in _tok(t):
                h = hash(w) % self.dim
                v[h] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))       # inputs are L2-normalized


@dataclass
class VectorItem:
    id: str
    vector: list[float]
    meta: dict


class VectorIndex:
    """In-process KNN. Metadata filter runs BEFORE similarity (mandatory, blueprint §11.15)."""
    def __init__(self, embed: EmbeddingClient | None = None):
        self.embed = embed or LocalEmbedding()
        self.items: dict[str, VectorItem] = {}

    def add(self, id: str, text: str, meta: dict | None = None):
        vec = self.embed.embed([text])[0]
        self.items[id] = VectorItem(id, vec, meta or {})

    def remove(self, id: str):
        self.items.pop(id, None)

    def search(self, query: str, k: int = 5, where: dict | None = None) -> list[tuple[str, float]]:
        qv = self.embed.embed([query])[0]
        cand = []
        for it in self.items.values():
            if where and any(it.meta.get(kk) != vv for kk, vv in where.items()):
                continue                              # metadata filter first
            cand.append((it.id, cosine(qv, it.vector)))
        cand.sort(key=lambda x: x[1], reverse=True)
        return cand[:k]


# ── Production backend (Postgres + pgvector) ─────────────────────────────────
PGVECTOR_DDL = """
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
  embedding     vector(%(dim)s)
);
CREATE INDEX IF NOT EXISTS convo_memory_scope ON convo_memory(scope) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS convo_memory_ann ON convo_memory
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
"""


class PgVectorBackend:
    """Postgres + pgvector adapter. Active only when a DATABASE_URL is configured and psycopg
    is importable; otherwise callers fall back to the in-process VectorIndex. KNN via the
    cosine operator `<=>`. Metadata (scope) filters in the WHERE clause before ANN."""
    def __init__(self, dsn: str | None = None, embed: EmbeddingClient | None = None):
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        self.embed = embed or LocalEmbedding()
        self._conn = None

    def available(self) -> bool:
        if not self.dsn:
            return False
        try:
            import psycopg  # noqa
            return True
        except Exception:
            return False

    def _c(self):
        if self._conn is None:
            import psycopg
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            self._conn.execute(PGVECTOR_DDL % {"dim": self.embed.dim})
        return self._conn

    def upsert(self, item: dict):
        vec = self.embed.embed([item["text"]])[0]
        self._c().execute(
            "INSERT INTO convo_memory (id,kind,scope,text,provenance,confidence,ts,version,deleted,embedding) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET text=EXCLUDED.text, embedding=EXCLUDED.embedding, "
            "deleted=EXCLUDED.deleted, version=EXCLUDED.version",
            (item["id"], item["kind"], item.get("scope", "global"), item["text"],
             item.get("provenance"), item.get("confidence"), item.get("ts"),
             item.get("version", 1), item.get("deleted", False), vec))

    def search(self, query: str, scope: str | None = None, k: int = 5) -> list[dict]:
        qv = self.embed.embed([query])[0]
        where = "deleted = FALSE" + (" AND (scope=%s OR scope='global')" if scope else "")
        params = ([scope] if scope else []) + [qv, k]
        rows = self._c().execute(
            f"SELECT id, kind, scope, text, provenance, ts, 1-(embedding <=> %s) AS score "
            f"FROM convo_memory WHERE {where} ORDER BY embedding <=> %s LIMIT %s",
            (params[0], qv, k) if scope else (qv, qv, k)).fetchall()
        return [{"id": r[0], "kind": r[1], "scope": r[2], "text": r[3],
                 "provenance": r[4], "ts": r[5], "score": r[6]} for r in rows]


from dataclasses import asdict
from .memory import MemoryStore, _tokens


class SemanticMemoryStore(MemoryStore):
    """MemoryStore with semantic retrieval. Uses PgVector when available, else the in-process
    index, and blends vector similarity with keyword overlap + preference boost so both a
    paraphrase and an exact term find the right memory."""
    def __init__(self, embed: EmbeddingClient | None = None, pg: PgVectorBackend | None = None):
        super().__init__()
        self.index = VectorIndex(embed)
        self.pg = pg if (pg and pg.available()) else None

    def commit(self, item):
        r = super().commit(item)
        payload = item.text + " " + " ".join(item.tags)
        if self.pg:
            try: self.pg.upsert(asdict(item))
            except Exception: self.index.add(item.id, payload, {"scope": item.scope, "kind": item.kind})
        else:
            self.index.add(item.id, payload, {"scope": item.scope, "kind": item.kind})
        return r

    def forget(self, item_id):
        r = super().forget(item_id)
        if r and not self.pg:
            self.index.remove(item_id)
        return r

    def retrieve(self, query, scope=None, k=5, min_score=0.0):
        qt = _tokens(query)
        vscores: dict[str, float] = {}
        if self.pg:
            try:
                for row in self.pg.search(query, scope, k * 3):
                    vscores[row["id"]] = row["score"]
            except Exception:
                pass
        if not vscores:
            for i, sc in self.index.search(query, k * 3):
                vscores[i] = sc
        results = []
        for it in self.items.values():
            if it.deleted:
                continue
            if scope and it.scope not in (scope, "global"):
                continue
            vs = vscores.get(it.id, 0.0)
            kw = len(qt & (_tokens(it.text) | {t.lower() for t in it.tags}))
            # preferences are always retrievable (so the ContextBuilder can surface them),
            # but with a small boost so a genuinely relevant content item still ranks above.
            pref = 0.15 if it.kind == "preference" else 0.0
            if vs <= 0 and kw == 0 and it.kind != "preference":
                continue
            score = 0.7 * vs + 0.3 * min(kw, 3) / 3.0 + pref
            results.append((score, it))
        results.sort(key=lambda s: s[0], reverse=True)
        return [it for _, it in results[:k]]


class DurableSemanticMemory(SemanticMemoryStore):
    """Production memory used by the live app: JSON-durable (survives restart) + in-process
    semantic retrieval now, and mirrors every write to pgvector when DATABASE_URL is set.
    Retrieval always uses the in-process index (rebuilt from JSON on start), so it never
    depends on a live DB round-trip — the DB is the scalable, queryable copy."""
    def __init__(self, path: str, embed: EmbeddingClient | None = None, pg: PgVectorBackend | None = None):
        super().__init__(embed=embed, pg=pg)
        self.path = path
        self._load_json()

    def _load_json(self):
        try:
            if self.path and os.path.exists(self.path):
                from .memory import MemoryItem
                for d in json.load(open(self.path, encoding="utf-8")).get("items", []):
                    it = MemoryItem(**d)
                    self.items[it.id] = it
                    if not it.deleted:
                        self.index.add(it.id, it.text + " " + " ".join(it.tags),
                                       {"scope": it.scope, "kind": it.kind})
        except Exception:
            pass

    def _persist_json(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": [asdict(i) for i in self.items.values()]}, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def commit(self, item):
        r = super().commit(item); self._persist_json(); return r

    def forget(self, item_id):
        r = super().forget(item_id); self._persist_json(); return r

    def correct(self, item_id, new_text):
        r = super().correct(item_id, new_text); self._persist_json(); return r


# To use a REAL embedding model in production, implement EmbeddingClient.embed() against your
# local embedder (e.g. an OpenAI-compatible /embeddings endpoint or a sentence-transformers
# model) and pass it into SemanticMemoryStore / VectorIndex / PgVectorBackend. Nothing else
# changes — the retrieval logic is embedding-agnostic.
