"""
semantic.resolver
─────────────────
Translates user-facing names into raw SQL expressions.

Given a user-facing field key (like 'spend' in 'ad_performance'), the resolver
returns the SQL expression that should be projected. For metrics this includes
the aggregation and any transformations (e.g. micros → dollars).

Example:
    resolver.field_sql('ad_performance', 'spend', model)
    → 'SUM(`cost`) / 1000000'  AS  'spend'

The resolver also knows how to handle:
  • Calculated metrics (defined formulas)
  • Combined dimensions (CASE WHEN rules)
  • Aggregation rules (SUM, AVG, COUNT_DISTINCT, SUM_BOOL)
"""

from __future__ import annotations
from typing import Optional

from .loader import (
    SemanticModel, TableDef, FieldDef,
    CalculatedMetric, CombinedDimension,
)


class ResolverError(Exception):
    pass


# ─── SQL identifier escaping ────────────────────────────────────────
def _quote_ident(name: str) -> str:
    """Wrap an identifier in backticks (BigQuery convention).

    Doesn't try to be clever about already-quoted strings — caller responsibility.
    """
    # Defensive: strip any existing backticks first
    clean = name.replace("`", "")
    return f"`{clean}`"


# ─── Aggregation handling ──────────────────────────────────────────
def _apply_aggregation(column_expr: str, aggregation: Optional[str]) -> str:
    """Wrap a column expression in the appropriate aggregation function."""
    if not aggregation:
        return column_expr

    agg = aggregation.upper()

    if agg == "SUM":
        return f"SUM({column_expr})"
    elif agg == "AVG":
        return f"AVG({column_expr})"
    elif agg == "COUNT":
        return f"COUNT({column_expr})"
    elif agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {column_expr})"
    elif agg == "SUM_BOOL":
        # For boolean columns: count rows where the value is true.
        # Cast to INT64 for BigQuery (true → 1, false → 0).
        return f"SUM(CAST({column_expr} AS INT64))"
    elif agg == "MIN":
        return f"MIN({column_expr})"
    elif agg == "MAX":
        return f"MAX({column_expr})"
    else:
        raise ResolverError(f"Unknown aggregation: {aggregation}")


def _apply_divide_by(column_expr: str, divide_by: Optional[float]) -> str:
    """Apply a divide_by transformation (e.g. micros → dollars)."""
    if not divide_by:
        return column_expr
    return f"({column_expr}) / {divide_by}"


# ─── Field resolution ──────────────────────────────────────────────
def field_sql(table_key: str, field_key: str, model: SemanticModel, alias: str = "") -> str:
    """Resolve a single field (dimension or metric) into a SQL expression.

    For dimensions, returns just the column reference (no aggregation).
    For metrics, returns the full aggregated + transformed expression.
    For calculated metrics, returns the formula with field substitutions.
    For combined dimensions, returns a CASE WHEN expression.

    Does NOT include the AS alias — caller assembles that.

    Args:
        table_key: User-facing table key (e.g. 'ad_performance')
        field_key: User-facing field key (e.g. 'spend', 'roas', 'service_line_group')
        model: The semantic model
        alias: Optional table alias to prefix column references with (e.g. "campaigns").
               When non-empty, regular column refs are emitted as `alias`.`col` to
               disambiguate columns in JOINs.

    Returns:
        SQL expression string

    Raises:
        ResolverError if the field can't be resolved.
    """
    table = model.table(table_key)
    if not table:
        raise ResolverError(f"Unknown table: {table_key}")

    # Try calculated metrics first (they can have the same key as raw fields conceptually)
    for cm in model.calc_metrics_for(table_key):
        if cm.key == field_key:
            return _resolve_calculated(cm, table, model)

    # Try combined dimensions
    for cd in model.combined_dims_for(table_key):
        if cd.key == field_key:
            return _resolve_combined(cd, table, model)

    # Try regular fields on the table
    fdef = table.field_by_key(field_key)
    if fdef:
        return _resolve_field(fdef, alias=alias)

    raise ResolverError(
        f"Field '{field_key}' not found in table '{table_key}'. "
        f"Known fields: {[f.key for f in table.all_fields()]}. "
        f"Known calc metrics: {[c.key for c in model.calc_metrics_for(table_key)]}. "
        f"Known combined dims: {[c.key for c in model.combined_dims_for(table_key)]}."
    )


def _resolve_field(fdef: FieldDef, alias: str = "") -> str:
    """Resolve a regular field (not calc, not combined).

    When `alias` is non-empty, the column reference is prefixed with the alias
    (e.g. `campaigns`.`campaign_id`) so SQL JOINs don't hit "Column name X is
    ambiguous" errors when two joined tables share a column name.
    """
    col = _quote_ident(fdef.column)
    column_expr = f"`{alias}`.{col}" if alias else col

    if fdef.field_kind == "metric":
        # Apply micros transformation INSIDE the aggregation if both are present.
        # SUM(cost) / 1000000 is mathematically equivalent to SUM(cost/1000000),
        # but the latter is much more expensive — BigQuery has to do per-row division.
        # We do the divide_by AFTER aggregation for performance.
        agg_expr = _apply_aggregation(column_expr, fdef.aggregation)
        return _apply_divide_by(agg_expr, fdef.divide_by)
    elif fdef.field_kind == "date":
        # Dates can act as dimensions; no aggregation by default.
        return column_expr
    else:
        # Dimension — just the column reference.
        return column_expr


