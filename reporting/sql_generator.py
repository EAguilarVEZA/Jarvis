"""
SQL generator — turn a complete ReportIntent into a parameterized BigQuery query.

Returns:
    sql: str           — the SELECT statement with @params
    params: dict       — values to bind

Conventions:
  - Always uses query parameters (no string interpolation of user input)
  - Always includes the time column in the WHERE clause
  - Aggregates raw metrics with SUM(); derived metrics use their `expr`
  - Returns dimensions un-aliased, metrics with their column name
  - For "no breakdown" (no dimensions), produces a single-row totals query
  - For dimensions, includes ORDER BY first metric DESC and LIMIT (default 50)
"""

from __future__ import annotations

from typing import Optional

from reporting.builder_types import ReportIntent, FilterClause
from reporting.catalog import WarehouseCatalog, TableMeta


# How many rows to return when grouping by a dimension
DEFAULT_DIMENSION_LIMIT = 50


def _quote_ident(name: str) -> str:
    """Wrap an identifier in backticks for BigQuery."""
    return f"`{name}`"


def _build_metric_select(metric_name: str, table: TableMeta) -> str:
    """Build the SELECT expression for a metric. Either SUM(col) or a derived expr."""
    # Check raw metric columns first
    col = next((c for c in table.metrics() if c.name == metric_name), None)
    if col:
        agg = (col.agg or "SUM").upper()
        return f"{agg}({_quote_ident(col.name)}) AS {_quote_ident(col.name)}"

    # Check derived metrics
    dm = next((d for d in table.derived_metrics if d.name == metric_name), None)
    if dm:
        return f"{dm.expr} AS {_quote_ident(dm.name)}"

    raise ValueError(f"Metric '{metric_name}' not found on table {table.full_name}")


def _build_filter_clause(f: FilterClause, param_prefix: str, params: dict) -> str:
    """Render a single filter into WHERE-clause SQL. Mutates `params` dict."""
    col = _quote_ident(f.column)
    op = f.op.upper()
    if op == "IS TRUE":
        return f"{col} IS TRUE"
    if op == "IS FALSE":
        return f"{col} IS FALSE"
    if op == "IN":
        # value should be a list
        values = f.value if isinstance(f.value, list) else [f.value]
        # BigQuery uses UNNEST([...]) for IN-array
        param_name = f"{param_prefix}_vals"
        params[param_name] = values
        return f"{col} IN UNNEST(@{param_name})"
    if op == "NOT IN":
        values = f.value if isinstance(f.value, list) else [f.value]
        param_name = f"{param_prefix}_vals"
        params[param_name] = values
        return f"{col} NOT IN UNNEST(@{param_name})"
    # Generic =, !=, <, >, <=, >=
    param_name = param_prefix
    params[param_name] = f.value
    return f"{col} {op} @{param_name}"


def generate_sql(intent: ReportIntent, catalog: WarehouseCatalog,
                  limit: Optional[int] = None) -> tuple[str, dict]:
    """
    Generate parameterized BigQuery SQL for the given ReportIntent.

    Returns:
        (sql, params) tuple suitable for `bq_client.query(sql, params)`.

    Raises:
        ValueError if the intent is incomplete or invalid.
    """
    if not intent.table:
        raise ValueError("Cannot generate SQL: intent has no table.")
    if not intent.metrics:
        raise ValueError("Cannot generate SQL: intent has no metrics.")
    if not intent.time_window.is_specified():
        raise ValueError("Cannot generate SQL: intent has no time window.")

    table = catalog.get(intent.table)
    if not table:
        raise ValueError(f"Table '{intent.table}' not in catalog.")

    time_col = table.time_column
    if not time_col:
        raise ValueError(f"Table {intent.table} has no time_column defined.")

    params: dict = {}

    # ─── SELECT clause ──────────────────────────────────────
    select_parts = []

    # Dimensions go in SELECT and GROUP BY
    group_by_parts = []
    for dim in intent.dimensions:
        if dim == "__none__":
            continue  # totals-only
        if dim.startswith("__time__:"):
            # synthetic "daily trend" dimension
            actual_col = dim.split(":", 1)[1]
            select_parts.append(f"{_quote_ident(actual_col)} AS {_quote_ident(actual_col)}")
            group_by_parts.append(_quote_ident(actual_col))
        else:
            select_parts.append(_quote_ident(dim))
            group_by_parts.append(_quote_ident(dim))

    # Metrics
    for metric in intent.metrics:
        if metric == "__all_defaults__":
            # Expand to default metrics
            for dm in table.default_metrics:
                select_parts.append(_build_metric_select(dm, table))
        else:
            select_parts.append(_build_metric_select(metric, table))

    select_clause = ",\n    ".join(select_parts)

    # ─── FROM ───────────────────────────────────────────────
    from_clause = _quote_ident(intent.table.replace(".", "`.`"))
    # BigQuery: use project.dataset.table → `project.dataset.table`
    # Most reports are scoped to a single project; let the bq_client default to it.

    # ─── WHERE: time window + filters ─────────────────────
    where_parts = []
    params["start_date"] = intent.time_window.start
    params["end_date"] = intent.time_window.end
    where_parts.append(f"{_quote_ident(time_col)} BETWEEN @start_date AND @end_date")

    for i, f in enumerate(intent.filters):
        where_parts.append(_build_filter_clause(f, f"flt_{i}", params))

    where_clause = "\n  AND ".join(where_parts)

    # ─── GROUP BY ───────────────────────────────────────
    group_by_clause = ""
    if group_by_parts:
        group_by_clause = "GROUP BY " + ", ".join(group_by_parts)

    # ─── ORDER BY ───────────────────────────────────────
    order_by_clause = ""
    if group_by_parts and intent.metrics:
        # Order by the first metric DESC
        first_metric = intent.metrics[0]
        if first_metric == "__all_defaults__":
            first_metric = table.default_metrics[0] if table.default_metrics else None
        if first_metric:
            order_by_clause = f"ORDER BY {_quote_ident(first_metric)} DESC"

    # ─── LIMIT ──────────────────────────────────────────
    limit_clause = ""
    if group_by_parts:  # only LIMIT for breakdowns; totals-only returns 1 row
        limit_clause = f"LIMIT {limit or DEFAULT_DIMENSION_LIMIT}"

    # ─── Assemble ────────────────────────────────────────
    parts = [
        "SELECT",
        f"    {select_clause}",
        f"FROM {from_clause}",
        f"WHERE {where_clause}",
    ]
    if group_by_clause:
        parts.append(group_by_clause)
    if order_by_clause:
        parts.append(order_by_clause)
    if limit_clause:
        parts.append(limit_clause)

    sql = "\n".join(parts)
    return sql, params
