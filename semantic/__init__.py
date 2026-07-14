"""
semantic
────────
Orlando Health Spatial Workspace — Semantic Layer.

The semantic layer is the single source of truth for what the workspace shows.
It maps user-facing names ("Campaigns", "Ad Spend") to raw BigQuery columns
("ad_dim_campaign", "cost / 1000000"). It defines calculated metrics, combined
dimensions, joins between tables, and the curated subset of fields surfaced
in the UI.

Public API
──────────

    from semantic import load_model, build_sql, run_query

    model = load_model()                          # parsed semantic.yaml
    sql = build_sql(structured_query, model)      # structured → SQL string
    result = run_query(sql)                       # execute, get rows

Curation surface (for Curation Mode UI):

    from semantic import (
        list_dataset_tables, introspect_table,    # discover the raw warehouse
        save_model,                               # persist edits
    )

Module map:
  loader.py             — parse semantic.yaml into typed dataclasses
  resolver.py           — translate friendly names to SQL expressions
  query_builder.py      — build full SELECT statements from structured queries
  executor.py           — run SQL against BigQuery, return rows
  schema_introspect.py  — fetch live BigQuery schemas
  writer.py             — persist semantic.yaml edits safely
"""

from .loader import (
    load as load_model,
    invalidate_cache,
    SemanticModel,
    TableDef,
    FieldDef,
    CalculatedMetric,
    CombinedDimension,
    CombinedDimensionRule,
    BusinessRule,
    JoinDef,
    ClusterDef,
    SourceDef,
    SemanticLoadError,
)

from .resolver import (
    field_sql,
    table_sql,
    alias_for,
    field_format,
    ResolverError,
)

from .query_builder import (
    build_sql as _bq_build_sql,
    StructuredQuery,
    FieldRef,
    Filter,
    OrderBy,
    QueryBuildError,
)


def build_sql(query, model):
    """Compile a StructuredQuery to SQL via the active engine (see jarvis_engine).

    Default ("bigquery"): Jarvis's query_builder assembles the SQL.
    "ktx": compile through ktx's approved measures (Seam A). If the query uses
    something ktx can't express yet (combined dimensions, metric filters), it
    transparently falls back to the BigQuery builder for that query. Any ktx
    fault also falls back, so callers are never broken.
    """
    try:
        from jarvis_engine import use_ktx
        _use_ktx = use_ktx()
    except Exception:
        _use_ktx = False

    if _use_ktx:
        try:
            from ktx_client import compile_structured_query, NotMappable
            try:
                return compile_structured_query(query, model)
            except NotMappable as _e:
                import logging
                logging.getLogger("semantic").info("ktx compile fallback (%s); using BigQuery builder", _e)
        except Exception as _e:  # noqa: BLE001
            import logging
            logging.getLogger("semantic").warning("ktx compile error (%s); using BigQuery builder", _e)

    return _bq_build_sql(query, model)

from .executor import (
    run_query as _bq_run_query,
    estimate_cost,
    format_bytes,
    QueryResult,
    ExecutorError,
    ExecutorConfigError,
    QueryExecutionError,
)


def run_query(sql, **kwargs):
    """Execute SQL via the active engine (see jarvis_engine).

    Default ("bigquery"): runs directly on BigQuery via semantic.executor.
    "ktx": routes execution through the ktx context layer (parser-validated,
    read-only) and returns the identical QueryResult shape.

    If ktx is selected but unavailable/erroring, this falls back to the
    BigQuery path automatically so callers are never broken by a ktx hiccup.
    Imports are lazy to avoid any import-time coupling/cycles.
    """
    try:
        from jarvis_engine import use_ktx
        _use_ktx = use_ktx()
    except Exception:
        _use_ktx = False

    if _use_ktx:
        try:
            from ktx_client import execute_sql as _ktx_execute_sql
            return _ktx_execute_sql(sql, **kwargs)
        except Exception as _e:  # noqa: BLE001 — resilience: never break callers on a ktx fault
            import logging
            logging.getLogger("semantic").warning(
                "ktx execution failed (%s); falling back to BigQuery executor", _e
            )

    return _bq_run_query(sql, **kwargs)

from .schema_introspect import (
    introspect_table,
    introspect_curated_table,
    list_dataset_tables,
    RawTableSchema,
    RawColumn,
    clear_cache as clear_schema_cache,
)

from .writer import (
    save as save_model,
    save_raw_yaml,
    publish as publish_model,
    publish_status,
    WriterError,
)

__all__ = [
    # Loader
    "load_model", "invalidate_cache",
    "SemanticModel", "TableDef", "FieldDef",
    "CalculatedMetric", "CombinedDimension", "CombinedDimensionRule", "BusinessRule", "JoinDef",
    "ClusterDef", "SourceDef", "SemanticLoadError",
    # Resolver
    "field_sql", "table_sql", "alias_for", "field_format", "ResolverError",
    # Query builder
    "build_sql", "StructuredQuery", "FieldRef", "Filter", "OrderBy", "QueryBuildError",
    # Executor
    "run_query", "estimate_cost", "format_bytes",
    "QueryResult", "ExecutorError", "ExecutorConfigError", "QueryExecutionError",
    # Schema introspection
    "introspect_table", "introspect_curated_table", "list_dataset_tables",
    "RawTableSchema", "RawColumn", "clear_schema_cache",
    # Writer
    "save_model", "save_raw_yaml", "publish_model", "publish_status", "WriterError",
]
