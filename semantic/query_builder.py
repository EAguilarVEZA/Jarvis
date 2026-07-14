"""
semantic.query_builder
──────────────────────
Builds BigQuery SQL from structured query requests.

The workspace never writes SQL. It sends structured requests like:
    {
      "primary_table": "ad_performance",
      "dimensions": [
        {"table": "campaigns", "field": "service_line"}
      ],
      "metrics": [
        {"table": "ad_performance", "field": "spend"},
        {"table": "ad_performance", "field": "roas"}
      ],
      "filters": [
        {"table": "ad_performance", "field": "date", "op": "between",
         "value": ["2024-01-01", "2024-12-31"]}
      ],
      "limit": 100,
      "order_by": [{"field": "spend", "direction": "desc"}]
    }

…and the query_builder turns that into:

    SELECT
      CASE WHEN ... END AS `service_line_group`,
      (SUM(`cost`))/1000000 AS `spend`,
      (...) AS `roas`
    FROM `oh-data-warehouse`.`gold`.`ad_fact_ads`
    LEFT JOIN `oh-data-warehouse`.`gold`.`ad_dim_campaign` ON ...
    WHERE `date` BETWEEN '2024-01-01' AND '2024-12-31'
    GROUP BY 1
    ORDER BY `spend` DESC
    LIMIT 100
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from .loader import SemanticModel
from .resolver import field_sql, table_sql, alias_for, ResolverError


# ─── Structured query model ─────────────────────────────────────────
@dataclass
class FieldRef:
    """A reference to a field in a table (table + field key)."""
    table: str
    field: str

    @classmethod
    def from_dict(cls, d: dict) -> "FieldRef":
        return cls(table=d["table"], field=d["field"])


@dataclass
class Filter:
    """A WHERE clause condition."""
    table: str
    field: str
    op: str                         # 'eq', 'in', 'between', 'gte', 'lte', 'gt', 'lt', 'like'
    value: Any                      # type depends on op

    @classmethod
    def from_dict(cls, d: dict) -> "Filter":
        return cls(table=d["table"], field=d["field"], op=d["op"], value=d["value"])


@dataclass
class OrderBy:
    field: str                      # the alias of a projected field
    direction: str = "desc"         # 'asc' | 'desc'

    @classmethod
    def from_dict(cls, d: dict) -> "OrderBy":
        return cls(field=d["field"], direction=d.get("direction", "desc"))


@dataclass
class StructuredQuery:
    """The full structured input to query_builder."""
    primary_table: str
    dimensions: list[FieldRef] = field(default_factory=list)
    metrics: list[FieldRef] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: Optional[int] = 1000   # hard cap default

    @classmethod
    def from_dict(cls, d: dict) -> "StructuredQuery":
        return cls(
            primary_table=d["primary_table"],
            dimensions=[FieldRef.from_dict(x) for x in d.get("dimensions", [])],
            metrics=[FieldRef.from_dict(x) for x in d.get("metrics", [])],
            filters=[Filter.from_dict(x) for x in d.get("filters", [])],
            order_by=[OrderBy.from_dict(x) for x in d.get("order_by", [])],
            limit=d.get("limit", 1000),
        )


class QueryBuildError(Exception):
    pass


# ─── Constants ─────────────────────────────────────────────────────
MAX_LIMIT = 100_000   # hard ceiling — no one needs to ask for more in a UI


# ─── SQL safety helpers ────────────────────────────────────────────
def _sql_literal(value: Any, numeric_hint: bool = False) -> str:
    """Render a Python value as a SQL literal. Used for filter values.

    When `numeric_hint` is True we emit numeric strings (e.g. "2024") without
    quotes so BigQuery compares them as INT/FLOAT instead of STRING. This
    handles planner outputs like `calendar_year eq "2024"` against an INT64
    column.

    NOTE: BigQuery parameterized queries are better security-wise. This is
    safe enough for an internal tool because filter values come from the
    semantic layer's known field definitions, not raw user input. For
    Phase 2 we should switch to parameterized queries.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # If we have a numeric hint and the string is a clean number, emit unquoted.
        if numeric_hint:
            s = value.strip()
            if s and (s.lstrip("-").replace(".", "", 1).isdigit()):
                return s
        # Escape single quotes
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, list):
        return "(" + ", ".join(_sql_literal(v, numeric_hint=numeric_hint) for v in value) + ")"
    raise QueryBuildError(f"Cannot render {type(value).__name__} as SQL literal")