def _resolve_calculated(cm: CalculatedMetric, table: TableDef, model: SemanticModel) -> str:
    """Resolve a calculated metric by substituting {field_name} placeholders."""
    expr = cm.formula

    # Find all {field_name} placeholders and substitute
    import re
    placeholders = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", expr)

    if not placeholders:
        raise ResolverError(
            f"Calculated metric '{cm.key}' has no field placeholders in formula: {cm.formula}"
        )

    for ph in set(placeholders):
        # Resolve the placeholder field within the same table context.
        # Note: this recurses through field_sql, so calc metrics can reference each other.
        try:
            substitution = field_sql(table.key, ph, model)
        except ResolverError as e:
            raise ResolverError(
                f"In calculated metric '{cm.key}', could not resolve placeholder '{{{ph}}}': {e}"
            )
        expr = expr.replace(f"{{{ph}}}", f"({substitution})")

    return expr


def _resolve_combined(cd: CombinedDimension, table: TableDef, model: SemanticModel) -> str:
    """Resolve a combined dimension into a CASE WHEN expression."""
    # Find the base field
    base_fdef = table.field_by_key(cd.base_field)
    if not base_fdef:
        raise ResolverError(
            f"Combined dimension '{cd.key}' references base_field '{cd.base_field}' "
            f"which doesn't exist in table '{table.key}'."
        )

    base_col = _quote_ident(base_fdef.column)

    # Build CASE WHEN
    parts = ["CASE"]
    default_label = None

    for rule in cd.rules:
        if rule.is_default:
            default_label = rule.label
            continue

        label_escaped = rule.label.replace("'", "''") if rule.label else ""

        # Numeric range rule (bins): range_min <= value < range_max
        if getattr(rule, "range_min", None) is not None or getattr(rule, "range_max", None) is not None:
            conds = []
            if rule.range_min is not None:
                conds.append(f"{base_col} >= {rule.range_min}")
            if rule.range_max is not None:
                conds.append(f"{base_col} < {rule.range_max}")
            cond = " AND ".join(conds) if conds else "TRUE"
            parts.append(f"  WHEN {cond} THEN '{label_escaped}'")
            continue

        # Value-match rule. Escape single quotes in values.
        match_values = [m.replace("'", "''") for m in rule.matches]
        in_list = ", ".join(f"'{v}'" for v in match_values)
        parts.append(f"  WHEN {base_col} IN ({in_list}) THEN '{label_escaped}'")

    if default_label is not None:
        default_escaped = default_label.replace("'", "''")
        parts.append(f"  ELSE '{default_escaped}'")
    else:
        # No explicit default → pass the original value through (enables aliases:
        # relabel some members, keep the rest unchanged).
        parts.append(f"  ELSE {base_col}")

    parts.append("END")
    return "\n".join(parts)


# ─── Table-level resolution ────────────────────────────────────────
def table_sql(table_key: str, model: SemanticModel) -> str:
    """Resolve a table key to its fully-qualified BigQuery reference.

    Returns: `project.dataset.raw_table`
    """
    table = model.table(table_key)
    if not table:
        raise ResolverError(f"Unknown table: {table_key}")

    source = model.sources.get(table.source)
    if not source:
        raise ResolverError(f"Table '{table_key}' references unknown source '{table.source}'.")

    if source.type != "bigquery":
        raise ResolverError(
            f"Source '{source.key}' has type '{source.type}'; only 'bigquery' is supported in Phase 1."
        )

    if not source.project or not source.dataset:
        raise ResolverError(
            f"Source '{source.key}' missing project or dataset."
        )

    return f"`{source.project}`.`{source.dataset}`.`{table.raw_table}`"


def alias_for(field_key: str) -> str:
    """The SQL alias to give a field in SELECT. We use the friendly key directly."""
    # field_key is already a safe identifier (validated to alphanumeric+underscore in loader).
    # But quote it anyway for safety against keywords.
    return _quote_ident(field_key)


def field_format(table_key: str, field_key: str, model: SemanticModel) -> Optional[str]:
    """Get the display format for a field (for the workspace UI).

    Returns 'currency', 'percent', 'number', 'ratio', or None.
    """
    table = model.table(table_key)
    if not table:
        return None

    # Calc metric?
    for cm in model.calc_metrics_for(table_key):
        if cm.key == field_key:
            return cm.format

    # Combined dimension? Always string/category.
    for cd in model.combined_dims_for(table_key):
        if cd.key == field_key:
            return "category"

    # Regular field
    fdef = table.field_by_key(field_key)
    if fdef:
        return fdef.format

    return None
