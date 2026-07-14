"""Metric Lab — governed metric definitions + a deterministic BigQuery SQL compiler.

This is the "define once, reuse everywhere" metrics layer (the Tableau Pulse / dbt /
Cube / Looker pattern): a metric is a named, governed definition — a measure, an
aggregation, a time dimension, fixed definition filters, breakdown dimensions, a
format and a favorable direction. A deterministic compiler turns a definition +
runtime context (grain, breakdown, extra filters, time window) into BigQuery SQL, so
the number means the same thing everywhere and an AI agent can select from a menu
instead of writing raw SQL.

Store: metric_definitions.json (list). Pure functions here are unit-tested; the
FastAPI layer (semantic_api) resolves table/column metadata from the semantic model
and executes the compiled SQL via the existing governed executor.
"""
from __future__ import annotations

import os
import re
import json
import time
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_DIR, "metric_definitions.json")

AGGREGATIONS = ["sum", "avg", "median", "min", "max", "count", "count_distinct"]
GRAINS = ["day", "week", "month", "quarter", "year"]
_GRAIN_SQL = {"day": "DAY", "week": "WEEK", "month": "MONTH", "quarter": "QUARTER", "year": "YEAR"}
_OP_SQL = {"eq": "=", "neq": "!=", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("metrics", [])
    except Exception:
        return []


def _save(metrics):
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"metrics": metrics}, f, indent=2)
    os.replace(tmp, _PATH)


def list_metrics():
    return _load()


def get_metric(mid):
    for m in _load():
        if m.get("id") == mid:
            return m
    return None


def upsert_metric(d: dict) -> dict:
    metrics = _load()
    d = dict(d)
    d["id"] = d.get("id") or ("met_" + uuid.uuid4().hex[:10])
    d.setdefault("created_at", time.time())
    d["updated_at"] = time.time()
    for i, m in enumerate(metrics):
        if m.get("id") == d["id"]:
            metrics[i] = d
            break
    else:
        metrics.append(d)
    _save(metrics)
    return d


def delete_metric(mid):
    metrics = [m for m in _load() if m.get("id") != mid]
    _save(metrics)
    return True