def _is_numeric_field(table, field_key: str) -> bool:
    """True if the field is a numeric type (INT64, FLOAT64, NUMERIC, BIGNUMERIC)."""
    try:
        f = table.field_by_key(field_key) if table else None
        if not f:
            return False
        t = (getattr(f, "type", "") or "").upper()
        return t in ("INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DOUBLE")
    except Exception:
        return False


_OP_ALIASES = {
    "equals": "eq", "equal": "eq", "=": "eq", "==": "eq", "is": "eq",
    "not_equals": "neq", "not equal": "neq", "!=": "neq", "<>": "neq",
    "greater_than": "gt", ">": "gt",
    "greater_than_or_equal": "gte", ">=": "gte", "at_least": "gte",
    "less_than": "lt", "<": "lt",
    "less_than_or_equal": "lte", "<=": "lte", "at_most": "lte",
    "contains": "like", "matches": "like", "wildcard": "like",
    "ilike": "like", "icontains": "like",   # case-insensitive variants — collapse to LIKE
    "in_list": "in", "any_of": "in",
    "is_blank": "is_null", "is_empty": "is_null",
    "is_not_null": "not_null", "is_not_blank": "not_null",
    "range": "between", "in_range": "between",
}


def _compare_sql(expr: str, op: str, value: Any, numeric_hint: bool = False) -> str:
    """Build the comparison fragment for a resolved expression."""
    op = (op or "").lower().strip()
    op = _OP_ALIASES.get(op, op)
    # If the planner emitted a "contains"-style op for a like, wrap the value with wildcards
    # unless the user explicitly supplied % markers.
    if op == "eq":
        return f"{expr} = {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "neq":
        return f"{expr} != {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "in":
        if not isinstance(value, list):
            raise QueryBuildError(f"'in' op requires a list value, got {type(value).__name__}")
        return f"{expr} IN {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op in ("not in", "not_in"):
        if not isinstance(value, list):
            raise QueryBuildError(f"'not in' op requires a list value, got {type(value).__name__}")
        return f"{expr} NOT IN {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise QueryBuildError(f"'between' op requires a [low, high] list, got {value}")
        return f"{expr} BETWEEN {_sql_literal(value[0], numeric_hint=numeric_hint)} AND {_sql_literal(value[1], numeric_hint=numeric_hint)}"
    elif op == "gte":
        return f"{expr} >= {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "lte":
        return f"{expr} <= {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "gt":
        return f"{expr} > {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "lt":
        return f"{expr} < {_sql_literal(value, numeric_hint=numeric_hint)}"
    elif op == "like":
        # If the planner forgot wildcards (sent "Pediatrics" instead of "%Pediatrics%"),
        # add them so substring matching actually works.
        sval = str(value or "")
        if sval and "%" not in sval:
            sval = f"%{sval}%"
        return f"{expr} LIKE {_sql_literal(sval)}"
    elif op == "is_null":
        return f"{expr} IS NULL"
    elif op == "not_null":
        return f"{expr} IS NOT NULL"
    else:
        raise QueryBuildError(f"Unknown filter op: {op}")


