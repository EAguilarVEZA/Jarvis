"""
Anomaly / early-warning engine (spec §11.8). Generates issue candidates only when a
governed rule's gates are met: effect threshold AND minimum sample AND (persistence OR
severity). Deterministic and versioned — the LLM never decides what an issue is.

Two rule types:
  - level:  a metric standing beyond an absolute threshold (e.g. negative_topic_rate ≥ 0.5)
            with adequate sample → a standing problem (severity gate).
  - change: a period-over-period move beyond a threshold with adequate sample in BOTH
            windows → an emerging problem.

Real statistical detectors (EWMA / CUSUM / robust z / change-point, spec §11.8) attach here
via MARTIN's existing experiments_api; this baseline keeps the pipeline runnable + testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...normalization.pipeline import deterministic_id
from ..metrics import engine as metrics

DETECTOR_VERSION = "anomaly-baseline-1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Rule:
    metric_id: str
    rule_type: str                 # level | change
    op: str                        # ">=" | "<=" for level; "<=" (drop) / ">=" (rise) for change
    threshold: float               # level: absolute value; change: relative_change bound
    min_sample: int = 20
    higher_is_better: bool = True


# Governed defaults. min_sample kept small here for the fixture; production values are
# governed per metric and enforce minimum cell-size policy (spec §28.13).
DEFAULT_RULES: list[Rule] = [
    Rule("negative_topic_rate", "level", ">=", 0.50, min_sample=2, higher_is_better=False),
    Rule("avg_rating", "change", "<=", -0.10, min_sample=2, higher_is_better=True),
    Rule("bottom_box_rate", "level", ">=", 0.40, min_sample=2, higher_is_better=False),
]


def _severity(effect: float) -> str:
    a = abs(effect)
    return "high" if a >= 0.75 else "medium" if a >= 0.4 else "low"


def detect_issues(store, entity_type: str, cur_start: str, cur_end: str,
                  prior_start: str | None = None, prior_end: str | None = None,
                  rules: list[Rule] | None = None, persist: bool = True) -> list[dict]:
    rules = rules or DEFAULT_RULES
    col = metrics._entity_col(entity_type)
    entities = [r[col] for r in store.query(
        f"SELECT DISTINCT {col} FROM experience_event e WHERE {col} IS NOT NULL "
        f"AND substr(COALESCE(e.feedback_at,e.event_at),1,10) BETWEEN ? AND ?",
        [cur_start, cur_end])]
    out: list[dict] = []
    for eid in entities:
        for rule in rules:
            m = metrics.METRICS[rule.metric_id]
            val, _num, _den, n = metrics._value(store, m, entity_type, eid, cur_start, cur_end)
            if val is None or n < rule.min_sample:
                continue                              # minimum-sample gate (spec §11.8)
            issue = None
            if rule.rule_type == "level":
                tripped = (val >= rule.threshold) if rule.op == ">=" else (val <= rule.threshold)
                if tripped:
                    issue = dict(current=val, baseline=None, rel=None,
                                 effect=val, severity=_severity(val))
            elif rule.rule_type == "change" and prior_start:
                cmp = metrics.compare_periods(store, rule.metric_id, entity_type, eid,
                                              cur_start, cur_end, prior_start, prior_end)
                rel = cmp["relative_change"]
                if rel is not None and cmp["n_comparison"] >= rule.min_sample:
                    tripped = (rel <= rule.threshold) if rule.op == "<=" else (rel >= rule.threshold)
                    if tripped:
                        issue = dict(current=cmp["current"], baseline=cmp["comparison"], rel=rel,
                                     effect=rel, severity=_severity(rel))
            if not issue:
                continue
            row = {
                "issue_id": deterministic_id(entity_type, eid, rule.metric_id, cur_start, cur_end, DETECTOR_VERSION),
                "entity_type": entity_type, "entity_id": eid, "metric_id": rule.metric_id,
                "topic_id": None, "current_value": issue["current"], "baseline_value": issue["baseline"],
                "relative_change": issue["rel"], "sample_n": n,
                "confidence": round(min(0.99, 0.5 + 0.08 * n), 3),
                "severity": issue["severity"], "status": "open", "detector_version": DETECTOR_VERSION,
                "started_at": cur_start, "created_at": _now(), "evidence_refs_json": None,
            }
            if persist:
                store.upsert("issue", row, conflict_cols=["issue_id"])
            out.append(row)
    if persist:
        store.commit()
    return sorted(out, key=lambda r: (r["severity"] != "high", -(r["sample_n"] or 0)))
