"""
Tool registry (spec §12.3). Each tool is a governed, versioned, DETERMINISTIC function over
the canonical model — the only way the orchestrator gets facts. The LLM selects and narrates
tool outputs; it never computes them. These wrap the engines already built (metrics, anomaly,
investigation) so there is one source of truth.
"""
from __future__ import annotations

from ..metrics import engine as metrics
from ..metrics.engine import METRICS
from ..anomaly import engine as anomaly
from ..investigation.engine import investigate_root_cause


def get_metric(store, metric_id, entity_type, entity_id, start, end):
    val, num, den, n = metrics._value(store, METRICS[metric_id], entity_type, entity_id, start, end)
    return {"metric_id": metric_id, "entity": {"type": entity_type, "id": entity_id},
            "value": val, "sample_n": n, "calculation_version": METRICS[metric_id].version}


def compare_metric(store, metric_id, entity_type, entity_id, cur_start, cur_end, prior_start, prior_end):
    return metrics.compare_periods(store, metric_id, entity_type, entity_id,
                                   cur_start, cur_end, prior_start, prior_end)


def scorecard(store, entity_type, start, end):
    metrics.rollup_period(store, entity_type, start, end)
    return {"period": f"{start}–{end}", "rows": metrics.scorecard(store, entity_type, start, end)}


def get_topic_distribution(store, start, end, entity_type=None, entity_id=None, negative_only=False):
    where = "substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ?"
    params = [start, end]
    if entity_type and entity_id:
        where += f" AND e.{metrics._entity_col(entity_type)}=?"; params.append(entity_id)
    join_neg = ("JOIN sentiment_fact s ON s.experience_event_id=e.experience_event_id "
                "AND s.topic_id=tf.topic_id AND s.sentiment_label='negative'") if negative_only else ""
    rows = store.query(
        f"SELECT tf.topic_id, t.name, COUNT(DISTINCT tf.experience_event_id) AS events "
        f"FROM topic_fact tf JOIN experience_event e ON e.experience_event_id=tf.experience_event_id "
        f"LEFT JOIN topic t ON t.topic_id=tf.topic_id {join_neg} "
        f"WHERE {where} GROUP BY tf.topic_id ORDER BY events DESC, tf.topic_id LIMIT 15", params)
    return {"topics": rows}


def find_anomalies(store, entity_type, start, end, prior_start=None, prior_end=None):
    return {"issues": anomaly.detect_issues(store, entity_type, start, end, prior_start, prior_end)}


def investigate(store, entity_type, entity_id, cur_start, cur_end, prior_start=None, prior_end=None, question=None):
    return investigate_root_cause(store, entity_type, entity_id, cur_start, cur_end,
                                  prior_start, prior_end, question)


def retrieve_feedback_examples(store, start, end, topic_id=None, entity_type=None, entity_id=None,
                               sentiment=None, limit=5):
    where = ["substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ?"]
    params = [start, end]
    join = ""
    if topic_id:
        join = "JOIN sentiment_fact s ON s.experience_event_id=e.experience_event_id AND s.topic_id=?"
        params.insert(0, topic_id)
        if sentiment:
            join += " AND s.sentiment_label=?"; params.insert(1, sentiment)
    if entity_type and entity_id:
        where.append(f"e.{metrics._entity_col(entity_type)}=?"); params.append(entity_id)
    rows = store.query(
        f"SELECT DISTINCT e.experience_event_id, e.source_record_id, e.rating_value, e.normalized_text "
        f"FROM experience_event e {join} WHERE {' AND '.join(where)} "
        f"ORDER BY e.rating_value ASC LIMIT ?", params + [limit])
    return {"examples": [{"evidence_ref": f"event:{r['experience_event_id']}",
                          "source_record_id": r["source_record_id"], "rating": r["rating_value"],
                          "quote": r["normalized_text"]} for r in rows]}


def get_entity_hierarchy(store, entity_type, entity_id):
    return {"entity": store.query(
        "SELECT entity_type, entity_id, display_name, parent_entity_id, specialty "
        "FROM entity_dimension WHERE entity_type=? AND entity_id=? AND is_current=1",
        [entity_type, entity_id])}


# name → (callable, one-line description) for planning + audit
REGISTRY = {
    "get_metric": (get_metric, "Get one governed metric value for an entity + period."),
    "compare_metric": (compare_metric, "Compare a metric across two periods."),
    "scorecard": (scorecard, "Unified experience scorecard for entities in a period."),
    "get_topic_distribution": (get_topic_distribution, "Topic prevalence (optionally negative-only)."),
    "find_anomalies": (find_anomalies, "Detect issue candidates via governed rules."),
    "investigate_root_cause": (investigate, "Structured FindingBundle: drivers + evidence."),
    "retrieve_feedback_examples": (retrieve_feedback_examples, "Representative feedback quotes."),
    "get_entity_hierarchy": (get_entity_hierarchy, "Entity dimension + hierarchy."),
}