def _filter_sql(f: Filter, model: SemanticModel, use_aliases: bool = False) -> tuple[str, str]:
    """Build a filter fragment and say whether it belongs in WHERE or HAVING.

    Dimension/date filters → WHERE (raw column).
    Metric / calculated-metric filters → HAVING (post-aggregation expression).
    Returns (fragment, "where"|"having").
    """
    table = model.table(f.table)
    if not table:
        raise QueryBuildError(f"Filter references unknown table: {f.table}")

    fdef = table.field_by_key(f.field)
    is_calc = any(m.key == f.field for m in model.calc_metrics_for(f.table))

    # Forgiving fallback: when the planner asks for "date" or a similarly-named
    # field that this table doesn't have, try to map it to the table's actual
    # date dimension before failing. Covers planner mistakes like "sessions.date"
    # when the real column is "sessions.session_date".
    if fdef is None and not is_calc:
        wanted = (f.field or "").lower()
        is_date_like = wanted in ("date", "event_date", "created_date", "session_date") or "date" in wanted
        if is_date_like and table.dates:
            # Pick the first date field on the table — almost always unambiguous.
            alt = table.dates[0]
            # Replace the filter in-place with the resolved field for downstream use.
            f.field = alt.key
            fdef = alt
        else:
            raise QueryBuildError(f"Filter references unknown field: {f.table}.{f.field}")

    is_metric = is_calc or (fdef is not None and fdef.field_kind == "metric")
    # Prefix with the table alias when JOINs are involved to avoid column ambiguity.
    # Strip backticks because _resolve_field re-wraps.
    filt_alias = (_aliased(f.table, model)[1] or "").replace("`", "") if use_aliases else ""
    expr = field_sql(f.table, f.field, model, alias=filt_alias)
    kind = "having" if is_metric else "where"
    # Coerce numeric values vs numeric columns (e.g. fiscal_year = "2024" → 2024).
    numeric_hint = _is_numeric_field(table, f.field)
    return _compare_sql(expr, f.op.lower(), f.value, numeric_hint=numeric_hint), kind


# ─── Join planning ─────────────────────────────────────────────────
def _plan_joins(primary: str, secondary_tables: list[str], model: SemanticModel) -> list[tuple[str, str, str]]:
    """Determine which tables need to be joined and how.

    Returns: list of (table_key, join_column, join_type) tuples in join order.
    join_type is one of 'inner', 'left', 'right', 'full', 'cross'. Default 'inner'.
    """
    result = []
    for secondary in secondary_tables:
        if secondary == primary:
            continue
        # Find a direct join. Phase 1 supports only direct joins (no multi-hop).
        join = model.join_path(primary, secondary)
        if not join:
            raise QueryBuildError(
                f"No join path between '{primary}' and '{secondary}'. "
                f"Phase 1 supports only directly-joined tables."
            )
        jt = (getattr(join, "join_type", "inner") or "inner").strip().lower()
        if jt not in ("inner", "left", "right", "full", "cross"):
            jt = "inner"
        result.append((secondary, join.on, jt))
    return result


def _aliased(table_key: str, model: SemanticModel) -> tuple[str, str]:
    """Return (fully_qualified, alias) for a table."""
    table = model.table(table_key)
    return (table_sql(table_key, model), f"`{table_key}`")