# ---------------------------------------------------------------------------
# SQL compiler (pure) — BigQuery dialect
# ---------------------------------------------------------------------------
def _col(name: str) -> str:
    """Safe column/identifier reference — backticked, only word chars/dots allowed."""
    name = str(name or "")
    if not re.match(r'^[A-Za-z0-9_.]+$', name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return "`" + name.replace("`", "") + "`" if "." not in name else name


def _lit(v) -> str:
    """Quote a filter literal safely (number as-is; else single-quoted, escaped)."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.match(r'^-?\d+(\.\d+)?$', s):
        return s
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _predicate(col_sql: str, op: str, value) -> str:
    op = (op or "eq").lower()
    if op == "is_null":
        return f"{col_sql} IS NULL"
    if op == "not_null":
        return f"{col_sql} IS NOT NULL"
    if op == "in":
        vals = value if isinstance(value, list) else [value]
        return f"{col_sql} IN ({', '.join(_lit(v) for v in vals)})"
    if op == "like":
        return f"{col_sql} LIKE {_lit(value)}"
    if op in _OP_SQL:
        return f"{col_sql} {_OP_SQL[op]} {_lit(value)}"
    raise ValueError(f"unknown operator: {op}")


def _measure_sql(aggregation: str, measure_sql: str) -> str:
    agg = (aggregation or "sum").lower()
    if agg == "count":
        return f"COUNT({measure_sql})"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {measure_sql})"
    if agg == "median":
        return f"APPROX_QUANTILES({measure_sql}, 2)[OFFSET(1)]"
    if agg in ("sum", "avg", "min", "max"):
        return f"{agg.upper()}({measure_sql})"
    raise ValueError(f"unknown aggregation: {agg}")


def compile_metric_sql(*, raw_table: str, measure_col: str, aggregation: str,
                       time_col: str, grain: str = "month",
                       expression: str = "", filters: list = None,
                       breakdown_col: str = "", limit: int = 500) -> str:
    """Compile a metric into a BigQuery time-series SQL string.
    `filters` = list of {column, op, value} (definition + scoped, already merged)."""
    if grain not in _GRAIN_SQL:
        grain = "month"
    measure_ref = expression.strip() if (expression or "").strip() else _col(measure_col)
    agg_sql = _measure_sql(aggregation, measure_ref)
    time_ref = _col(time_col)
    period = f"DATE_TRUNC(CAST({time_ref} AS DATE), {_GRAIN_SQL[grain]})"
    select = [f"{period} AS period"]
    group = ["period"]
    if breakdown_col:
        select.append(f"{_col(breakdown_col)} AS breakdown")
        group.append("breakdown")
    select.append(f"{agg_sql} AS value")
    where = []
    for f in (filters or []):
        col = f.get("column")
        if not col:
            continue
        where.append(_predicate(_col(col), f.get("op", "eq"), f.get("value")))
    where.append(f"{time_ref} IS NOT NULL")
    # Normalize FROM (raw_table is a fully-qualified id like project.dataset.table)
    from_clause = raw_table if raw_table.strip().startswith("`") else "`" + raw_table + "`"
    sql = ("SELECT " + ", ".join(select) +
           f"\nFROM {from_clause}" +
           ("\nWHERE " + " AND ".join(where) if where else "") +
           "\nGROUP BY " + ", ".join(group) +
           "\nORDER BY period" +
           (", breakdown" if breakdown_col else "") +
           f"\nLIMIT {int(limit)}")
    return sql


def _cond_measure_sql(aggregation: str, measure_ref: str, pred_sql: str) -> str:
    """Conditional aggregate: apply `aggregation` to `measure_ref` only over rows
    matching `pred_sql` (empty pred = aggregate all). Used by ratio metrics so both
    sides of a rate are computed in one pass over the base table."""
    agg = (aggregation or "sum").lower()
    if not pred_sql:
        return _measure_sql(agg, measure_ref)
    if agg == "count":
        return f"COUNTIF({pred_sql})"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT IF({pred_sql}, {measure_ref}, NULL))"
    if agg == "sum":
        return f"SUM(IF({pred_sql}, {measure_ref}, 0))"
    if agg in ("avg", "min", "max"):
        return f"{agg.upper()}(IF({pred_sql}, {measure_ref}, NULL))"
    if agg == "median":
        return f"APPROX_QUANTILES(IF({pred_sql}, {measure_ref}, NULL), 2)[OFFSET(1)]"
    raise ValueError(f"unknown aggregation: {agg}")


def _side_sql(side: dict) -> str:
    """Compile one side (numerator/denominator) of a ratio into a conditional agg.
    side = {measure_col|expression, aggregation, filters:[{column,op,value}]}."""
    ref = (side.get("expression") or "").strip() or _col(side.get("measure_col") or "1")
    preds = []
    for f in (side.get("filters") or []):
        col = f.get("column")
        if col:
            preds.append(_predicate(_col(col), f.get("op", "eq"), f.get("value")))
    pred_sql = " AND ".join(preds)
    return _cond_measure_sql(side.get("aggregation", "count_distinct"), ref, pred_sql)


def compile_ratio_sql(*, raw_table: str, time_col: str, numerator: dict, denominator: dict,
                      grain: str = "month", filters: list = None,
                      breakdown_col: str = "", limit: int = 500) -> str:
    """Compile a RATIO metric (numerator/denominator) into a time-series SQL. Both
    sides are conditional aggregates over the same base table, divided with
    SAFE_DIVIDE so a zero denominator yields NULL rather than an error. This is how
    conversion rates, revenue-per-visitor, ROAS, etc. are expressed."""
    if grain not in _GRAIN_SQL:
        grain = "month"
    time_ref = _col(time_col)
    period = f"DATE_TRUNC(CAST({time_ref} AS DATE), {_GRAIN_SQL[grain]})"
    num = _side_sql(numerator)
    den = _side_sql(denominator)
    select = [f"{period} AS period"]
    group = ["period"]
    if breakdown_col:
        select.append(f"{_col(breakdown_col)} AS breakdown")
        group.append("breakdown")
    select.append(f"SAFE_DIVIDE({num}, {den}) AS value")
    where = []
    for f in (filters or []):
        col = f.get("column")
        if col:
            where.append(_predicate(_col(col), f.get("op", "eq"), f.get("value")))
    where.append(f"{time_ref} IS NOT NULL")
    from_clause = raw_table if raw_table.strip().startswith("`") else "`" + raw_table + "`"
    return ("SELECT " + ", ".join(select) +
            f"\nFROM {from_clause}" +
            ("\nWHERE " + " AND ".join(where) if where else "") +
            "\nGROUP BY " + ", ".join(group) +
            "\nORDER BY period" + (", breakdown" if breakdown_col else "") +
            f"\nLIMIT {int(limit)}")


def format_value(v, fmt: dict):
    """Format a numeric value per the metric's format spec (for display only)."""
    fmt = fmt or {}
    try:
        n = float(v)
    except Exception:
        return v
    typ = fmt.get("type", "number")
    dec = int(fmt.get("decimals", 0))
    if typ == "percent":
        return f"{n*100:.{dec}f}%"
    if typ == "currency":
        cur = fmt.get("currency", "$")
        return f"{cur}{n:,.{dec}f}"
    return f"{n:,.{dec}f}"
