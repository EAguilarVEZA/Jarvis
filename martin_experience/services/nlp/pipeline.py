"""
NLP enrichment pipeline (spec §11.1-11.3, §29 step 9). Runs topic + aspect-sentiment
classification over canonical events and writes the AI facts — always in SEPARATE tables
(topic_fact, sentiment_fact), never overwriting source (spec §28.2/§28.11). Every inference
is recorded and versioned in model_inference (spec §28.3). Convenience columns on
experience_event (primary_topic_id, sentiment_label/score) are updated for fast filtering.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ...normalization.pipeline import deterministic_id
from .base import NlpResult
from .baseline import BaselineAspectSentiment, BaselineTopicClassifier

TAXONOMY_VERSION = "taxonomy-1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NlpPipeline:
    def __init__(self, store, topic_clf=None, sentiment_clf=None):
        self.store = store
        self.topic = topic_clf or BaselineTopicClassifier()
        self.sentiment = sentiment_clf or BaselineAspectSentiment()

    def run(self, limit: int | None = None) -> dict:
        rows = self.store.query(
            "SELECT experience_event_id, normalized_text FROM experience_event "
            "WHERE normalized_text IS NOT NULL AND normalized_text <> '' "
            + (f"LIMIT {int(limit)}" if limit else ""))
        n_events = n_topics = n_sent = 0
        for r in rows:
            eid, text = r["experience_event_id"], r["normalized_text"]
            topics = self.topic.classify(text)
            aspects, doc = self.sentiment.classify(text, topics)

            for h in topics:
                self.store.upsert("topic_fact", {
                    "topic_fact_id": deterministic_id(eid, h.topic_id, self.topic.version),
                    "experience_event_id": eid, "feedback_item_id": None,
                    "topic_id": h.topic_id, "taxonomy_version": TAXONOMY_VERSION,
                    "confidence": h.confidence, "span_start": h.span_start, "span_end": h.span_end,
                    "evidence_quote_ref": None, "classifier_version": self.topic.version,
                    "created_at": _now(),
                }, conflict_cols=["topic_fact_id"])
                n_topics += 1

            for a in list(aspects) + [doc]:
                self.store.upsert("sentiment_fact", {
                    "sentiment_fact_id": deterministic_id(eid, a.topic_id or "_doc", self.sentiment.version),
                    "experience_event_id": eid, "topic_id": a.topic_id,
                    "sentiment_label": a.label, "sentiment_score": a.score,
                    "model_version": self.sentiment.version, "created_at": _now(),
                }, conflict_cols=["sentiment_fact_id"])
                n_sent += 1

            # versioned inference records (spec §28.3)
            for task, ver, out in (("topic", self.topic.version, [h.__dict__ for h in topics]),
                                   ("sentiment", self.sentiment.version,
                                    {"aspects": [a.__dict__ for a in aspects], "document": doc.__dict__})):
                self.store.upsert("model_inference", {
                    "inference_id": deterministic_id(eid, task, ver),
                    "target_kind": "experience_event", "target_id": eid,
                    "model_name": "baseline", "model_version": ver, "task": task,
                    "output_json": json.dumps(out, default=str), "created_at": _now(),
                }, conflict_cols=["inference_id"])

            # convenience columns (fast filter; authoritative facts live in the fact tables)
            primary = topics[0].topic_id if topics else None
            self.store.conn.execute(
                "UPDATE experience_event SET primary_topic_id=?, sentiment_label=?, "
                "sentiment_score=?, model_version=?, updated_at=? WHERE experience_event_id=?",
                [primary, doc.label, doc.score, self.topic.version, _now(), eid])
            n_events += 1

        self.store.commit()
        return {"events": n_events, "topic_facts": n_topics, "sentiment_facts": n_sent}