# ─── Main build function ───────────────────────────────────────────
def build_sql(query: StructuredQuery, model: SemanticModel) -> str:
    """Build a BigQuery SQL string from a structured query.

    Returns the SQL string. Does NOT execute it.
    """
    # ─── Validation ──
    primary = model.table(query.primary_table)
    if not primary:
        raise QueryBuildError(f"Unknown primary table: {query.primary_table}")

    if not query.dimensions and not query.metrics:
        raise QueryBuildError("Query has no dimensions or metrics — nothing to select")

    if query.limit is not None and query.limit > MAX_LIMIT:
        raise QueryBuildError(f"limit {query.limit} exceeds maximum {MAX_LIMIT}")

    # ─── Collect tables referenced by all fields & filters ──
    all_tables = set([query.primary_table])
    for f in query.dimensions + query.metrics:
        all_tables.add(f.table)
    for filt in query.filters:
        all_tables.add(filt.table)

    secondaries = [t for t in all_tables if t != query.primary_table]

    # Plan the joins (validates that joins are possible)
    join_plan = _plan_joins(query.primary_table, secondaries, model)

    # ─── Build SELECT ──
    # When 2+ tables are joined, prefix every column with its table alias so
    # BigQuery doesn't hit "Column name X is ambiguous" for columns shared
    # across tables (e.g. ad_platform appearing on both campaigns and ad_perf).
    use_aliases = len(all_tables) > 1
    def _table_alias(tk: str) -> str:
        # _aliased() returns (fqn, alias) with the alias already wrapped in
        # backticks (e.g. `campaigns`). _resolve_field will re-wrap, so strip
        # the backticks here to avoid `` `\`tk\` ``.col double-wrapping which
        # BigQuery parses as an empty identifier.
        if not use_aliases:
            return ""
        a = _aliased(tk, model)[1] or ""
        return a.replace("`", "")

    select_parts = []
    group_by_indexes = []

    next_idx = 1

    for d in query.dimensions:
        expr = field_sql(d.table, d.field, model, alias=_table_alias(d.table))
        alias = alias_for(d.field)
        select_parts.append(f"  {expr} AS {alias}")
        group_by_indexes.append(next_idx)
        next_idx += 1

    for m in query.metrics:
        expr = field_sql(m.table, m.field, model, alias=_table_alias(m.table))
        alias = alias_for(m.field)
        select_parts.append(f"  {expr} AS {alias}")
        next_idx += 1

    select_sql = "SELECT\n" + ",\n".join(select_parts)

    # ─── Build FROM + JOIN ──
    primary_fq, primary_alias = _aliased(query.primary_table, model)
    from_sql = f"FROM {primary_fq} AS {primary_alias}"

    join_sql_parts = []
    # Map our internal join_type → SQL keyword. CROSS has no ON clause.
    _JT_SQL = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN",
               "full": "FULL OUTER JOIN", "cross": "CROSS JOIN"}
    for sec_key, join_col, join_type in join_plan:
        sec_fq, sec_alias = _aliased(sec_key, model)
        kw = _JT_SQL.get(join_type, "LEFT JOIN")
        if join_type == "cross":
            # CROSS JOIN has no ON clause — Cartesian product
            join_sql_parts.append(f"{kw} {sec_fq} AS {sec_alias}")
        else:
            join_col_quoted = f"`{join_col.replace('`', '')}`"
            join_sql_parts.append(
                f"{kw} {sec_fq} AS {sec_alias}\n"
                f"  ON {primary_alias}.{join_col_quoted} = {sec_alias}.{join_col_quoted}"
            )

    # NOTE: This currently uses bare table aliases (e.g. `campaigns`) but the
    # field_sql output uses bare `column_name` references without table prefix.
    # That's safe in BigQuery as long as column names are unambiguous across
    # joined tables. For Phase 1 our 4 tables don't have name collisions
    # (campaign_id appears in multiple tables but only one is being filtered/
    # projected at a time via field_sql which selects from the right table by
    # column name. BigQuery will resolve to whichever has it — fine for now.
    # Phase 2 will add proper table-prefix-aware resolution.

    # ─── Build WHERE / HAVING ──
    where_sql = ""
    having_sql = ""
    if query.filters:
        where_parts, having_parts = [], []
        for f in query.filters:
            frag, kind = _filter_sql(f, model, use_aliases=use_aliases)
            (having_parts if kind == "having" else where_parts).append(frag)
        if where_parts:
            where_sql = "WHERE " + "\n  AND ".join(where_parts)
        if having_parts:
            having_sql = "HAVING " + "\n  AND ".join(having_parts)

    # ─── Build GROUP BY ──
    group_by_sql = ""
    if group_by_indexes:
        # Group whenever there are dimensions. With metrics → aggregation per group.
        # Without metrics → distinct combinations of the dimension(s) (a value list).
        # Metrics-only (no dimensions) → no GROUP BY (overall aggregate).
        group_by_sql = "GROUP BY " + ", ".join(str(i) for i in group_by_indexes)

    # ─── Build ORDER BY ──
    order_by_sql = ""
    if query.order_by:
        order_parts = []
        for o in query.order_by:
            direction = o.direction.upper()
            if direction not in ("ASC", "DESC"):
                raise QueryBuildError(f"Invalid order direction: {o.direction}")
            order_parts.append(f"{alias_for(o.field)} {direction}")
        order_by_sql = "ORDER BY " + ", ".join(order_parts)

    # ─── Build LIMIT ──
    limit_sql = f"LIMIT {query.limit}" if query.limit else ""

    # ─── Assemble ──
    parts = [select_sql, from_sql]
    parts.extend(join_sql_parts)
    if where_sql:
        parts.append(where_sql)
    if group_by_sql:
        parts.append(group_by_sql)
    if having_sql:
        parts.append(having_sql)
    if order_by_sql:
        parts.append(order_by_sql)
    if limit_sql:
        parts.append(limit_sql)

    return "\n".join(parts)
