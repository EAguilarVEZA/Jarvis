"""
Root-cause investigation engine (spec §11.10). Chains the deterministic engines into a
structured FindingBundle (spec §2.4 / CLAUDE.md answer contract):

  1. detect metric change (period comparison)
  2. decompose by topic (which topics drive negativity now)
  3. retrieve representative feedback as evidence
  4. rank candidate contributors
  5. emit a FindingBundle

The LLM later turns this bundle into narrative and MUST NOT add facts not present here
(spec §1, §12.1). Every number here comes from SQL over the canonical model.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..metrics import engine as metrics
from ..metrics.engine import METRICS

_DELTA_METRICS = ["avg_rating", "negative_topic_rate", "bottom_box_rate"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entity_name(store, entity_type: str, entity_id: str) -> str:
    return store.scalar(
        "SELECT display_name FROM entity_dimension WHERE entity_type=? AND entity_id=? AND is_current=1",
        [entity_type, entity_id]) or entity_id


def _topic_drivers(store, entity_type: str, entity_id: str, start: str, end: str, limit: int = 5):
    """Negative aspect prevalence by topic in the window → ranked candidate drivers."""
    col = metrics._entity_col(entity_type)
    rows = store.query(
        f"SELECT s.topic_id, t.name, COUNT(DISTINCT s.experience_event_id) AS neg_events "
        f"FROM sentiment_fact s "
        f"JOIN experience_event e ON e.experience_event_id = s.experience_event_id "
        f"LEFT JOIN topic t ON t.topic_id = s.topic_id "
        f"WHERE s.sentiment_label='negative' AND s.topic_id IS NOT NULL "
        f"  AND e.{col}=? AND substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ? "
        f"GROUP BY s.topic_id ORDER BY neg_events DESC, s.topic_id LIMIT ?",
        [entity_id, start, end, limit])
    total = store.scalar(
        f"SELECT COUNT(*) FROM experience_event e WHERE e.{col}=? "
        f"AND substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ?",
        [entity_id, start, end]) or 1
    return [{"topic_id": r["topic_id"], "name": r["name"] or r["topic_id"],
             "negative_events": r["neg_events"], "share": round(r["neg_events"] / total, 3)}
            for r in rows]


def _evidence(store, entity_type: str, entity_id: str, start: str, end: str,
              topic_ids: list[str], limit: int = 4):
    """Representative negative feedback for the top drivers (evidence trace, spec §2.4)."""
    if not topic_ids:
        return []
    col = metrics._entity_col(entity_type)
    ph = ",".join("?" for _ in topic_ids)
    rows = store.query(
        f"SELECT DISTINCT e.experience_event_id, e.source_record_id, e.rating_value, e.normalized_text "
        f"FROM experience_event e JOIN sentiment_fact s ON s.experience_event_id=e.experience_event_id "
        f"WHERE s.sentiment_label='negative' AND s.topic_id IN ({ph}) "
        f"  AND e.{col}=? AND substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ? "
        f"ORDER BY e.rating_value ASC LIMIT ?",
        [*topic_ids, entity_id, start, end, limit])
    return [{"evidence_ref": f"event:{r['experience_event_id']}",
             "source_record_id": r["source_record_id"], "rating": r["rating_value"],
             "quote": r["normalized_text"]} for r in rows]


def investigate_root_cause(store, entity_type: str, entity_id: str,
                           cur_start: str, cur_end: str,
                           prior_start: str | None = None, prior_end: str | None = None,
                           question: str | None = None) -> dict:
    name = _entity_name(store, entity_type, entity_id)

    findings = []
    for mid in _DELTA_METRICS:
        if prior_start:
            cmp = metrics.compare_periods(store, mid, entity_type, entity_id,
                                          cur_start, cur_end, prior_start, prior_end)
        else:
            val, _, _, n = metrics._value(store, METRICS[mid], entity_type, entity_id, cur_start, cur_end)
            cmp = {"current": val, "comparison": None, "relative_change": None,
                   "n_current": n, "n_comparison": 0, "method": "level"}
        findings.append({
            "type": "metric_change", "metric_id": mid,
            "current": cmp["current"], "comparison": cmp["comparison"],
            "relative_change": cmp["relative_change"],
            "n_current": cmp["n_current"], "n_comparison": cmp["n_comparison"],
            "method": cmp["method"], "higher_is_better": METRICS[mid].higher_is_better,
            "calculation_version": METRICS[mid].version,
            "evidence_refs": [f"metric:{mid}:{entity_type}:{entity_id}:{cur_start}:{cur_end}"],
        })

    drivers = _topic_drivers(store, entity_type, entity_id, cur_start, cur_end)
    evidence = _evidence(store, entity_type, entity_id, cur_start, cur_end,
                         [d["topic_id"] for d in drivers[:3]])

    limitations = ["Associations shown are correlational, not causal, unless a valid design is applied."]
    n_cur = findings[0]["n_current"] if findings else 0
    if n_cur and n_cur < 30:
        limitations.append(f"Small sample (n={n_cur}); treat magnitudes as directional.")

    return {
        "question": question or f"What is driving experience at {name}?",
        "entity": {"type": entity_type, "id": entity_id, "name": name},
        "period": {"start": cur_start, "end": cur_end},
        "comparison_period": ({"start": prior_start, "end": prior_end} if prior_start else None),
        "findings": findings,
        "candidate_drivers": drivers,
        "feedback_evidence": evidence,
        "limitations": limitations,
        "generated_at": _now(),
        "engine_versions": {
            "metric": "1.0", "detector": "anomaly-baseline-1.0",
            "topic": "topic-baseline-1.0", "sentiment": "sentiment-baseline-1.0",
        },
    }
