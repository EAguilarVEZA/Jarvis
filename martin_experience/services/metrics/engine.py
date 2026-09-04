"""
Deterministic metric engine (spec §11.7). Rolls canonical `experience_event` up into the
generic `metric_observation` fact (§7.5), computes period-over-period change (the "what
changed?" answer, §1), and assembles a unified scorecard (§18).

Non-negotiables encoded (spec §28): the LLM never computes these — this is plain SQL.
Every metric has a governed, versioned definition (`calculation_version`); raw regulated
values (star ratings) are never merged across unlike scales. `metric_observation` is
idempotent on (entity, metric, period, calc_version). This is also the seam where MARTIN's
existing `experiments_api` (anomaly, driver, difference-of-periods) attaches.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...normalization.pipeline import deterministic_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MetricDef:
    metric_id: str
    name: str
    kind: str            # count | mean | rate
    version: str = "1.0"
    field: str = "rating_value"
    numerator_predicate: str = ""     # for rate/count-with-filter, over experience_event `e`
    higher_is_better: bool = True
    scale: str = ""


# Governed metric registry (data, not hard-coded UI). Star-scale rates use the 1..5 source.
METRICS: dict[str, MetricDef] = {
    "review_count": MetricDef("review_count", "Review count", "count",
                              higher_is_better=True, scale="count"),
    "avg_rating": MetricDef("avg_rating", "Average rating", "mean", field="rating_value",
                            higher_is_better=True, scale="1-5"),
    "top_box_rate": MetricDef("top_box_rate", "Top-box rate (5★)", "rate",
                              numerator_predicate="e.rating_value >= 5", scale="0-1"),
    "bottom_box_rate": MetricDef("bottom_box_rate", "Bottom-box rate (≤2★)", "rate",
                                 numerator_predicate="e.rating_value <= 2",
                                 higher_is_better=False, scale="0-1"),
    "negative_topic_rate": MetricDef("negative_topic_rate", "Negative-topic rate", "rate",
                                     numerator_predicate=(
                                         "EXISTS (SELECT 1 FROM sentiment_fact s "
                                         "WHERE s.experience_event_id = e.experience_event_id "
                                         "AND s.sentiment_label='negative')"),
                                     higher_is_better=False, scale="0-1"),
}

# Filter events into a period for one entity. feedback_at is ISO 'YYYY-MM-DD...'.
_WHERE = ("e.{col} = ? AND substr(COALESCE(e.feedback_at, e.event_at),1,10) "
          "BETWEEN ? AND ?")


def _entity_col(entity_type: str) -> str:
    return {"location": "location_id", "provider": "provider_id",
            "service_line": "service_line_id", "department": "department_id"}[entity_type]


def _value(store, m: MetricDef, entity_type: str, entity_id: str,
           start: str, end: str) -> tuple[float | None, float | None, float | None, int]:
    """Return (value, numerator, denominator, sample_n) for one metric/entity/period."""
    col = _entity_col(entity_type)
    where = _WHERE.format(col=col)
    n = store.scalar(f"SELECT COUNT(*) FROM experience_event e WHERE {where}",
                     [entity_id, start, end]) or 0
    if n == 0:
        return None, None, 0, 0
    if m.kind == "count":
        return float(n), float(n), None, n
    if m.kind == "mean":
        v = store.scalar(f"SELECT AVG(e.{m.field}) FROM experience_event e WHERE {where}",
                         [entity_id, start, end])
        return (round(v, 4) if v is not None else None), None, None, n
    if m.kind == "rate":
        num = store.scalar(
            f"SELECT COUNT(*) FROM experience_event e "
            f"WHERE {where} AND ({m.numerator_predicate})",
            [entity_id, start, end]) or 0
        return round(num / n, 5), float(num), float(n), n
    raise ValueError(f"unknown metric kind {m.kind}")


def rollup_period(store, entity_type: str, start: str, end: str,
                  metric_ids: list[str] | None = None) -> int:
    """Compute every metric for every entity that had events in [start,end] and persist
    to metric_observation. Idempotent. Returns rows written."""
    col = _entity_col(entity_type)
    entities = [r[col] for r in store.query(
        f"SELECT DISTINCT {col} FROM experience_event e "
        f"WHERE {col} IS NOT NULL AND substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ?",
        [start, end])]
    mids = metric_ids or list(METRICS)
    written = 0
    for eid in entities:
        for mid in mids:
            m = METRICS[mid]
            val, num, den, n = _value(store, m, entity_type, eid, start, end)
            if n == 0:
                continue
            store.upsert("metric_observation", {
                "metric_observation_id": deterministic_id(entity_type, eid, mid, start, end, m.version),
                "entity_type": entity_type, "entity_id": eid, "metric_id": mid,
                "period_start": start, "period_end": end,
                "numerator": num, "denominator": den, "value": val,
                "comparison_value": None, "benchmark_value": None, "sample_n": n,
                "calculation_version": m.version, "created_at": _now(),
            }, conflict_cols=["entity_type", "entity_id", "metric_id",
                              "period_start", "period_end", "calculation_version"])
            written += 1
    store.commit()
    return written


def compare_periods(store, metric_id: str, entity_type: str, entity_id: str,
                    cur_start: str, cur_end: str, prior_start: str, prior_end: str) -> dict:
    """Period-over-period change with sample sizes — the shape the FindingBundle/issue needs."""
    m = METRICS[metric_id]
    cur, _, _, n_cur = _value(store, m, entity_type, entity_id, cur_start, cur_end)
    prior, _, _, n_prior = _value(store, m, entity_type, entity_id, prior_start, prior_end)
    rel = None
    if cur is not None and prior not in (None, 0):
        rel = round((cur - prior) / abs(prior), 4)
    return {
        "metric_id": metric_id, "entity": {"type": entity_type, "id": entity_id},
        "current": cur, "comparison": prior, "relative_change": rel,
        "n_current": n_cur, "n_comparison": n_prior,
        "method": "difference_of_periods", "calculation_version": m.version,
        "higher_is_better": m.higher_is_better,
    }


def scorecard(store, entity_type: str, start: str, end: str) -> list[dict]:
    """Unified scorecard (§18): all metrics per entity for the period. Reads the facts the
    rollup wrote, joined to the entity display name."""
    rows = store.query(
        "SELECT mo.entity_id, d.display_name, mo.metric_id, mo.value, mo.sample_n "
        "FROM metric_observation mo LEFT JOIN entity_dimension d "
        "  ON d.entity_type=mo.entity_type AND d.entity_id=mo.entity_id AND d.is_current=1 "
        "WHERE mo.entity_type=? AND mo.period_start=? AND mo.period_end=? "
        "ORDER BY mo.entity_id, mo.metric_id", [entity_type, start, end])
    by_entity: dict[str, dict] = {}
    for r in rows:
        e = by_entity.setdefault(r["entity_id"], {
            "entity_id": r["entity_id"], "name": r["display_name"], "metrics": {}})
        e["metrics"][r["metric_id"]] = {"value": r["value"], "n": r["sample_n"]}
    return list(by_entity.values())
