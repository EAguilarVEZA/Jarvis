"""
Memory + personalization (blueprint §11, §4.3). Memory makes the assistant more effective
without becoming intrusive: it retrieves ONLY what's relevant to the current task, rather than
stuffing everything into the prompt (§11, Non-Goals). Every item carries provenance, timestamp,
confidence, and scope, and supports update/deletion (§4.3, §13).

Kinds (§11):
  episodic    prior decisions, plans, artifacts, unfinished work
  preference  durable communication / workflow preferences
  project     scoped knowledge for a named project/company/task
  fact        a retrieved/known fact with provenance

Writes are explicit or governed by conservative relevance rules (§11): propose() surfaces a
candidate; commit() persists it. Nothing enters retrievable memory until committed.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

_STOP = set("the a an of to for and or with your you our we i how what did do does is are was "
            "were be on in at it this that about make made set let".split())


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 2}


@dataclass
class MemoryItem:
    id: str
    kind: str                       # episodic | preference | project | fact
    text: str
    scope: str = "global"           # "global" or a project id
    tags: list = field(default_factory=list)
    provenance: str = "conversation"
    confidence: float = 0.9
    ts: float = field(default_factory=time.time)
    version: int = 1
    deleted: bool = False
    superseded_by: str | None = None

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "text": self.text, "scope": self.scope,
                "provenance": self.provenance, "confidence": self.confidence,
                "ts": self.ts, "version": self.version}


class MemoryStore:
    def __init__(self):
        self.items: dict[str, MemoryItem] = {}

    # ── governed writes (§11) ────────────────────────────────────────────────
    def propose(self, kind, text, scope="global", tags=None, provenance="conversation",
                confidence=0.9) -> MemoryItem:
        """Return a candidate — NOT yet retrievable until commit()."""
        return MemoryItem(id=f"mem-{uuid.uuid4().hex[:8]}", kind=kind, text=text, scope=scope,
                          tags=tags or [], provenance=provenance, confidence=confidence)

    def commit(self, item: MemoryItem) -> MemoryItem:
        self.items[item.id] = item
        return item

    def add(self, *a, **k) -> MemoryItem:
        return self.commit(self.propose(*a, **k))

    # ── user controls (§4.3, §13) ────────────────────────────────────────────
    def forget(self, item_id: str) -> bool:
        it = self.items.get(item_id)
        if it:
            it.deleted = True
            return True
        return False

    def correct(self, item_id: str, new_text: str) -> MemoryItem | None:
        old = self.items.get(item_id)
        if not old:
            return None
        new = self.propose(old.kind, new_text, old.scope, old.tags, "correction", old.confidence)
        new.version = old.version + 1
        self.commit(new)
        old.deleted = True
        old.superseded_by = new.id
        return new

    # ── retrieval (§11: only relevant; not indiscriminate stuffing) ──────────
    def retrieve(self, query: str, scope: str | None = None, k: int = 5,
                 min_score: float = 1.0) -> list[MemoryItem]:
        qt = _tokens(query)
        now = time.time()
        scored: list[tuple[float, MemoryItem]] = []
        for it in self.items.values():
            if it.deleted:
                continue
            if scope and it.scope not in (scope, "global"):
                continue
            overlap = len(qt & (_tokens(it.text) | {t.lower() for t in it.tags}))
            if overlap == 0 and it.kind != "preference":
                continue                                   # preferences may always apply
            recency = 0.3 * (1.0 / (1.0 + (now - it.ts) / 86400.0))  # gentle recency
            pref_boost = 0.5 if it.kind == "preference" else 0.0
            score = overlap + recency + pref_boost + 0.2 * it.confidence
            if score >= min_score or (it.kind == "preference" and overlap == 0 and scope):
                scored.append((score, it))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [it for _, it in scored[:k]]

    def active(self, kind: str | None = None, scope: str | None = None) -> list[MemoryItem]:
        return [it for it in self.items.values() if not it.deleted
                and (kind is None or it.kind == kind)
                and (scope is None or it.scope == scope)]


class ContextBuilder:
    """Assembles the turn context (§16): recent verbatim turns + relevant long-term memory.
    Corrections and preferences are surfaced explicitly so the planner honors them."""
    def __init__(self, memory: MemoryStore):
        self.memory = memory

    def build(self, working_memory, query: str, scope: str | None = None) -> dict:
        recent = [{"role": m.role, "text": m.text, "kind": m.kind}
                  for m in working_memory.authoritative_context()[-8:]]
        relevant = self.memory.retrieve(query, scope=scope, k=5)
        return {
            "query": query,
            "recent_turns": recent,
            "relevant_memory": [it.to_dict() for it in relevant],
            "preferences": [it.text for it in relevant if it.kind == "preference"],
            "scope": scope,
        }
