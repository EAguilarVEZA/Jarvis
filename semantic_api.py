"""
semantic_api
────────────
HTTP layer over the semantic engine (Stage 2 of Phase A).

Exposes the engine via REST so the spatial workspace (and any future client)
can query the curated semantic layer without importing Python. Mounted into
the main FastAPI app via `app.include_router(router)` in server.py.

Route map (prefix: /api/semantic):
  GET    /tables                         — list curated tables
  GET    /tables/{key}                   — full schema for one curated table
  POST   /query                          — execute a structured query
  GET    /calculated                     — list calculated metrics
  GET    /combined                       — list combined dimensions
  GET    /raw_tables                     — list raw warehouse tables (for Curation Mode)
  GET    /raw_schema                     — live BQ schema for one raw table
  POST   /save                           — persist YAML edits (Form A: full replace)

Errors:
  • SemanticLoadError, ResolverError, QueryBuildError      → HTTP 400
  • ExecutorConfigError (missing creds / BQ lib)           → HTTP 503
  • QueryExecutionError, ExecutorError, WriterError, etc.  → HTTP 500
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
from functools import partial
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from semantic import (
    load_model, save_model,
    SemanticLoadError,
    build_sql, run_query,
    StructuredQuery,
    QueryBuildError,
    ResolverError, field_sql, table_sql,
    ExecutorConfigError, ExecutorError, QueryExecutionError,
    introspect_table, introspect_curated_table, list_dataset_tables,
    save_raw_yaml, WriterError,
    publish_model, publish_status,
)
from semantic.loader import (
    SEMANTIC_YAML, FieldDef, TableDef, SourceDef, JoinDef,
    CalculatedMetric, CombinedDimension, CombinedDimensionRule, BusinessRule, Hierarchy,
)

def _loadm(draft: bool = False):
    """Centralized loader. draft=True reads the working layer (curation workspace);
    default reads the published layer (Dashboards/Analysis)."""
    return load_model(use_draft=draft)


# Valid filter operators, shared by business rules and query filters.
VALID_OPS = {"eq", "neq", "in", "between", "gte", "lte", "gt", "lt", "like", "is_null", "not_null"}
NULL_OPS = {"is_null", "not_null"}
LIST_OPS = {"in", "between"}


log = logging.getLogger("semantic_api")

router = APIRouter(prefix="/api/semantic", tags=["semantic"])


# ─── Serialization helpers ───────────────────────────────────────────
def _field_to_dict(f: FieldDef) -> dict:
    """One field (dimension, metric, or date) as JSON."""
    out = {
        "key": f.key,
        "column": f.column,
        "display_name": f.display_name,
        "field_kind": f.field_kind,
        "type": f.type,
        "description": f.description,
    }
    # Metric-only fields, only emit when present
    if f.aggregation is not None:
        out["aggregation"] = f.aggregation
    if f.format is not None:
        out["format"] = f.format
    if f.divide_by is not None:
        out["divide_by"] = f.divide_by
    return out


def _table_summary(t: TableDef) -> dict:
    """Lightweight table summary for the /tables list response."""
    return {
        "key": t.key,
        "display_name": t.display_name,
        "description": t.description,
        "cluster": t.cluster,
        "kind": t.kind,
        "raw_table": t.raw_table,
        "source": t.source,
        "field_count": {
            "dimensions": len(t.dimensions),
            "metrics": len(t.metrics),
            "dates": len(t.dates),
        },
    }


def _calc_metric_to_dict(cm: CalculatedMetric) -> dict:
    return {
        "key": cm.key,
        "display_name": cm.display_name,
        "description": cm.description,
        "formula": cm.formula,
        "format": cm.format,
        "available_in": list(cm.available_in),
        "prefix": getattr(cm, "prefix", ""),
        "suffix": getattr(cm, "suffix", ""),
    }


def _combined_dim_to_dict(cd: CombinedDimension) -> dict:
    return {
        "key": cd.key,
        "display_name": cd.display_name,
        "description": cd.description,
        "base_field": cd.base_field,
        "available_in": list(cd.available_in),
        "rule_count": len(cd.rules),
        "rules": [
            {
                "matches": list(r.matches),
                "label": r.label,
                "is_default": r.is_default,
            }
            for r in cd.rules
        ],
    }


def _business_rule_to_dict(r: BusinessRule) -> dict:
    return {
        "key": r.key,
        "display_name": r.display_name,
        "description": r.description,
        "table": r.table,
        "field": r.field,
        "op": r.op,
        "value": r.value,
    }


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    """Consistent error envelope."""
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


# ─── Write-path helpers ──────────────────────────────────────────────
async def _persist(model) -> dict:
    """Save the model to disk (off the event loop) and return the save envelope."""
    info = await asyncio.get_running_loop().run_in_executor(None, save_model, model)
    return {
        "ok": True,
        "path": info["path"],
        "bytes_written": info["bytes_written"],
        "backup_path": info["backup_path"],
    }


def _validate_field_ref(model, table_key: str, field_key: str) -> Optional[str]:
    """Return an error string if table/field don't exist, else None."""
    t = model.table(table_key)
    if not t:
        return f"Unknown table '{table_key}'"
    if not t.field_by_key(field_key):
        return f"Unknown field '{field_key}' in table '{table_key}'"
    return None


def _validate_op_value(op: str, value) -> Optional[str]:
    """Return an error string if op/value combination is invalid, else None."""
    if op not in VALID_OPS:
        return f"Invalid op '{op}'. Valid: {', '.join(sorted(VALID_OPS))}"
    if op in NULL_OPS:
        return None  # value ignored
    if op in LIST_OPS:
        if not isinstance(value, list) or (op == "between" and len(value) != 2):
            need = "a [low, high] list" if op == "between" else "a list"
            return f"Op '{op}' requires {need}"
    else:
        if isinstance(value, (list, dict)):
            return f"Op '{op}' requires a scalar value"
    return None


def _default_source(model) -> tuple[Optional[str], Optional[str]]:
    """Return (project, dataset) of the first BigQuery source in the model, or (None, None)."""
    for s in model.sources.values():
        if s.type == "bigquery" and s.project and s.dataset:
            return (s.project, s.dataset)
    return (None, None)


# ─── 1. GET /api/semantic/tables ─────────────────────────────────────
@router.get("/tables")
async def list_tables(draft: bool = False):
    """List all curated tables with lightweight summary info.
    Pass ?draft=true to read the working layer (curation workspace)."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    tables = [_table_summary(t) for t in model.tables.values()]
    return {"tables": tables, "count": len(tables)}


# ─── 2. GET /api/semantic/tables/{table_key} ─────────────────────────
@router.get("/tables/{table_key}")
async def get_table(table_key: str, draft: bool = False):
    """Full schema for one curated table — fields, calc metrics, combined dims, joins."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    table = model.table(table_key)
    if not table:
        return _err(404, "table not found", f"No curated table with key '{table_key}'")

    # Joins this table participates in
    joins = [
        {"left": j.left, "right": j.right, "on": j.on}
        for j in model.joins
        if j.left == table_key or j.right == table_key
    ]

    return {
        "key": table.key,
        "display_name": table.display_name,
        "description": table.description,
        "cluster": table.cluster,
        "kind": table.kind,
        "source": table.source,
        "raw_table": table.raw_table,
        "primary_key": table.primary_key,
        "join_keys": list(table.join_keys),
        "dimensions": [_field_to_dict(f) for f in table.dimensions],
        "metrics": [_field_to_dict(f) for f in table.metrics],
        "dates": [_field_to_dict(f) for f in table.dates],
        "calculated_metrics": [_calc_metric_to_dict(m) for m in model.calc_metrics_for(table_key)],
        "combined_dimensions": [_combined_dim_to_dict(d) for d in model.combined_dims_for(table_key)],
        "joins": joins,
    }


# ─── 3. POST /api/semantic/query ─────────────────────────────────────
class QueryRequest(BaseModel):
    """Mirror of semantic.StructuredQuery.from_dict input shape."""
    primary_table: str
    dimensions: list[dict] = Field(default_factory=list)
    metrics: list[dict] = Field(default_factory=list)
    filters: list[dict] = Field(default_factory=list)
    order_by: list[dict] = Field(default_factory=list)
    limit: Optional[int] = 1000


@router.post("/query")
async def post_query(
    body: QueryRequest,
    include_sql: bool = Query(True, description="Include generated SQL in response (debug)"),
):
    """Execute a structured query against the semantic layer and return rows."""
    # Build phase
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    try:
        sq = StructuredQuery.from_dict(body.model_dump())
    except KeyError as e:
        return _err(400, "malformed query", f"Missing required field: {e}")
    except Exception as e:
        return _err(400, "malformed query", str(e))

    try:
        sql = build_sql(sq, model)
    except (QueryBuildError, ResolverError) as e:
        return _err(400, "query build failed", str(e))

    # Execute phase (off the event loop — BQ blocks)
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except QueryExecutionError as e:
        return _err(500, "query execution failed", str(e))
    except ExecutorError as e:
        return _err(500, "executor error", str(e))

    payload = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "bytes_processed": result.bytes_processed,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
    }
    if include_sql:
        payload["sql"] = sql
    return payload


class RawSqlRequest(BaseModel):
    sql: str


@router.post("/run_sql")
async def post_run_sql(body: RawSqlRequest, request: Request):
    """Execute a user-edited SQL statement against the warehouse (read-only).

    Power-user escape hatch from the Query Panel's SQL tab. Guarded to a single
    read-only SELECT / WITH statement — no DML/DDL and no multiple statements —
    so the editor can't be used to mutate the warehouse.
    """
    sql = (body.sql or "").strip()
    while sql.endswith(";"):
        sql = sql[:-1].strip()
    if not sql:
        return _err(400, "empty sql", "Provide a SQL statement to run.")
    if ";" in sql:
        return _err(400, "single statement only", "Run one statement at a time (no semicolons).")
    low = sql.lstrip("(").lstrip().lower()
    if not (low.startswith("select") or low.startswith("with")):
        return _err(400, "read-only only", "Only SELECT / WITH queries can be run from this editor.")
    if re.search(r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|call)\b", sql, re.I):
        return _err(400, "read-only only", "This editor runs read-only queries only.")
    # Governance — resolve {{user.x}} row-security tokens + enforce the SQL policy.
    # Fail-closed: a query referencing security properties the user lacks is refused.
    from sql_security import enforce as _sec_enforce, SecurityError as _SecErr
    from sql_reviews import record as _rec_review
    email = (request.headers.get("X-Jarvis-User") or "").strip() or None
    _tmpl = sql
    try:
        sql = _sec_enforce(sql, email)
    except _SecErr as e:
        _rec_review(_tmpl, user=email, error=str(e), blocked=True)
        return _err(403, "blocked by security policy", str(e))
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except QueryExecutionError as e:
        _rec_review(sql, user=email, error=str(e))
        return _err(400, "query execution failed", str(e))
    except ExecutorError as e:
        _rec_review(sql, user=email, error=str(e))
        return _err(500, "executor error", str(e))
    _rec_review(sql, user=email, rows=result.row_count)
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "bytes_processed": result.bytes_processed,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
        "sql": sql,
    }


# ─── 4. GET /api/semantic/calculated ─────────────────────────────────
@router.get("/calculated")
async def list_calculated_metrics(draft: bool = False):
    """List all calculated metrics defined in semantic.yaml."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    metrics = [_calc_metric_to_dict(m) for m in model.calculated_metrics.values()]
    return {"calculated_metrics": metrics, "count": len(metrics)}


# ─── 5. GET /api/semantic/combined ───────────────────────────────────
@router.get("/combined")
async def list_combined_dimensions(draft: bool = False):
    """List all combined dimensions defined in semantic.yaml."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    dims = [_combined_dim_to_dict(d) for d in model.combined_dimensions.values()]
    return {"combined_dimensions": dims, "count": len(dims)}


# ─── 6. GET /api/semantic/raw_tables ─────────────────────────────────
def _raw_tables_metadata_sql(project: str, dataset: str) -> str:
    """Single query against __TABLES__ + INFORMATION_SCHEMA.COLUMNS.

    Pulls row_count, size_bytes (from legacy __TABLES__) and column_count
    (from INFORMATION_SCHEMA.COLUMNS) for every table in the dataset in
    one shot. Much faster than 24 sequential get_table() calls.
    """
    return f"""
SELECT
  t.table_id AS table_name,
  t.row_count,
  t.size_bytes,
  IFNULL(c.column_count, 0) AS column_count
FROM `{project}.{dataset}.__TABLES__` t
LEFT JOIN (
  SELECT table_name, COUNT(*) AS column_count
  FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
  GROUP BY table_name
) c
  ON c.table_name = t.table_id
ORDER BY t.table_id
""".strip()


@router.get("/raw_tables")
async def list_raw_tables(
    project: Optional[str] = Query(None, description="BQ project (defaults to first source)"),
    dataset: Optional[str] = Query(None, description="BQ dataset (defaults to first source)"),
):
    """List tables in a BigQuery dataset, marking which are already curated."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    # Default to first BQ source when project/dataset omitted
    if not project or not dataset:
        dp, dd = _default_source(model)
        project = project or dp
        dataset = dataset or dd

    if not project or not dataset:
        return _err(400, "no source", "project and dataset are required (no default BQ source in semantic.yaml)")

    # Build reverse map: raw_table → curated key, for tables in this dataset's source
    raw_to_curated: dict[str, str] = {}
    for t in model.tables.values():
        src = model.sources.get(t.source)
        if src and src.project == project and src.dataset == dataset:
            raw_to_curated[t.raw_table] = t.key

    # Pull metadata for every table in the dataset in one query
    sql = _raw_tables_metadata_sql(project, dataset)
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except QueryExecutionError as e:
        return _err(500, "metadata query failed", str(e))
    except ExecutorError as e:
        return _err(500, "executor error", str(e))

    tables = []
    for row in result.rows:
        # Columns: table_name, row_count, size_bytes, column_count
        name, row_count, size_bytes, column_count = row
        curated_key = raw_to_curated.get(name)
        tables.append({
            "name": name,
            "curated": curated_key is not None,
            "curated_as": curated_key,
            "column_count": int(column_count) if column_count is not None else 0,
            "row_count": int(row_count) if row_count is not None else 0,
            "size_bytes": int(size_bytes) if size_bytes is not None else 0,
        })

    return {
        "project": project,
        "dataset": dataset,
        "count": len(tables),
        "tables": tables,
    }


# ─── 7. GET /api/semantic/raw_schema ─────────────────────────────────
@router.get("/raw_schema")
async def get_raw_schema(
    project: Optional[str] = Query(None),
    dataset: Optional[str] = Query(None),
    table: str = Query(..., description="Raw table name (e.g. ad_dim_campaign)"),
):
    """Live column schema for one raw BigQuery table."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    if not project or not dataset:
        dp, dd = _default_source(model)
        project = project or dp
        dataset = dataset or dd

    if not project or not dataset:
        return _err(400, "no source", "project and dataset are required (no default BQ source in semantic.yaml)")

    try:
        schema = await asyncio.get_running_loop().run_in_executor(
            None, introspect_table, project, dataset, table
        )
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "introspect failed", str(e))

    return {
        "project": schema.project,
        "dataset": schema.dataset,
        "table": schema.table,
        "fqn": schema.fqn,
        "row_count": schema.num_rows,
        "size_bytes": schema.size_bytes,
        "columns": [c.to_dict() for c in schema.columns],
    }


# ─── 7b. GET /api/semantic/raw_rows ───────────────────────────────
@router.get("/raw_rows")
async def get_raw_rows(
    project: Optional[str] = Query(None),
    dataset: Optional[str] = Query(None),
    table: str = Query(..., description="Raw table name"),
    limit: int = Query(500, ge=1, le=10000),
):
    """Read raw rows from a BigQuery table (no curation required). Used by the
    hierarchy importer (org_hierarchy.dim_entity) and other one-shot loaders.

    Returns {columns, rows, row_count}. No filters — pulls top N rows ordered
    by the first column for stable iteration.
    """
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    if not project or not dataset:
        dp, dd = _default_source(model)
        project = project or dp
        dataset = dataset or dd
    if not project or not dataset:
        return _err(400, "no source", "project and dataset are required.")

    # Build a safe parametric SQL — backtick-quote each identifier so dots in
    # the project don't break the parser.
    sql = f"SELECT * FROM `{project}`.`{dataset}`.`{table}` LIMIT {int(limit)}"
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "query failed", str(e))
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "fqn": f"{project}.{dataset}.{table}",
    }


# ─── 7c. POST /api/semantic/detail — drill to underlying record-grain rows ──
class DetailRequest(BaseModel):
    primary_table: str
    filters: list = []
    limit: int = 200


@router.post("/detail")
async def post_detail(body: DetailRequest, request: Request):
    """Drill-to-detail: return raw, record-grain rows from the primary table,
    filtered to a clicked row's dimension values. `SELECT *` with no aggregation
    so the user sees the individual underlying records behind an aggregate cell.

    Only filters that live on the primary table are applied (no joins), and the
    same security enforcement + review logging as /run_sql runs on the SQL.
    """
    from semantic.query_builder import _filter_sql, _aliased, Filter as _Filter

    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    prim = model.table(body.primary_table)
    if not prim:
        return _err(400, "unknown table", f"Unknown primary table: {body.primary_table}")
    try:
        fqn, alias = _aliased(body.primary_table, model)
    except Exception as e:
        return _err(400, "resolve failed", str(e))

    # Build WHERE from dimension filters that belong to the primary table.
    where_parts = []
    for fd in (body.filters or []):
        if (fd.get("table") or body.primary_table) != body.primary_table:
            continue  # skip joined-table filters — detail is single-table
        try:
            filt = _Filter.from_dict({**fd, "table": body.primary_table})
            frag, kind = _filter_sql(filt, model, use_aliases=False)
            if kind == "where":
                where_parts.append(frag)
        except Exception:
            continue

    limit = max(1, min(int(body.limit or 200), 5000))
    where_sql = ("WHERE " + "\n  AND ".join(where_parts)) if where_parts else ""
    sql = f"SELECT * FROM {fqn} AS {alias}\n{where_sql}\nLIMIT {limit}"

    from sql_security import enforce as _sec_enforce, SecurityError as _SecErr
    from sql_reviews import record as _rec_review
    email = (request.headers.get("X-Jarvis-User") or "").strip() or None
    try:
        sql = _sec_enforce(sql, email)
    except _SecErr as e:
        _rec_review(sql, user=email, error=str(e), blocked=True)
        return _err(403, "blocked by security policy", str(e))
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        _rec_review(sql, user=email, error=str(e))
        return _err(400, "detail query failed", str(e))
    _rec_review(sql, user=email, rows=result.row_count)
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "sql": sql,
    }


# ─── 8. POST /api/semantic/save ─────────────────────────────────────
class SaveRequest(BaseModel):
    """Form A only for now: replace the entire YAML.

    TODO: Form B (structured patch with deep merge) — deferred to Phase B,
    when the Curation Mode UI actually starts authoring incremental edits.
    """
    raw_yaml: Optional[str] = Field(None, description="Full semantic.yaml content (Form A)")
    patch: Optional[dict] = Field(None, description="Structured patch (Form B, NOT IMPLEMENTED)")


@router.post("/save")
async def save_semantic_yaml(body: SaveRequest):
    """Persist YAML edits. Currently supports Form A (full replace) only."""
    if body.patch is not None and body.raw_yaml is None:
        return _err(501, "form B not implemented", "Structured patch deferred to Phase B; use raw_yaml for now.")

    if not body.raw_yaml:
        return _err(400, "raw_yaml required", "Provide 'raw_yaml' with full semantic.yaml content.")

    try:
        info = await asyncio.get_running_loop().run_in_executor(
            None, save_raw_yaml, body.raw_yaml
        )
    except WriterError as e:
        return _err(500, "save failed", str(e))
    except Exception as e:
        return _err(500, "unexpected save failure", str(e))

    return {
        "ok": True,
        "path": info["path"],
        "bytes_written": info["bytes_written"],
        "backup_path": info["backup_path"],
    }


# ─── 8a. Publish: promote draft → live ───────────────────────────────
@router.get("/publish_status")
async def get_publish_status():
    """Tell the UI whether a draft exists, whether it differs from live, and when last published."""
    try:
        return {"ok": True, **publish_status()}
    except Exception as e:
        return _err(500, "status failed", str(e))


@router.post("/publish")
async def publish_endpoint():
    """Promote the draft semantic.yaml to the published layer. Dashboards now see the new model."""
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, publish_model)
    except WriterError as e:
        return _err(500, "publish failed", str(e))
    except Exception as e:
        return _err(500, "unexpected publish failure", str(e))
    if not info.get("ok"):
        return _err(400, "nothing to publish", info.get("error", "no draft to publish"))
    return info


@router.post("/sync_ktx")
async def sync_ktx_endpoint():
    """Regenerate ktx's authored measures + field map from the published semantic.yaml,
    then reindex — so the canonical ktx engine uses your latest curated metrics/filters.
    Run this after publishing semantic-layer changes."""
    try:
        from ktx_sync import sync
        result = await asyncio.get_running_loop().run_in_executor(None, sync)
        return {"ok": True, **result}
    except Exception as e:  # noqa: BLE001
        return _err(500, "ktx sync failed", str(e))


# ═══════════════════════════════════════════════════════════════════
#  PHASE B — Write-path mutation endpoints
#  Each loads a fresh model, mutates the dataclass, and persists via
#  writer.save() (atomic write + timestamped backup). Cache is
#  invalidated by the writer so the next read reflects the change.
# ═══════════════════════════════════════════════════════════════════

def _load_fresh(draft: bool = True):
    """Load a fresh model for mutation. Defaults to the DRAFT layer (mutations
    stack on top of the working copy until publish promotes them to live)."""
    return load_model(force=True, use_draft=draft)


# ─── 9. GET /api/semantic/rules — list business rules ────────────────
@router.get("/rules")
async def list_business_rules(draft: bool = False):
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    rules = [_business_rule_to_dict(r) for r in model.business_rules.values()]
    return {"business_rules": rules, "count": len(rules)}


# ─── 10. POST /api/semantic/tables/{key}/rename ──────────────────────
class RenameTableRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None


@router.post("/tables/{table_key}/rename")
async def rename_table(table_key: str, body: RenameTableRequest):
    """Rename a curated table's display_name and/or description."""
    if body.display_name is None and body.description is None:
        return _err(400, "nothing to change", "Provide display_name and/or description.")
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    t = model.table(table_key)
    if not t:
        return _err(404, "table not found", f"No curated table '{table_key}'")
    if body.display_name is not None:
        t.display_name = body.display_name
    if body.description is not None:
        t.description = body.description

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["table"] = {"key": t.key, "display_name": t.display_name, "description": t.description}
    return out


# ─── 11. POST /api/semantic/tables/{key}/fields/{field}/rename ───────
class RenameFieldRequest(BaseModel):
    display_name: str
    description: Optional[str] = None


@router.post("/tables/{table_key}/fields/{field_key}/rename")
async def rename_field(table_key: str, field_key: str, body: RenameFieldRequest):
    """Rename a curated field's display_name (and optionally description)."""
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    t = model.table(table_key)
    if not t:
        return _err(404, "table not found", f"No curated table '{table_key}'")
    fdef = t.field_by_key(field_key)
    if not fdef:
        return _err(404, "field not found", f"No field '{field_key}' in table '{table_key}'")

    fdef.display_name = body.display_name
    if body.description is not None:
        fdef.description = body.description

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["field"] = {"key": fdef.key, "display_name": fdef.display_name}
    return out


# ─── 11b. Add a curated field to a table ────────────────────────────
class AddFieldRequest(BaseModel):
    key: str                          # field key (e.g. "patient_service")
    column: str                       # raw BQ column name
    display_name: str
    field_kind: str                   # 'dimension' | 'metric' | 'date'
    type: Optional[str] = None        # 'STRING' / 'INTEGER' / 'DATE' / etc.
    description: Optional[str] = None
    aggregation: Optional[str] = None # metric-only
    format: Optional[str] = None      # metric-only


@router.post("/tables/{table_key}/fields")
async def add_field_to_table(table_key: str, body: AddFieldRequest):
    """Append a curated field (dimension, date, or metric) to an existing table.

    Used by the dashboard widget builder's "+ New dimension" inline action so
    users can promote a raw column to a curated field without leaving the
    builder. Mutates the draft layer; publish to promote.
    """
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    t = model.table(table_key)
    if not t:
        return _err(404, "table not found", f"No curated table '{table_key}'")
    if t.field_by_key(body.key):
        return _err(409, "field exists", f"'{body.key}' already exists on '{table_key}'")
    kind = (body.field_kind or "dimension").lower()
    if kind not in ("dimension", "metric", "date"):
        return _err(400, "bad field_kind", "field_kind must be 'dimension', 'metric', or 'date'.")

    # Build FieldDef
    from semantic.loader import FieldDef
    fdef = FieldDef(
        key=body.key,
        column=body.column or body.key,
        display_name=body.display_name,
        field_kind=kind,
        type=body.type,
        description=body.description,
        aggregation=(body.aggregation if kind == "metric" else None),
        format=(body.format if kind == "metric" else None),
    )
    if kind == "metric":
        t.metrics.append(fdef)
    elif kind == "date":
        t.dates.append(fdef)
    else:
        t.dimensions.append(fdef)

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["field"] = {"key": fdef.key, "column": fdef.column, "display_name": fdef.display_name, "kind": kind}
    return out


# ─── 12. Calculated metrics — create / update / delete ───────────────
class CalcMetricRequest(BaseModel):
    key: Optional[str] = None              # required for create, ignored for update
    display_name: str
    description: str = ""
    formula: str
    format: str = "number"
    available_in: list[str] = Field(default_factory=list)
    prefix: str = ""                       # display unit before the value, e.g. "$"
    suffix: str = ""                       # display unit after the value, e.g. "%", "×"


def _validate_available_in(model, available_in: list[str]) -> Optional[str]:
    unknown = [k for k in available_in if not model.table(k)]
    if unknown:
        return f"Unknown table(s) in available_in: {', '.join(unknown)}"
    return None


@router.post("/calculated")
async def create_calculated_metric(body: CalcMetricRequest):
    if not body.key:
        return _err(400, "key required", "A unique 'key' is required to create a calculated metric.")
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    if body.key in model.calculated_metrics:
        return _err(409, "already exists", f"Calculated metric '{body.key}' already exists. Use PUT to update.")
    err = _validate_available_in(model, body.available_in)
    if err:
        return _err(400, "invalid available_in", err)

    model.calculated_metrics[body.key] = CalculatedMetric(
        key=body.key,
        display_name=body.display_name,
        description=body.description,
        formula=body.formula,
        format=body.format,
        available_in=list(body.available_in),
        prefix=body.prefix,
        suffix=body.suffix,
    )
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["calculated_metric"] = _calc_metric_to_dict(model.calculated_metrics[body.key])
    return out


@router.put("/calculated/{key}")
async def update_calculated_metric(key: str, body: CalcMetricRequest):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    if key not in model.calculated_metrics:
        return _err(404, "not found", f"No calculated metric '{key}'")
    err = _validate_available_in(model, body.available_in)
    if err:
        return _err(400, "invalid available_in", err)

    model.calculated_metrics[key] = CalculatedMetric(
        key=key,
        display_name=body.display_name,
        description=body.description,
        formula=body.formula,
        format=body.format,
        available_in=list(body.available_in),
        prefix=body.prefix,
        suffix=body.suffix,
    )
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["calculated_metric"] = _calc_metric_to_dict(model.calculated_metrics[key])
    return out


@router.delete("/calculated/{key}")
async def delete_calculated_metric(key: str):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.calculated_metrics:
        return _err(404, "not found", f"No calculated metric '{key}'")
    del model.calculated_metrics[key]
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["deleted"] = key
    return out


# ─── 13. Combined dimensions — create / update / delete ──────────────
class CombinedRuleModel(BaseModel):
    matches: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    is_default: bool = False
    range_min: Optional[float] = None
    range_max: Optional[float] = None


class CombinedDimRequest(BaseModel):
    key: Optional[str] = None
    display_name: str
    description: str = ""
    base_field: str
    available_in: list[str] = Field(default_factory=list)
    rules: list[CombinedRuleModel] = Field(default_factory=list)


def _build_combined_dim(key: str, body: CombinedDimRequest) -> CombinedDimension:
    rules = [
        CombinedDimensionRule(
            matches=list(r.matches),
            label=r.label,
            is_default=r.is_default,
            range_min=r.range_min,
            range_max=r.range_max,
        )
        for r in body.rules
    ]
    return CombinedDimension(
        key=key,
        display_name=body.display_name,
        description=body.description,
        base_field=body.base_field,
        available_in=list(body.available_in),
        rules=rules,
    )


@router.post("/combined")
async def create_combined_dimension(body: CombinedDimRequest):
    if not body.key:
        return _err(400, "key required", "A unique 'key' is required to create a combined dimension.")
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if body.key in model.combined_dimensions:
        return _err(409, "already exists", f"Combined dimension '{body.key}' already exists. Use PUT to update.")
    err = _validate_available_in(model, body.available_in)
    if err:
        return _err(400, "invalid available_in", err)

    model.combined_dimensions[body.key] = _build_combined_dim(body.key, body)
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["combined_dimension"] = _combined_dim_to_dict(model.combined_dimensions[body.key])
    return out


@router.put("/combined/{key}")
async def update_combined_dimension(key: str, body: CombinedDimRequest):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.combined_dimensions:
        return _err(404, "not found", f"No combined dimension '{key}'")
    err = _validate_available_in(model, body.available_in)
    if err:
        return _err(400, "invalid available_in", err)

    model.combined_dimensions[key] = _build_combined_dim(key, body)
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["combined_dimension"] = _combined_dim_to_dict(model.combined_dimensions[key])
    return out


@router.delete("/combined/{key}")
async def delete_combined_dimension(key: str):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.combined_dimensions:
        return _err(404, "not found", f"No combined dimension '{key}'")
    del model.combined_dimensions[key]
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["deleted"] = key
    return out


# ─── 14. Business rules — create / update / delete ───────────────────
class BusinessRuleRequest(BaseModel):
    key: Optional[str] = None
    display_name: str
    description: str = ""
    table: str
    field: str
    op: str
    value: Any = None


@router.post("/rules")
async def create_business_rule(body: BusinessRuleRequest):
    if not body.key:
        return _err(400, "key required", "A unique 'key' is required to create a business rule.")
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if body.key in model.business_rules:
        return _err(409, "already exists", f"Business rule '{body.key}' already exists. Use PUT to update.")

    ref_err = _validate_field_ref(model, body.table, body.field)
    if ref_err:
        return _err(400, "invalid field reference", ref_err)
    op_err = _validate_op_value(body.op, body.value)
    if op_err:
        return _err(400, "invalid op/value", op_err)

    model.business_rules[body.key] = BusinessRule(
        key=body.key, display_name=body.display_name, description=body.description,
        table=body.table, field=body.field, op=body.op, value=body.value,
    )
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["business_rule"] = _business_rule_to_dict(model.business_rules[body.key])
    return out


@router.put("/rules/{key}")
async def update_business_rule(key: str, body: BusinessRuleRequest):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.business_rules:
        return _err(404, "not found", f"No business rule '{key}'")

    ref_err = _validate_field_ref(model, body.table, body.field)
    if ref_err:
        return _err(400, "invalid field reference", ref_err)
    op_err = _validate_op_value(body.op, body.value)
    if op_err:
        return _err(400, "invalid op/value", op_err)

    model.business_rules[key] = BusinessRule(
        key=key, display_name=body.display_name, description=body.description,
        table=body.table, field=body.field, op=body.op, value=body.value,
    )
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["business_rule"] = _business_rule_to_dict(model.business_rules[key])
    return out


@router.delete("/rules/{key}")
async def delete_business_rule(key: str):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.business_rules:
        return _err(404, "not found", f"No business rule '{key}'")
    del model.business_rules[key]
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["deleted"] = key
    return out


# ─── Hierarchies — ordered drill paths (consumed by dashboard drill-down) ──
def _hierarchy_to_dict(h: Hierarchy) -> dict:
    return {"key": h.key, "display_name": h.display_name, "levels": list(h.levels),
            "available_in": list(h.available_in), "description": h.description}


class HierarchyRequest(BaseModel):
    key: Optional[str] = None
    display_name: str
    levels: list[str] = Field(default_factory=list)
    available_in: list[str] = Field(default_factory=list)
    description: str = ""


@router.get("/hierarchies")
async def list_hierarchies(draft: bool = False):
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    items = [_hierarchy_to_dict(h) for h in getattr(model, "hierarchies", {}).values()]
    return {"hierarchies": items, "count": len(items)}


@router.post("/hierarchies")
async def create_hierarchy(body: HierarchyRequest):
    if not body.key:
        return _err(400, "key required", "A unique 'key' is required to create a hierarchy.")
    if len(body.levels) < 2:
        return _err(400, "too few levels", "A hierarchy needs at least 2 ordered levels.")
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if body.key in model.hierarchies:
        return _err(409, "already exists", f"Hierarchy '{body.key}' already exists. Use PUT to update.")
    err = _validate_available_in(model, body.available_in)
    if err:
        return _err(400, "invalid available_in", err)
    model.hierarchies[body.key] = Hierarchy(
        key=body.key, display_name=body.display_name, levels=list(body.levels),
        available_in=list(body.available_in), description=body.description)
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["hierarchy"] = _hierarchy_to_dict(model.hierarchies[body.key])
    return out


@router.put("/hierarchies/{key}")
async def update_hierarchy(key: str, body: HierarchyRequest):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.hierarchies:
        return _err(404, "not found", f"No hierarchy '{key}'")
    model.hierarchies[key] = Hierarchy(
        key=key, display_name=body.display_name, levels=list(body.levels),
        available_in=list(body.available_in), description=body.description)
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["hierarchy"] = _hierarchy_to_dict(model.hierarchies[key])
    return out


@router.delete("/hierarchies/{key}")
async def delete_hierarchy(key: str):
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if key not in model.hierarchies:
        return _err(404, "not found", f"No hierarchy '{key}'")
    del model.hierarchies[key]
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["deleted"] = key
    return out


# ═══════════════════════════════════════════════════════════════════
#  AUTO-CURATION — introspect a raw BQ table and generate a curated
#  TableDef with heuristics. Lets us scale from 4 curated tables to all
#  24 Gold tables without hand-writing every definition.
#
#  Heuristics (a sensible first pass; refine via the authoring UI):
#    • DATE/DATETIME/TIMESTAMP/TIME      → date field
#    • STRING/BOOL/BYTES                 → dimension
#    • numeric ending in _id/_key/_sk/id → dimension (identifier) + join key
#    • other numeric                     → metric (SUM); 'micros' → divide_by 1e6
#    • columns starting with '_'         → skipped (internal/audit)
#    • cluster + kind inferred from the table name
# ═══════════════════════════════════════════════════════════════════

_DATE_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIME"}
_NUM_TYPES = {"INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
_ID_SUFFIXES = ("_id", "_key", "_sk", "_pk", "_fk")


def _title_case(s: str) -> str:
    parts = [w for w in re.split(r"[_\s]+", s) if w]
    return " ".join(w[:1].upper() + w[1:] for w in parts)


def _infer_cluster(raw_table: str) -> str:
    n = raw_table.lower()
    if "ga4" in n or "session" in n or "ga_" in n or "analytics" in n or "event" in n or "pageview" in n:
        return "ga4"
    if "search" in n or "keyword" in n or "query" in n or "serp" in n:
        return "search"
    if "date" in n or "calendar" in n or "time" in n:
        return "time"
    if "ad" in n or "campaign" in n or "platform" in n or "creative" in n or "adgroup" in n:
        return "ad_platforms"
    return "ad_platforms"


def _infer_kind(raw_table: str) -> str:
    n = raw_table.lower()
    if "fact" in n:
        return "fact"
    if "dim" in n:
        return "dim"
    return "dim"


def _derive_table_key(raw_table: str, taken: set) -> str:
    base = re.sub(r"^(ad|ga4|search)_+", "", raw_table.lower())
    base = re.sub(r"(dim|fact)_+", "", base)
    base = re.sub(r"[^a-z0-9_]+", "_", base).strip("_") or raw_table.lower()
    key = base
    i = 2
    while key in taken:
        key = f"{base}_{i}"
        i += 1
    return key


def _strip_table_display(raw_table: str) -> str:
    name = re.sub(r"^(ad|ga4|search)_+", "", raw_table.lower())
    name = re.sub(r"(dim|fact)_+", "", name)
    return _title_case(name) or _title_case(raw_table)


def _classify_column(col) -> Optional[str]:
    """Return 'date' | 'dimension' | 'metric', or None to skip."""
    name = col.name.lower()
    if name.startswith("_"):
        return None  # internal/audit column
    t = (col.type or "").upper()
    if t in _DATE_TYPES:
        return "date"
    if t in _NUM_TYPES:
        if name == "id" or name.endswith(_ID_SUFFIXES):
            return "dimension"
        return "metric"
    return "dimension"  # STRING, BOOL, BYTES, etc.


def _build_table_def_from_schema(raw_table: str, schema, table_key: str,
                                 cluster: Optional[str], kind: Optional[str],
                                 source_key: str = "bigquery_gold") -> tuple[TableDef, dict]:
    """Build a TableDef from a live RawTableSchema. Returns (table_def, summary)."""
    cluster = cluster or _infer_cluster(raw_table)
    kind = kind or _infer_kind(raw_table)

    dims, mets, dates, join_keys, skipped = [], [], [], [], []
    for col in schema.columns:
        kind_of = _classify_column(col)
        name = col.name
        lname = name.lower()
        if kind_of is None:
            skipped.append(name)
            continue
        display = _title_case(name)
        if kind_of == "date":
            dates.append(FieldDef(key=lname, column=name, display_name=display,
                                  field_kind="date", type=col.type))
        elif kind_of == "metric":
            divide_by = 1_000_000 if "micros" in lname else None
            fmt = "currency" if any(k in lname for k in ("cost", "spend", "value", "revenue", "budget")) else "number"
            mets.append(FieldDef(key=lname, column=name, display_name=display, field_kind="metric",
                                 aggregation="SUM", format=fmt, divide_by=divide_by))
        else:  # dimension
            dims.append(FieldDef(key=lname, column=name, display_name=display,
                                 field_kind="dimension", type=col.type))
            if lname == "id" or lname.endswith(_ID_SUFFIXES):
                join_keys.append(name)

    td = TableDef(
        key=table_key,
        display_name=_strip_table_display(raw_table),
        description=f"Auto-curated from {raw_table}. Refine names and field roles as needed.",
        source=source_key,
        raw_table=raw_table,
        cluster=cluster,
        kind=kind,
        primary_key=None,
        join_keys=join_keys,
        dimensions=dims,
        metrics=mets,
        dates=dates,
    )
    summary = {
        "key": table_key, "raw_table": raw_table, "display_name": td.display_name,
        "cluster": cluster, "kind": kind,
        "dimensions": len(dims), "metrics": len(mets), "dates": len(dates),
        "skipped": skipped,
        "warnings": [],
    }
    if not mets and kind == "fact":
        summary["warnings"].append("No metrics detected for a fact table — check classification.")
    if len(schema.columns) > 0 and not (dims or mets or dates):
        summary["warnings"].append("No fields surfaced.")
    return td, summary


def _source_project_dataset(model) -> tuple[Optional[str], Optional[str]]:
    for s in model.sources.values():
        if s.type == "bigquery" and s.project and s.dataset:
            return s.project, s.dataset
    return None, None


def _source_key_for(model, project: str, dataset: str) -> Optional[str]:
    """Return the source key whose (project, dataset) matches, or None."""
    for key, s in model.sources.items():
        if s.type == "bigquery" and s.project == project and s.dataset == dataset:
            return key
    return None


# ─── 15. POST /api/semantic/autocurate — curate one raw table ────────
class AutocurateRequest(BaseModel):
    raw_table: str
    project: Optional[str] = None      # if omitted, use default source
    dataset: Optional[str] = None      # if omitted, use default source
    cluster: Optional[str] = None
    kind: Optional[str] = None
    overwrite: bool = False


@router.post("/autocurate")
async def autocurate_table(body: AutocurateRequest):
    """Introspect one raw BQ table and add a heuristically-curated definition."""
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    # Honour explicit project/dataset from the request — needed now that we
    # support multiple BigQuery sources. Fall back to default source.
    if body.project and body.dataset:
        project, dataset = body.project, body.dataset
    else:
        project, dataset = _source_project_dataset(model)
    if not project or not dataset:
        return _err(400, "no source", "No default BigQuery source in semantic.yaml")

    # NOTE: the (source, raw_table) duplicate check happens AFTER introspect,
    # below, so we can disambiguate the same raw name across multiple datasets.

    try:
        schema = await asyncio.get_running_loop().run_in_executor(
            None, introspect_table, project, dataset, body.raw_table)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "introspect failed", str(e))

    # Tables in different datasets can share a raw name (e.g. dim_date in gold
    # AND data_mart_er). Match on (source, raw_table) — not raw_table alone.
    src_key = _source_key_for(model, project, dataset) or "bigquery_gold"
    taken = set(model.tables.keys())
    existing_key = next((t.key for t in model.tables.values()
                         if t.raw_table == body.raw_table and t.source == src_key), None)
    if existing_key and body.overwrite:
        taken.discard(existing_key)
        del model.tables[existing_key]
    if existing_key and not body.overwrite:
        return _err(409, "already curated", f"'{body.raw_table}' in {dataset} is already curated as '{existing_key}'. Pass overwrite=true to replace.")
    # Disambiguate the key by dataset when the simple name is taken
    base_key = _derive_table_key(body.raw_table, set())
    table_key = existing_key if (existing_key and body.overwrite) else (
        base_key if base_key not in taken else _derive_table_key(f"{dataset}_{body.raw_table}", taken)
    )

    td, summary = _build_table_def_from_schema(body.raw_table, schema, table_key, body.cluster, body.kind, source_key=src_key)
    model.tables[table_key] = td

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["curated"] = summary
    return out


# ─── 16. POST /api/semantic/autocurate_all — curate all uncurated ────
class AutocurateAllRequest(BaseModel):
    project: Optional[str] = None      # if omitted, use default source
    dataset: Optional[str] = None      # if omitted, use default source
    overwrite: bool = False


@router.post("/autocurate_all")
async def autocurate_all(body: AutocurateAllRequest):
    """Introspect and curate every table in the dataset not already curated (one save at the end).

    When called without project/dataset, uses the default source (first BQ source in semantic.yaml).
    Pass project+dataset to bulk-curate a specific dataset (e.g. data_mart_er, roi_exercise).
    """
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    if body.project and body.dataset:
        project, dataset = body.project, body.dataset
    else:
        project, dataset = _source_project_dataset(model)
    if not project or not dataset:
        return _err(400, "no source", "No default BigQuery source in semantic.yaml")

    try:
        all_tables = await asyncio.get_running_loop().run_in_executor(
            None, list_dataset_tables, project, dataset)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "list tables failed", str(e))

    src_key = _source_key_for(model, project, dataset) or "bigquery_gold"
    # Already-curated check is now (source, raw_table) — same raw name can
    # appear in multiple datasets and each needs its own curated entry.
    already = {t.raw_table for t in model.tables.values() if t.source == src_key}
    targets = all_tables if body.overwrite else [t for t in all_tables if t not in already]

    results, errors = [], []
    for raw in targets:
        try:
            schema = await asyncio.get_running_loop().run_in_executor(
                None, introspect_table, project, dataset, raw)
        except Exception as e:
            errors.append({"raw_table": raw, "error": str(e)})
            continue
        taken = set(model.tables.keys())
        existing_key = next((t.key for t in model.tables.values()
                             if t.raw_table == raw and t.source == src_key), None)
        if existing_key and body.overwrite:
            taken.discard(existing_key)
            del model.tables[existing_key]
        base_key = _derive_table_key(raw, set())
        table_key = existing_key if (existing_key and body.overwrite) else (
            base_key if base_key not in taken else _derive_table_key(f"{dataset}_{raw}", taken)
        )
        td, summary = _build_table_def_from_schema(raw, schema, table_key, None, None, source_key=src_key)
        model.tables[table_key] = td
        results.append(summary)

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["curated_count"] = len(results)
    out["total_tables"] = len(model.tables)
    out["curated"] = results
    out["errors"] = errors
    return out


# ═══════════════════════════════════════════════════════════════════
#  METRIC BUILDER — formula tokens, live validation, preview
# ═══════════════════════════════════════════════════════════════════

# Function/snippet palette surfaced to the formula builder UI.
FORMULA_FUNCTIONS = [
    {"label": "SAFE_DIVIDE", "snippet": "SAFE_DIVIDE(${1}, ${2})", "desc": "Divide, returns NULL on /0 (preferred over NULLIF)"},
    {"label": "NULLIF", "snippet": "NULLIF(${1}, 0)", "desc": "NULL when equal — classic divide-by-zero guard"},
    {"label": "SUM", "snippet": "SUM(${1})", "desc": "Sum"},
    {"label": "AVG", "snippet": "AVG(${1})", "desc": "Average"},
    {"label": "COUNT", "snippet": "COUNT(${1})", "desc": "Count rows"},
    {"label": "COUNT DISTINCT", "snippet": "COUNT(DISTINCT ${1})", "desc": "Distinct count"},
    {"label": "CASE WHEN", "snippet": "CASE WHEN ${1} THEN ${2} ELSE ${3} END", "desc": "Conditional"},
    {"label": "COALESCE", "snippet": "COALESCE(${1}, ${2})", "desc": "First non-null"},
    {"label": "ROUND", "snippet": "ROUND(${1}, 2)", "desc": "Round to N places"},
    {"label": "* 100", "snippet": " * 100", "desc": "Scale to percentage"},
    {"label": "* 1000", "snippet": " * 1000", "desc": "Per-thousand (e.g. CPM)"},
]


# ─── 17. GET /api/semantic/formula_tokens ────────────────────────────
@router.get("/formula_tokens")
async def formula_tokens(table: str, draft: bool = False):
    """Referenceable tokens for building a formula against {table}: native metrics,
    calculated metrics, and dimensions. Plus the static function palette."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    t = model.table(table)
    if not t:
        return _err(404, "table not found", f"No curated table '{table}'")

    native = [{"key": f.key, "display_name": f.display_name, "kind": "metric"} for f in t.metrics]
    calc = [{"key": m.key, "display_name": m.display_name, "kind": "calc"}
            for m in model.calc_metrics_for(table)]
    dims = [{"key": f.key, "display_name": f.display_name, "kind": "dimension"} for f in t.dimensions]
    return {
        "table": table,
        "metrics": native + calc,     # both are referenceable as {key}
        "dimensions": dims,
        "functions": FORMULA_FUNCTIONS,
    }


# ─── 18. POST /api/semantic/validate_formula ─────────────────────────
class ValidateFormulaRequest(BaseModel):
    formula: str
    available_in: list[str] = Field(default_factory=list)
    format: str = "number"


def _build_validation_sql(base_model, formula: str, table: str, fmt: str):
    """On a throwaway copy of the model, add a temp metric and build its SQL."""
    model = copy.deepcopy(base_model)
    tmp = "__validate_tmp__"
    model.calculated_metrics[tmp] = CalculatedMetric(
        key=tmp, display_name="tmp", description="", formula=formula,
        format=fmt, available_in=[table],
    )
    q = StructuredQuery.from_dict({
        "primary_table": table,
        "metrics": [{"table": table, "field": tmp}],
        "limit": 1,
    })
    return build_sql(q, model)


@router.post("/validate_formula")
async def validate_formula(body: ValidateFormulaRequest):
    """Validate a candidate formula: resolve refs, build SQL, dry-run on BigQuery (0 cost)."""
    if not body.formula.strip():
        return {"valid": False, "error": "Formula is empty."}
    try:
        base = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if not body.available_in:
        return {"valid": False, "error": "Pick at least one table for the metric (Available in)."}
    table = body.available_in[0]
    if not base.table(table):
        return {"valid": False, "error": f"Unknown table '{table}'."}

    # 1. Resolve + build (catches unknown {refs}, bad structure)
    try:
        sql = _build_validation_sql(base, body.formula, table, body.format)
    except (QueryBuildError, ResolverError) as e:
        return {"valid": False, "error": str(e)}
    except Exception as e:
        return {"valid": False, "error": f"Could not build SQL: {e}"}

    # 2. Dry-run (catches SQL syntax / type errors) — free on BigQuery
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, partial(run_query, sql, dry_run=True))
    except ExecutorConfigError as e:
        # BQ not configured (e.g. dev box) — formula resolved structurally though.
        return {"valid": None, "warning": "Resolved OK, but BigQuery isn't configured to dry-run here.",
                "resolved_sql": sql, "detail": str(e)}
    except Exception as e:
        return {"valid": False, "error": str(e), "resolved_sql": sql}

    return {"valid": True, "resolved_sql": sql, "validated_against": table}


# ─── 19. POST /api/semantic/preview_metric ───────────────────────────
class PreviewMetricRequest(BaseModel):
    formula: str
    available_in: list[str] = Field(default_factory=list)
    format: str = "number"


@router.post("/preview_metric")
async def preview_metric(body: PreviewMetricRequest):
    """Run the candidate formula as an overall aggregate and return the sample value."""
    if not body.formula.strip():
        return {"ok": False, "error": "Formula is empty."}
    try:
        base = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if not body.available_in:
        return {"ok": False, "error": "Pick at least one table for the metric (Available in)."}
    table = body.available_in[0]
    if not base.table(table):
        return {"ok": False, "error": f"Unknown table '{table}'."}

    try:
        sql = _build_validation_sql(base, body.formula, table, body.format)
    except (QueryBuildError, ResolverError) as e:
        return {"ok": False, "error": str(e)}

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return {"ok": False, "error": "BigQuery not configured: " + str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e), "resolved_sql": sql}

    value = result.rows[0][0] if result.rows and result.rows[0] else None
    return {"ok": True, "value": value, "format": body.format,
            "validated_against": table, "elapsed_ms": result.elapsed_ms}


# ═══════════════════════════════════════════════════════════════════
#  SCHEMA GRAPH (ERD) — tables + fields + joins in one call, join CRUD
# ═══════════════════════════════════════════════════════════════════

# ─── 20. GET /api/semantic/graph ─────────────────────────────────────
@router.get("/graph")
async def semantic_graph(draft: bool = False):
    """Everything the ERD needs: all curated tables (with fields) and all joins."""
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    tables = []
    for t in model.tables.values():
        tables.append({
            "key": t.key,
            "display_name": t.display_name,
            "cluster": t.cluster,
            "kind": t.kind,
            "raw_table": t.raw_table,
            "source": t.source,
            "primary_key": t.primary_key,
            "join_keys": list(t.join_keys),
            "dimensions": [{"key": f.key, "display_name": f.display_name, "field_kind": "dimension"} for f in t.dimensions],
            "metrics": [{"key": f.key, "display_name": f.display_name, "field_kind": "metric"} for f in t.metrics],
            "dates": [{"key": f.key, "display_name": f.display_name, "field_kind": "date"} for f in t.dates],
            "field_count": len(t.dimensions) + len(t.metrics) + len(t.dates),
        })
    joins = [
        {"left": j.left, "right": j.right, "on": j.on,
         "join_type": getattr(j, "join_type", "inner") or "inner"}
        for j in model.joins
    ]
    clusters = {k: {"display_name": c.display_name, "color": c.color} for k, c in model.clusters.items()}
    return {"tables": tables, "joins": joins, "clusters": clusters,
            "table_count": len(tables), "join_count": len(joins)}


# ─── 21. POST /api/semantic/joins — add or update a join (by left+right) ──
class JoinRequest(BaseModel):
    left: str
    right: str
    on: str
    # SQL JOIN semantics — defaults to 'inner' for back-compat. Normalized below.
    join_type: Optional[str] = "inner"


_VALID_JOIN_TYPES = ("inner", "left", "right", "full", "cross")


def _join_warnings(model, body: JoinRequest) -> list[str]:
    warns = []
    lt, rt = model.table(body.left), model.table(body.right)
    if lt and body.on not in (lt.join_keys or []):
        warns.append(f"'{body.on}' is not listed as a join key on '{body.left}'.")
    if rt and body.on not in (rt.join_keys or []):
        warns.append(f"'{body.on}' is not listed as a join key on '{body.right}'.")
    return warns


@router.post("/joins")
async def upsert_join(body: JoinRequest):
    """Add a join, or update the key if one already exists between the two tables."""
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if not model.table(body.left) or not model.table(body.right):
        return _err(404, "table not found", f"Both tables must be curated: '{body.left}', '{body.right}'.")
    if body.left == body.right:
        return _err(400, "invalid join", "Cannot join a table to itself.")

    warns = _join_warnings(model, body)
    # Normalize join_type
    jt = (body.join_type or "inner").strip().lower()
    if jt not in _VALID_JOIN_TYPES:
        return _err(400, "invalid join_type", f"join_type must be one of {_VALID_JOIN_TYPES} (got '{body.join_type}').")
    found = False
    for j in model.joins:
        if {j.left, j.right} == {body.left, body.right}:
            j.on = body.on
            j.join_type = jt
            found = True
            break
    if not found:
        model.joins.append(JoinDef(left=body.left, right=body.right, on=body.on, join_type=jt))

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["join"] = {"left": body.left, "right": body.right, "on": body.on, "join_type": jt, "updated": found}
    out["warnings"] = warns
    return out


# ─── 22. DELETE /api/semantic/joins — remove a join by pair ──────────
class JoinDeleteRequest(BaseModel):
    left: str
    right: str


@router.post("/joins/delete")
async def delete_join(body: JoinDeleteRequest):
    """Remove the join between two tables (order-insensitive)."""
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    before = len(model.joins)
    model.joins = [j for j in model.joins if {j.left, j.right} != {body.left, body.right}]
    if len(model.joins) == before:
        return _err(404, "not found", f"No join between '{body.left}' and '{body.right}'.")
    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["deleted"] = {"left": body.left, "right": body.right}
    return out


# ─── R37. GET /api/semantic/joins/suggest — auto-detect FK candidates ──────
# Scores potential joins between curated tables using name heuristics.
# Pure heuristics (no BQ INFORMATION_SCHEMA scan) so the call is fast and
# works even when the warehouse has no declared FKs (the common case for
# analytical Fivetran landings).
@router.get("/joins/suggest")
async def suggest_joins(min_confidence: int = 30):
    """Return ranked join suggestions. Excludes pairs that already have a join."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    tables = list(model.tables.values())
    # Pair-set of existing joins so we never suggest a duplicate
    existing_pairs = {frozenset({j.left, j.right}) for j in model.joins}

    # Build a quick index: every field key per table + flagged join_keys
    table_keys: dict[str, set[str]] = {}
    table_join_keys: dict[str, set[str]] = {}
    table_names_lower: dict[str, str] = {}
    for t in tables:
        keys = set()
        for f in t.dimensions: keys.add(f.key)
        for f in t.metrics:    keys.add(f.key)
        for f in t.dates:      keys.add(f.key)
        table_keys[t.key] = keys
        table_join_keys[t.key] = set(t.join_keys or [])
        # Strip common prefixes/suffixes from a key for "contains table name" matching
        table_names_lower[t.key] = re.sub(r"[^a-z0-9]", "", t.key.lower())

    def _score(left: str, right: str, col: str) -> tuple[int, list[str]]:
        """Return (score, reasons). Higher = more confident this is a real FK."""
        s = 0
        why = []
        col_l = col.lower()
        # 1. Flagged in join_keys on both sides — almost certainly a real key
        if col in table_join_keys.get(left, set()) and col in table_join_keys.get(right, set()):
            s += 50; why.append("listed as a join key on both tables")
        elif col in table_join_keys.get(left, set()) or col in table_join_keys.get(right, set()):
            s += 25; why.append("listed as a join key on one side")
        # 2. Column name contains the other table's name (campaign_id ↔ campaigns)
        nl = table_names_lower.get(left, "")
        nr = table_names_lower.get(right, "")
        col_squashed = re.sub(r"[^a-z0-9]", "", col_l)
        if nl and len(nl) >= 4 and nl in col_squashed:
            s += 40; why.append(f"column name references the '{left}' table")
        if nr and len(nr) >= 4 and nr in col_squashed:
            s += 40; why.append(f"column name references the '{right}' table")
        # 3. Conventional FK naming
        if col_l.endswith("_id") or col_l.endswith("_key") or col_l.endswith("_uuid") or col_l.endswith("_fk"):
            s += 25; why.append("follows the *_id/_key/_uuid foreign-key naming convention")
        elif col_l == "id":
            # Bare 'id' is usually a primary key, ambiguous as a join — lower score
            s += 5; why.append("bare 'id' column (often a primary key, weaker as a join target)")
        # 4. Date columns shouldn't be joined on directly (almost always wrong)
        date_cols_l = {f.key for f in model.table(left).dates}
        date_cols_r = {f.key for f in model.table(right).dates}
        if col in date_cols_l and col in date_cols_r:
            s -= 30; why.append("⚠ date column — joining tables on a raw date is almost always wrong")
        return s, why

    candidates = []
    seen = set()
    for i, ta in enumerate(tables):
        for tb in tables[i+1:]:
            pair = frozenset({ta.key, tb.key})
            if pair in existing_pairs:
                continue
            shared = table_keys[ta.key] & table_keys[tb.key]
            for col in shared:
                score, why = _score(ta.key, tb.key, col)
                if score < min_confidence:
                    continue
                sig = (ta.key, tb.key, col)
                if sig in seen:
                    continue
                seen.add(sig)
                tier = "high" if score >= 70 else "medium" if score >= 45 else "low"
                candidates.append({
                    "left": ta.key,
                    "right": tb.key,
                    "on": col,
                    "confidence": score,
                    "tier": tier,
                    "reasons": why,
                })

    candidates.sort(key=lambda x: (-x["confidence"], x["left"], x["right"]))
    return {"suggestions": candidates, "count": len(candidates)}


# ═══════════════════════════════════════════════════════════════════
#  DATA MARTS — list datasets, curate tables from any dataset
# ═══════════════════════════════════════════════════════════════════

# ─── 23. GET /api/semantic/datasets ──────────────────────────────────
@router.get("/datasets")
async def list_datasets(project: Optional[str] = None):
    """List datasets in the project (defaults to the configured BQ project)."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    proj, ds = _source_project_dataset(model)
    project = project or proj
    if not project:
        return _err(400, "no source", "No default BigQuery project in semantic.yaml")

    sql = f"SELECT schema_name FROM `{project}`.INFORMATION_SCHEMA.SCHEMATA ORDER BY schema_name"
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "list datasets failed", str(e))

    # Which datasets already have curated tables?
    curated_datasets = set()
    for t in model.tables.values():
        s = model.sources.get(t.source)
        if s and s.dataset:
            curated_datasets.add(s.dataset)

    datasets = []
    for row in result.rows:
        name = row[0]
        datasets.append({"name": name, "has_curated": name in curated_datasets,
                         "is_default": name == ds})
    return {"project": project, "count": len(datasets), "datasets": datasets}


def _ensure_source(model, project: str, dataset: str) -> str:
    """Return the source key for (project, dataset), creating one if needed."""
    for key, s in model.sources.items():
        if s.type == "bigquery" and s.project == project and s.dataset == dataset:
            return key
    key = f"bigquery_{re.sub(r'[^a-z0-9_]+', '_', dataset.lower())}"
    model.sources[key] = SourceDef(key=key, type="bigquery", project=project, dataset=dataset)
    return key


# ─── 24. POST /api/semantic/autocurate_dataset ───────────────────────
class AutocurateDatasetRequest(BaseModel):
    dataset: str
    project: Optional[str] = None
    overwrite: bool = False


@router.post("/autocurate_dataset")
async def autocurate_dataset(body: AutocurateDatasetRequest):
    """Auto-curate every table in a given dataset (e.g. a data mart), adding a
    source for that dataset if needed."""
    try:
        model = _load_fresh()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    proj, _ = _source_project_dataset(model)
    project = body.project or proj
    if not project:
        return _err(400, "no source", "No default BigQuery project in semantic.yaml")

    source_key = _ensure_source(model, project, body.dataset)

    try:
        all_tables = await asyncio.get_running_loop().run_in_executor(
            None, list_dataset_tables, project, body.dataset)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "list tables failed", str(e))

    # Already-curated raw tables within this source
    already = {t.raw_table for t in model.tables.values() if t.source == source_key}
    targets = all_tables if body.overwrite else [t for t in all_tables if t not in already]

    results, errors = [], []
    for raw in targets:
        try:
            schema = await asyncio.get_running_loop().run_in_executor(
                None, introspect_table, project, body.dataset, raw)
        except Exception as e:
            errors.append({"raw_table": raw, "error": str(e)})
            continue
        taken = set(model.tables.keys())
        existing_key = next((t.key for t in model.tables.values()
                             if t.raw_table == raw and t.source == source_key), None)
        if existing_key and body.overwrite:
            taken.discard(existing_key)
            del model.tables[existing_key]
        table_key = existing_key if (existing_key and body.overwrite) else _derive_table_key(raw, taken)
        td, summary = _build_table_def_from_schema(raw, schema, table_key, None, None, source_key=source_key)
        model.tables[table_key] = td
        results.append(summary)

    try:
        out = await _persist(model)
    except WriterError as e:
        return _err(500, "save failed", str(e))
    out["dataset"] = body.dataset
    out["source_key"] = source_key
    out["curated_count"] = len(results)
    out["total_tables"] = len(model.tables)
    out["curated"] = results
    out["errors"] = errors
    return out


# ─── 25. GET /api/semantic/distinct — distinct values of a dimension ──
@router.get("/distinct")
async def distinct_values(table: str, field: str, limit: int = 200):
    """Distinct values of a dimension/date field — for populating filter pickers."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))
    if not model.table(table):
        return _err(404, "table not found", f"No curated table '{table}'")
    try:
        expr = field_sql(table, field, model)
        tsql = table_sql(table, model)
    except ResolverError as e:
        return _err(400, "resolve failed", str(e))
    lim = max(1, min(int(limit or 200), 1000))
    sql = ("SELECT " + expr + " AS value FROM " + tsql + " AS `" + table + "` "
           "WHERE " + expr + " IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT " + str(lim))
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "distinct query failed", str(e))
    return {"table": table, "field": field, "count": result.row_count,
            "values": [r[0] for r in result.rows]}


# ═══════════════════════════════════════════════════════════════════
#  AI ASK — natural language → structured query (via Claude),
#  constrained to the curated semantic layer so it can't hallucinate.
# ═══════════════════════════════════════════════════════════════════

# ─── Question-aware table reranking ───────────────────────────────────
# When the user's prompt contains certain domain keywords, we surface the
# right MART at the top of the catalog and remove tables that look superficially
# right but actually have wrong data (the classic example: "lead source" →
# Haiku picks `sessions` from training-data priors; we want `data_mart_er_leads`).
#
# Each rule is (trigger_regex, hoist_keys_in_priority_order, ban_keys).
# - hoist_keys: tables to surface FIRST in the catalog with a [PRIMARY FOR THIS QUESTION] tag
# - ban_keys: tables to REMOVE from the catalog entirely (Haiku can't pick what it can't see)
_CATALOG_RULES = [
    # Lead source / pipeline / funnel — must use leads mart, never sessions
    # NOTE: regex requires "lead/leads" near "source|conversion|funnel|pipeline|by|to"
    # so we DON'T accidentally match "Cost PER LEAD by service line" (that's
    # a campaign-efficiency question that needs gold's ad_performance with the
    # campaigns.service_line join, not the leads mart).
    (
        re.compile(
            r"\b(lead.source|leads.source|lead.conversion|lead.funnel|lead.pipeline"
            r"|lead.to.opp|leads.to.opp|leads.by.\w+|top.\w*.leads"
            r"|conversion.analysis|pipeline.conversion)\b",
            re.IGNORECASE,
        ),
        ["data_mart_er_leads"],
        ["sessions", "ga4_dim_sessions", "ga4_fact_events"],
    ),
    # Revenue / ROI / fiscal year — must use revenue mart, not gold facts
    (
        re.compile(r"\b(revenue|romi|fiscal.year|attributed.revenue|marketing.revenue)\b", re.IGNORECASE),
        ["revenue_attributed_to_marketing"],
        [],
    ),
    # Top-N campaign / channel mix — prefer the mart with channel+CTR+cost+leads in one row.
    # We also hoist `combined_campaign_stats` (without _dashboard) as a secondary
    # fallback since it has per-campaign leads, and we ban `campaign_expense`
    # because the planner reflexively pairs `campaigns` + `campaign_expense.leads`
    # which has no join path → 422.
    # NOTE: do NOT match "cost per lead" / "cpl" here — those need the
    # service_line-aware mart (report_executive_summary_view), handled below.
    (
        re.compile(r"\b(top.*campaigns?|channel.mix|ctr.by.channel)\b", re.IGNORECASE),
        ["combined_campaign_stats_dashboard", "combined_campaign_stats"],
        ["campaign_expense"],
    ),
    # Cost per lead / CPL / cost-per-lead — must use the mart that has
    # cost + leads + service_line in one row. The campaigns + ad_performance +
    # campaign_expense join doesn't actually work (campaign_expense doesn't join
    # campaigns), so we hoist report_executive_summary_view and ban
    # campaign_expense to keep the planner from reaching for it.
    (
        re.compile(
            r"\b(cost.per.lead|cost.+per.+lead|cpl|cost.+by.+service.+line|spend.+per.+lead)\b",
            re.IGNORECASE,
        ),
        ["report_executive_summary_view"],
        ["campaign_expense"],
    ),
]


def _rerank_catalog(model, prompt: str):
    """Return (hoisted_table_keys, banned_table_keys) given the user's prompt."""
    hoisted, banned = [], set()
    for pattern, hoist, ban in _CATALOG_RULES:
        if pattern.search(prompt or ""):
            for k in hoist:
                if k not in hoisted:
                    hoisted.append(k)
            banned.update(ban)
    return hoisted, banned


def _ask_context(model, prompt: str = "") -> str:
    """Compact description of the curated layer for the LLM.

    Question-aware: when the prompt matches domain rules in _CATALOG_RULES,
    relevant marts are hoisted to the top and confusable tables are removed
    from the catalog entirely so Haiku physically can't pick them.
    """
    hoisted, banned = _rerank_catalog(model, prompt)
    lines = ["Available curated tables (use ONLY these table keys and field keys):"]

    def _table_line(t, prefix=""):
        dims = [f.key for f in t.dimensions] + [f.key for f in t.dates]
        mets = [f.key for f in t.metrics]
        calc = [m.key for m in model.calc_metrics_for(t.key)]
        return f"- {prefix}{t.key} ({t.kind}): dimensions/dates={dims}; metrics={mets + calc}"

    # 1. Hoisted tables first, with a strong tag
    for hk in hoisted:
        t = model.table(hk)
        if t:
            lines.append(_table_line(t, prefix="**PRIMARY FOR THIS QUESTION** "))

    # 2. Everything else, except hoisted (avoid duplicates) and banned (hidden entirely)
    hoisted_set = set(hoisted)
    for t in model.tables.values():
        if t.key in hoisted_set or t.key in banned:
            continue
        lines.append(_table_line(t))

    if model.joins:
        # Drop join entries that reference banned tables
        joins = ", ".join(
            f"{j.left}<->{j.right} on {j.on}"
            for j in model.joins
            if j.left not in banned and j.right not in banned
        )
        if joins:
            lines.append("Joins (tables that can be combined): " + joins)
    return "\n".join(lines)


def _metric_format(model, table: str, field: str) -> str:
    t = model.table(table)
    if t:
        fd = t.field_by_key(field)
        if fd and fd.format:
            return fd.format
    cm = model.calculated_metrics.get(field)
    if cm and cm.format:
        return cm.format
    return "number"


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # strip ```json fences if present
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_ASK_SYSTEM = (
    "You translate a business question into a single structured query over a curated data model. "
    "Respond with ONLY a JSON object, no prose, no code fences. Schema:\n"
    '{"primary_table": "<table key>", '
    '"dimensions": [{"table":"<key>","field":"<key>"}], '
    '"metrics": [{"table":"<key>","field":"<key>"}], '
    '"filters": [{"table":"<key>","field":"<date_field_key>","op":"between","value":["YYYY-MM-DD","YYYY-MM-DD"]}], '
    '"chart_type": "table|bar|line|pie|kpi", '
    '"title": "<short title>"}\n'
    "Rules:\n"
    "- Use ONLY the table and field keys provided.\n"
    "- Pick a chart_type that matches the question:\n"
    "    • kpi  → a single overall total/number (no dimensions)\n"
    "    • line → trends over time (any date dimension)\n"
    "    • pie  → composition / share, when 2-5 categories\n"
    "    • bar  → ranking / comparison across 2-15 categories\n"
    "    • table→ many categories (>15), or many metric columns, or any drill-list\n"
    "- If a date dimension or date filter could apply but the user didn't specify a range, default to the LAST 90 DAYS\n"
    "  by adding a filters entry of op 'between' on a date field, with the inclusive [YYYY-MM-DD, YYYY-MM-DD] range\n"
    "  (you can assume today's date is the current real date). Do not default a date when the question is clearly\n"
    "  about an all-time total.\n"
    "- Keep the title under 8 words."
)


class AskRequest(BaseModel):
    prompt: str
    history: Optional[list] = None  # [{question, headline, summary, columns, rows_preview}]
    # Optional scope inheritance: when the user asks from a dashboard with an
    # active hierarchy selection / date range, pass the dashboard id and the
    # server resolves them into qdict.filters automatically. So "top campaigns"
    # asked from a dashboard scoped to Pediatrics returns Pediatrics-only.
    dashboard_id: Optional[str] = None
    # Set true on the follow-up call after the user answered a clarifying
    # question, so the planner commits to a spec instead of clarifying again.
    no_clarify: bool = False


# Instruction appended to the planner when clarifying questions are enabled.
# The planner may return a clarify object INSTEAD of a spec, but only when
# genuinely torn — otherwise it should pick the best interpretation and answer.
_ASK_CLARIFY_ADDENDUM = (
    "\n\nClarify-when-torn: If — and ONLY if — the question is genuinely ambiguous in a way that would "
    "materially change the answer, you MAY return this object INSTEAD of a query spec:\n"
    '{"clarify": {"question": "<one short question>", "options": ["<opt1>","<opt2>", "<opt3?>"], "kind": "metric|date|dimension"}}\n'
    "Only clarify for real forks, such as:\n"
    "  • two or more distinct candidate metrics that would give very different answers "
    "(e.g. 'performance' could mean spend, conversions, or ROAS),\n"
    "  • a clearly time-sensitive question with no derivable date window and where the default would likely mislead.\n"
    "Do NOT clarify for minor wording, chart choice, or anything you can reasonably infer. Prefer answering. "
    "Give 2-4 concise, mutually exclusive options phrased as the user would pick them. Never combine clarify with a spec."
)


def _history_for_prompt(history: list) -> str:
    """Compress prior turns into a short context the model can use to refine."""
    if not history:
        return ""
    lines = ["Prior conversation (for context — answer the NEW question, refining from these turns):"]
    for i, t in enumerate(history[-4:], 1):  # cap at last 4 turns to stay light
        q = (t.get("question") or "").strip()
        h = (t.get("headline") or "").strip()
        s = (t.get("summary") or "").strip()
        lines.append(f"Turn {i} Q: {q}")
        if h: lines.append(f"Turn {i} A (headline): {h}")
        if s: lines.append(f"Turn {i} A (summary): {s[:200]}")
        rp = t.get("rows_preview")
        if rp: lines.append(f"Turn {i} A (top rows): {rp[:300]}")
    return "\n".join(lines) + "\n\n"


# ─── Server-side date-phrase resolution ────────────────────────────
# Orlando Health's fiscal year runs April 1 → March 31. "FY 2024" =
# Apr 1 2023 → Mar 31 2024. The planner has trouble keeping that straight,
# so we resolve it server-side and inject a self-contained filter on the
# primary table's first date field BEFORE the resolver runs.

_FY_START_MONTH = 4   # Orlando Health: April 1
_FY_START_DAY = 1


def _resolve_date_phrase(prompt: str) -> Optional[tuple[str, str, str]]:
    """Return (start_iso, end_iso, matched_phrase) or None.

    Recognises:
      • "fiscal year YYYY", "FY YYYY", "FY-YYYY", "FYYYYY"
      • "Q1/Q2/Q3/Q4 YYYY" (fiscal-year quarters)
      • "last fiscal year", "this fiscal year"
      • "last N days", "past N days"
      • "last quarter", "this quarter"
      • "last month", "this month", "MTD"
      • "year YYYY", "calendar year YYYY"
      • "YTD", "year to date"
    """
    from datetime import date, timedelta
    import re as _re
    today = date.today()
    p = prompt.lower()

    def _fy_bounds(fy_year: int) -> tuple[str, str]:
        start = date(fy_year - 1, _FY_START_MONTH, _FY_START_DAY)
        end = date(fy_year, _FY_START_MONTH, _FY_START_DAY) - timedelta(days=1)
        return start.isoformat(), end.isoformat()

    # Explicit fiscal year "FY 2024" / "fiscal year 2024" / "FY2024"
    m = _re.search(r"\b(?:fy|fiscal\s*year)[\s\-]*?(\d{4})\b", p)
    if m:
        s, e = _fy_bounds(int(m.group(1)))
        return s, e, m.group(0)

    # Fiscal quarter "Q1 2024" / "Q2 FY2024"
    m = _re.search(r"\bq([1-4])\s*(?:fy)?\s*(\d{4})\b", p)
    if m:
        q = int(m.group(1))
        fy = int(m.group(2))
        start_month = ((q - 1) * 3 + _FY_START_MONTH - 1) % 12 + 1
        start_year = fy - 1 if start_month >= _FY_START_MONTH else fy
        start = date(start_year, start_month, 1)
        # End of quarter = start + 3 months - 1 day
        end_month = start_month + 2
        end_year = start_year
        while end_month > 12:
            end_month -= 12
            end_year += 1
        # Last day of end_month
        if end_month == 12:
            next_first = date(end_year + 1, 1, 1)
        else:
            next_first = date(end_year, end_month + 1, 1)
        end = next_first - timedelta(days=1)
        return start.isoformat(), end.isoformat(), m.group(0)

    # "last fiscal year" / "this fiscal year"
    if "last fiscal year" in p or "previous fiscal year" in p:
        # Current FY is the FY whose end date is in the future or today
        if today.month >= _FY_START_MONTH:
            cur_fy = today.year + 1
        else:
            cur_fy = today.year
        s, e = _fy_bounds(cur_fy - 1)
        return s, e, "last fiscal year"
    if "this fiscal year" in p or "current fiscal year" in p:
        if today.month >= _FY_START_MONTH:
            cur_fy = today.year + 1
        else:
            cur_fy = today.year
        s, e = _fy_bounds(cur_fy)
        return s, e, "this fiscal year"

    # "last N days" / "past N days"
    m = _re.search(r"\b(?:last|past)\s*(\d{1,4})\s*days?\b", p)
    if m:
        n = int(m.group(1))
        return (today - timedelta(days=n)).isoformat(), today.isoformat(), m.group(0)

    # "last quarter" (calendar quarter)
    if "last quarter" in p:
        # Previous calendar quarter
        cur_q = (today.month - 1) // 3 + 1
        if cur_q == 1:
            start_month, start_year = 10, today.year - 1
        else:
            start_month, start_year = (cur_q - 2) * 3 + 1, today.year
        start = date(start_year, start_month, 1)
        if start_month == 10:
            end = date(start_year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(start_year, start_month + 3, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), "last quarter"

    # "this quarter"
    if "this quarter" in p or "current quarter" in p:
        cur_q = (today.month - 1) // 3 + 1
        start_month = (cur_q - 1) * 3 + 1
        start = date(today.year, start_month, 1)
        if start_month == 10:
            end = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(today.year, start_month + 3, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), "this quarter"

    # "last month"
    if "last month" in p or "previous month" in p:
        first_this = date(today.year, today.month, 1)
        end = first_this - timedelta(days=1)
        start = date(end.year, end.month, 1)
        return start.isoformat(), end.isoformat(), "last month"

    # "this month" / MTD
    if "this month" in p or "mtd" in p or "month to date" in p:
        return date(today.year, today.month, 1).isoformat(), today.isoformat(), "this month"

    # "YTD" / "year to date"
    if "ytd" in p or "year to date" in p:
        return date(today.year, 1, 1).isoformat(), today.isoformat(), "year to date"

    # Explicit calendar year "year 2024" / "calendar year 2024" / "in 2024"
    m = _re.search(r"\b(?:calendar\s*)?year\s*(\d{4})\b", p)
    if m:
        y = int(m.group(1))
        return f"{y}-01-01", f"{y}-12-31", m.group(0)

    return None


def _inject_resolved_date_filter(qdict: dict, prompt: str, model) -> None:
    """If the prompt contains a recognisable date phrase, inject a between-filter
    on the primary table's first date field.

    Drops EVERY existing between-op filter (regardless of table) and every
    filter that references the dim_date / date table, since once we've resolved
    the user's date range server-side, those alternative filters are either
    redundant or wrong-tabled (planner often picks `date.calendar_year` and
    triggers a "no join path" error).
    """
    resolved = _resolve_date_phrase(prompt)
    if not resolved:
        return
    start, end, _phrase = resolved
    prim = model.table(qdict.get("primary_table"))
    if not prim or not prim.dates:
        return
    date_field = prim.dates[0].key
    table_key = qdict["primary_table"]
    # Drop everything that's either:
    #  • a between-op filter (we're replacing it with the canonical resolved range), OR
    #  • a filter on the `date` / dim_date table (which would require an unavailable JOIN).
    qdict["filters"] = [
        f for f in (qdict.get("filters") or [])
        if f.get("op") not in ("between", "in_range")
        and (f.get("table") or "").lower() not in ("date", "dim_date", "gold_dim_date")
    ]
    qdict["filters"].append({
        "table": table_key,
        "field": date_field,
        "op": "between",
        "value": [start, end],
    })


def _inject_dashboard_scope(qdict: dict, dashboard_id: Optional[str], model) -> None:
    """When a dashboard_id is provided, look up that view's hierarchy_selections
    and date_range, resolve them, and AND them into qdict.filters. This means
    asking 'top campaigns' from a Pediatrics-scoped dashboard returns only
    Pediatrics campaigns — no extra prompt-engineering required.
    """
    if not dashboard_id:
        return
    # Read the view spec (and the hierarchies + selections it references) from
    # the JSON stores. We import locally to avoid a top-level circular import
    # (views_api lives in another module).
    try:
        from views_api import _load as _views_load
        from hierarchies_api import _load_json, HIERARCHIES_PATH
    except Exception:
        return
    try:
        views = _views_load().get("views", [])
    except Exception:
        return
    view = next((v for v in views if v.get("id") == dashboard_id), None)
    if not view:
        return
    # Hierarchy selections → IN filter on the hierarchy's filter_table/field
    sel_map = view.get("hierarchy_selections") or {}
    if sel_map:
        try:
            hierarchies = _load_json(HIERARCHIES_PATH, {"hierarchies": []}).get("hierarchies", [])
        except Exception:
            hierarchies = []
        prim_table = qdict.get("primary_table")
        for hid, ids in sel_map.items():
            if not ids:
                continue
            h = next((x for x in hierarchies if x.get("id") == hid), None)
            if not h or h.get("filter_table") != prim_table:
                continue
            # Resolve selected node IDs to the union of match_values (cascading)
            nodes = h.get("nodes") or []
            by_parent: dict = {}
            for n in nodes:
                by_parent.setdefault(n.get("parent_id"), []).append(n)
            seen: set = set()
            values: list[str] = []
            def collect(nid):
                if nid in seen: return
                seen.add(nid)
                node = next((n for n in nodes if n.get("id") == nid), None)
                if not node: return
                for v in (node.get("match_values") or []):
                    if v not in values:
                        values.append(v)
                for child in by_parent.get(nid, []):
                    collect(child.get("id"))
            for nid in ids:
                collect(nid)
            if values:
                qdict.setdefault("filters", []).append({
                    "table": h.get("filter_table"),
                    "field": h.get("filter_field"),
                    "op": "in",
                    "value": values,
                })
    # Date range → between filter on the primary table's first date field
    dr = view.get("date_range") or {}
    if dr.get("start") and dr.get("end"):
        prim_key = qdict.get("primary_table")
        prim = model.table(prim_key) if prim_key else None
        if prim and prim.dates:
            df = prim.dates[0].key
            # Drop any existing between filter to avoid conflict
            qdict["filters"] = [
                f for f in (qdict.get("filters") or [])
                if f.get("op") not in ("between", "in_range")
            ]
            qdict["filters"].append({
                "table": prim_key,
                "field": df,
                "op": "between",
                "value": [dr["start"], dr["end"]],
            })


def _build_qdict(model, spec, prompt, dashboard_id):
    """Turn a planner spec into a validated qdict + chart_type/title, applying the
    same server-side defensive fixes used on the first pass. Reused by the
    self-correction retry so the recovered query gets identical treatment."""
    chart_type = spec.get("chart_type", "table")
    title = spec.get("title") or prompt
    qdict = {
        "primary_table": spec["primary_table"],
        "dimensions": spec.get("dimensions", []) if chart_type != "kpi" else [],
        "metrics": spec.get("metrics", []),
        "filters": spec.get("filters", []),
        "limit": 50,
    }
    _inject_resolved_date_filter(qdict, prompt, model)
    _inject_dashboard_scope(qdict, dashboard_id, model)
    prim_table = model.table(spec["primary_table"])
    if prim_table:
        prim_fields = {f.key for f in prim_table.dimensions + prim_table.metrics + prim_table.dates}
        for bucket in ("dimensions", "metrics", "filters"):
            for entry in qdict[bucket]:
                if entry.get("table") != spec["primary_table"] and entry.get("field") in prim_fields:
                    entry["table"] = spec["primary_table"]
    if not qdict["dimensions"] and not qdict["metrics"]:
        prim = model.table(spec["primary_table"])
        if prim and prim.metrics:
            qdict["metrics"] = [{"table": spec["primary_table"], "field": prim.metrics[0].key}]
            if chart_type == "table":
                chart_type = "kpi"
    if qdict["dimensions"] and qdict["metrics"]:
        qdict["order_by"] = [{"field": qdict["metrics"][0]["field"], "direction": "desc"}]
    return qdict, chart_type, title


def _field_catalog(model, table_key):
    """Human-readable list of the real fields on a table, for the fix prompt."""
    t = model.table(table_key)
    if not t:
        return f"(unknown table '{table_key}')"
    dims = [f.key for f in t.dimensions]
    dates = [f.key for f in t.dates]
    mets = [f.key for f in t.metrics]
    joins = []
    try:
        for other in (model.tables.values() if hasattr(model, "tables") else []):
            if other.key != table_key and model.join_path(table_key, other.key):
                joins.append(other.key)
    except Exception:
        pass
    out = f"primary_table '{table_key}':\n  dimensions: {dims}\n  dates: {dates}\n  metrics: {mets}"
    if joins:
        out += f"\n  joinable tables: {joins[:12]}"
    return out


async def _planner_retry(client, model, question, spec, err):
    """Agentic self-correction: show the model the exact build error + the real
    field catalog and ask it to return a corrected spec. Uses a stronger model
    since this is a rare, quality-critical fix path. Returns a spec dict or None."""
    try:
        prim = spec.get("primary_table")
        catalog = _field_catalog(model, prim)
        fix_model = os.getenv("JARVIS_PLANNER_FIX_MODEL", "claude-sonnet-4-6")
        system = (
            "You fix a failed structured-data query. Return ONLY a JSON object with this exact "
            "schema and nothing else: {\"primary_table\": str, \"dimensions\": [{\"table\":str,\"field\":str}], "
            "\"metrics\": [{\"table\":str,\"field\":str}], \"filters\": [{\"table\":str,\"field\":str,\"op\":str,\"value\":any}], "
            "\"chart_type\": str, \"title\": str}. Every field MUST exist on its table per the catalog. "
            "Prefer fields on the primary table. Do not invent field names."
        )
        msg = (
            f"User question: {question}\n\n"
            f"Your previous query FAILED to build.\n"
            f"Previous spec: {json.dumps(spec)}\n"
            f"Build error: {err}\n\n"
            f"Real field catalog:\n{catalog}\n\n"
            "Return the corrected JSON spec. Keep it minimal and valid."
        )
        resp = await client.messages.create(
            model=fix_model, max_tokens=600, system=system,
            messages=[{"role": "user", "content": msg}],
        )
        fixed = _extract_json(resp.content[0].text if resp.content else "")
        return fixed if (fixed and fixed.get("primary_table")) else None
    except Exception as e:
        log.warning(f"planner self-correction failed: {e}")
        return None


async def _planner_retry_empty(client, model, question, spec, qdict):
    """Result-grounded self-correction: the query built and ran fine but returned
    ZERO rows — almost always an over-restrictive filter or a wrong/served date
    window. Ask the stronger model to relax or fix the offending filter(s) and
    return a corrected spec. Returns a spec dict or None. Best-effort."""
    try:
        prim = spec.get("primary_table")
        catalog = _field_catalog(model, prim)
        filters = (qdict or {}).get("filters", [])
        fix_model = os.getenv("JARVIS_PLANNER_FIX_MODEL", "claude-sonnet-4-6")
        system = (
            "You fix a structured-data query that returned ZERO rows. The query was valid but "
            "too restrictive. Return ONLY a JSON object with this exact schema and nothing else: "
            "{\"primary_table\": str, \"dimensions\": [{\"table\":str,\"field\":str}], "
            "\"metrics\": [{\"table\":str,\"field\":str}], \"filters\": [{\"table\":str,\"field\":str,\"op\":str,\"value\":any}], "
            "\"chart_type\": str, \"title\": str}. Diagnose the most likely cause of the empty result "
            "(an over-narrow value filter, a mistyped category, or a date window with no data) and "
            "relax or correct it — e.g. drop a doubtful equality filter, widen the date range, or fix a "
            "misspelled category value. Keep the dimensions/metrics the user clearly asked for. Every "
            "field MUST exist on its table per the catalog. Do not invent field names."
        )
        msg = (
            f"User question: {question}\n\n"
            f"This spec built and ran but returned 0 rows:\n{json.dumps(spec)}\n\n"
            f"Applied filters: {json.dumps(filters)}\n\n"
            f"Real field catalog:\n{catalog}\n\n"
            "Return a corrected JSON spec that is most likely to return the data the user wanted."
        )
        resp = await client.messages.create(
            model=fix_model, max_tokens=600, system=system,
            messages=[{"role": "user", "content": msg}],
        )
        fixed = _extract_json(resp.content[0].text if resp.content else "")
        return fixed if (fixed and fixed.get("primary_table")) else None
    except Exception as e:
        log.warning(f"empty-result self-correction failed: {e}")
        return None


async def _ktx_hint(prompt: str) -> str:
    """Leverage KTX's canonical semantic index to ground the planner. Best-effort:
    short-timeout, silent on any failure (KTX not installed / slow / no match)."""
    if os.getenv("JARVIS_USE_KTX_CONTEXT", "1") == "0":
        return ""
    try:
        import ktx_client
        loop = asyncio.get_running_loop()
        items = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ktx_client.search_context(prompt, 4)),
            timeout=float(os.getenv("JARVIS_KTX_TIMEOUT", "4")),
        )
        if not items:
            return ""
        lines = ["\n\nKTX canonical matches (authoritative semantic sources — prefer these when they fit the question):"]
        for it in items[:4]:
            nm = it.get("name") or "?"
            desc = (it.get("description") or "").strip().replace("\n", " ")[:120]
            meta = []
            if it.get("measures"):
                meta.append(f"{it['measures']} measures")
            if it.get("joins"):
                meta.append(f"{it['joins']} joins")
            lines.append(f"- {nm}" + (f" — {desc}" if desc else "") + (f" ({', '.join(meta)})" if meta else ""))
        return "\n".join(lines)
    except Exception:
        return ""


_EXPERIMENT_HINTS = (
    "did the", "did our", "did my", "impact of", "effect of", "incremental",
    "lift from", "vs control", "versus control", "control group", "test and learn",
    "test vs", "causal", "before and after", "before vs after", "did the promo",
    "did the campaign", "roll out", "rollout worth", "was it worth", "attributable to",
    "move the needle", "drive more", "caused by", "because of the",
)


def _looks_like_experiment(prompt: str) -> bool:
    """Cheap intent detector for causal / test-vs-control questions."""
    p = (prompt or "").lower()
    if any(h in p for h in _EXPERIMENT_HINTS):
        return True
    # "did X increase/change Y" pattern
    if p.startswith("did ") and any(w in p for w in ("increase", "change", "improve", "boost", "raise", "grow", "work")):
        return True
    return False


_EXPERIMENT_PLANNER_SYSTEM = (
    "You extract a controlled experiment (test vs control) specification from a business question, "
    "using ONLY the provided data model field keys. Respond with ONLY JSON.\n"
    "If the question is NOT about measuring the causal impact of an intervention via a test-vs-control "
    "comparison, return {\"is_experiment\": false}.\n"
    "Otherwise return: {\"is_experiment\": true, \"primary_table\": str, \"metric_field\": str, "
    "\"unit_field\": str, \"date_field\": str, \"pre_start\": \"YYYY-MM-DD\", \"pre_end\": \"YYYY-MM-DD\", "
    "\"post_start\": \"YYYY-MM-DD\", \"post_end\": \"YYYY-MM-DD\", \"group_field\": str|null, "
    "\"test_values\": [..]|null, \"control_values\": [..]|null, \"metric_name\": str, "
    "\"missing\": [..]}.\n"
    "unit_field = the dimension identifying an experimental unit (store, region, customer, campaign). "
    "group_field + test_values/control_values define which units are test vs control. "
    "Put any pieces you cannot determine into \"missing\" (e.g. 'control group', 'pre/post dates'). "
    "Assume today is the current real date when resolving relative windows."
)


def _experiment_spec_complete(spec: dict) -> bool:
    req = ("primary_table", "metric_field", "unit_field", "date_field",
           "pre_start", "pre_end", "post_start", "post_end")
    if not all(spec.get(k) for k in req):
        return False
    return bool(spec.get("group_field") and spec.get("test_values") and spec.get("control_values"))


async def _try_experiment(model, prompt, key):
    """Detect + plan + auto-run a Test & Learn experiment. Returns a response dict
    to short-circuit /ask, or None to fall back to the normal query planner.
    Best-effort and fully defensive — any failure returns None."""
    if os.getenv("JARVIS_EXPERIMENT_DETECT", "1") == "0":
        return None
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        plan_model = os.getenv("JARVIS_EXPERIMENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=plan_model, max_tokens=700, system=_EXPERIMENT_PLANNER_SYSTEM,
            messages=[{"role": "user", "content": _ask_context(model, prompt) + "\n\nQuestion: " + prompt}],
        )
        spec = _extract_json(resp.content[0].text if resp.content else "") or {}
    except Exception as e:
        log.warning(f"experiment planner failed: {e}")
        return None
    if not spec.get("is_experiment"):
        return None

    # Not enough to auto-run → route the user into the Test & Learn lab with hints.
    if not _experiment_spec_complete(spec):
        missing = spec.get("missing") or []
        if not missing:
            for k, lbl in (("group_field", "test vs control groups"), ("metric_field", "the metric"),
                           ("pre_start", "before/after date windows")):
                if not spec.get(k):
                    missing.append(lbl)
        return {
            "ok": True, "experiment_intent": True, "route": "experiments",
            "spec": spec, "missing": missing,
            "message": ("This looks like a causal 'did it work?' question — best answered with a "
                        "test-vs-control experiment. Open the Test & Learn lab and I'll pre-fill "
                        "what I could infer; you supply: " + (", ".join(missing) if missing else "the cohorts and dates") + "."),
        }

    # Auto-run against the semantic layer.
    try:
        import experiments_api as _exp
        req = _exp.ExperimentFromDataRequest(
            primary_table=spec["primary_table"], metric_field=spec["metric_field"],
            unit_field=spec["unit_field"], date_field=spec["date_field"],
            pre_start=spec["pre_start"], pre_end=spec["pre_end"],
            post_start=spec["post_start"], post_end=spec["post_end"],
            group_field=spec.get("group_field"), test_values=spec.get("test_values"),
            control_values=spec.get("control_values"),
            metric_name=spec.get("metric_name") or spec["metric_field"], robust=True,
        )
        readout = await _exp._run_experiment_from_data(req)
        if readout.get("error"):
            return {"ok": True, "experiment_intent": True, "route": "experiments",
                    "spec": spec, "message": "I planned the experiment but couldn't run it: "
                    + readout["error"] + " Open the Test & Learn lab to adjust cohorts/dates."}
        return {"ok": True, "mode": "experiment", "experiment": readout, "spec": spec}
    except Exception as e:
        log.warning(f"experiment auto-run failed: {e}")
        return None


@router.post("/ask")
async def ask(body: AskRequest):
    import time as _t
    _t_start = _t.time()
    if not body.prompt.strip():
        return _err(400, "empty prompt", "Type a question.")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return _err(503, "AI not configured", "ANTHROPIC_API_KEY is not set on the server.")
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    # 0. Experiment detection: causal 'did it work?' questions are answered with a
    # test-vs-control experiment (Test & Learn), not a plain aggregate query.
    if not body.no_clarify and not body.dashboard_id and _looks_like_experiment(body.prompt):
        _exp_resp = await _try_experiment(model, body.prompt, key)
        if _exp_resp is not None:
            return _exp_resp

    # 1. Ask Claude for a structured query — include prior conversation if any
    # Splice in Knowledge rules: "always"-mode rules into system, top "auto"
    # rules into the user message (relevant-only).
    try:
        from knowledge_api import always_rules_text, auto_rules_for_question
        always_blk = always_rules_text()
        auto_blk = auto_rules_for_question(body.prompt)
    except Exception:
        always_blk, auto_blk = "", ""

    # Ground the planner in KTX's canonical semantic index (best-effort, tight timeout).
    ktx_blk = await _ktx_hint(body.prompt)

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        history_blk = _history_for_prompt(body.history or [])
        _clarify_on = (os.getenv("JARVIS_CLARIFY", "1") != "0") and not body.no_clarify
        system_prompt = _ASK_SYSTEM + always_blk + (_ASK_CLARIFY_ADDENDUM if _clarify_on else "")
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            system=system_prompt,
            messages=[{"role": "user", "content": _ask_context(model, body.prompt) + auto_blk + ktx_blk + "\n\n" + history_blk + "Question: " + body.prompt}],
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:
        return _err(502, "AI request failed", str(e))

    spec = _extract_json(raw)
    # Clarifying-question loop: when the planner is genuinely torn it returns a
    # clarify object instead of a spec. Surface it to the user (no query runs);
    # the follow-up call arrives with no_clarify=True and the chosen answer folded
    # into the prompt, so the planner then commits.
    if _clarify_on and isinstance(spec, dict) and spec.get("clarify") and not spec.get("primary_table"):
        clr = spec["clarify"] or {}
        opts = [str(o) for o in (clr.get("options") or []) if str(o).strip()][:4]
        if clr.get("question") and len(opts) >= 2:
            return {
                "ok": True,
                "needs_clarification": True,
                "clarify": {
                    "question": str(clr.get("question")),
                    "options": opts,
                    "kind": clr.get("kind") or "metric",
                },
            }
    if not spec or not spec.get("primary_table"):
        return _err(422, "could not interpret", "The AI couldn't map that to your data. Try rephrasing.", )

    chart_type = spec.get("chart_type", "table")
    title = spec.get("title") or body.prompt
    qdict = {
        "primary_table": spec["primary_table"],
        "dimensions": spec.get("dimensions", []) if chart_type != "kpi" else [],
        "metrics": spec.get("metrics", []),
        "filters": spec.get("filters", []),
        "limit": 50,
    }
    # Server-side date-phrase resolution: detect "FY YYYY", "last N days",
    # "last quarter", etc. in the prompt and inject a self-contained date
    # filter on the primary table. The planner can't forget what the server
    # already computed → no more wrong-magnitude answers from missing date scopes.
    _inject_resolved_date_filter(qdict, body.prompt, model)
    # Inherit hierarchy + date scope from the dashboard the user is asking from.
    # (No-op when body.dashboard_id is None.)
    _inject_dashboard_scope(qdict, body.dashboard_id, model)
    # Defensive: when the planner references a field on a different table than
    # the primary, but the SAME field exists on the primary table, rewrite to
    # use primary. This avoids "No join path" errors for self-contained marts
    # (e.g. combined_campaign_stats_dashboard has service_line, but planner
    # asked for `campaigns.service_line` from habit).
    prim_table = model.table(spec["primary_table"])
    if prim_table:
        prim_fields = {f.key for f in prim_table.dimensions + prim_table.metrics + prim_table.dates}
        for bucket in ("dimensions", "metrics", "filters"):
            for entry in qdict[bucket]:
                if entry.get("table") != spec["primary_table"] and entry.get("field") in prim_fields:
                    entry["table"] = spec["primary_table"]
    # Defensive fallback: when Haiku omits metrics on a KPI-style question
    # ("What was total revenue?"), grab the first metric on the primary table
    # so we don't 422 with "no dimensions or metrics — nothing to select".
    if not qdict["dimensions"] and not qdict["metrics"]:
        prim = model.table(spec["primary_table"])
        if prim and prim.metrics:
            qdict["metrics"] = [{"table": spec["primary_table"], "field": prim.metrics[0].key}]
            # If we're auto-filling, this is implicitly a KPI question
            if chart_type == "table":
                chart_type = "kpi"
    if qdict["dimensions"] and qdict["metrics"]:
        qdict["order_by"] = [{"field": qdict["metrics"][0]["field"], "direction": "desc"}]

    # 2. Build + run (validates against the real layer)
    sql = None
    try:
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
    except (QueryBuildError, ResolverError, KeyError) as e:
        # Agentic self-correction: feed the exact error + the real field catalog
        # back to a stronger model and retry once before giving up.
        fixed = await _planner_retry(client, model, body.prompt, spec, str(e))
        if fixed:
            try:
                spec = fixed
                qdict, chart_type, title = _build_qdict(model, spec, body.prompt, body.dashboard_id)
                sq = StructuredQuery.from_dict(qdict)
                sql = build_sql(sq, model)
            except Exception as e2:
                e = e2
        if sql is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "could not build query",
                    "detail": f"{e}",
                    "planner_spec": spec,
                    "interpreted": qdict,
                    "self_corrected": bool(fixed),
                },
            )
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "query failed", str(e))

    # Result-grounded self-correction: a valid query that returns ZERO rows is
    # usually an over-restrictive filter or an empty date window. Try once to
    # relax/fix the spec and re-run; only adopt the retry if it yields rows.
    _empty_corrected = False
    if getattr(result, "row_count", None) == 0 and qdict.get("filters") \
            and os.getenv("JARVIS_EMPTY_RETRY", "1") != "0":
        fixed2 = await _planner_retry_empty(client, model, body.prompt, spec, qdict)
        if fixed2:
            try:
                qdict2, chart_type2, title2 = _build_qdict(model, fixed2, body.prompt, body.dashboard_id)
                sq2 = StructuredQuery.from_dict(qdict2)
                sql2 = build_sql(sq2, model)
                result2 = await asyncio.get_running_loop().run_in_executor(None, run_query, sql2)
                if getattr(result2, "row_count", 0) > 0:
                    spec, qdict, chart_type, title, sql, result = fixed2, qdict2, chart_type2, title2, sql2, result2
                    _empty_corrected = True
            except Exception as e:
                log.warning(f"empty-result retry rebuild failed: {e}")

    formats = [_metric_format(model, m["table"], m["field"]) for m in qdict["metrics"]]
    widget = {
        "type": chart_type, "title": title, "query": qdict, "metricFormats": formats,
        "layout": {"w": 6 if chart_type != "kpi" else 3},
    }

    # 3. Strategic brief — ask Claude to interpret the actual data.
    brief = await _generate_brief(client, body.prompt, result.columns, result.rows, formats)

    # Surface BOTH the planner's raw intent (spec) and what actually ran (qdict)
    # so callers can verify server-side mutations (e.g. date-phrase injection).
    _t_elapsed = round(_t.time() - _t_start, 1)
    return {
        "ok": True, "widget": widget,
        "interpreted": qdict,            # what actually ran (post-injection / rewrite)
        "planner_spec": spec,            # raw planner output, pre-mutation
        "brief": brief,
        "columns": result.columns, "rows": result.rows, "row_count": result.row_count,
        "time_elapsed": _t_elapsed,      # seconds, for the bottom toolbar
        "engine": "BigQuery via curated semantic layer",
        "empty_corrected": _empty_corrected,  # true if a 0-row result was auto-relaxed
    }


# ─── Strategic brief from the returned rows ──────────────────────────
# Kaelio-style brief: ONE narrative body (markdown essay with embedded tables)
# plus a conversational note and 3 suggested follow-ups. That's it.
_BRIEF_SYSTEM = (
    "You are a senior marketing analyst at Orlando Health writing a strategic research brief "
    "based on actual data the user asked for. Your output is a flowing narrative, NOT a "
    "bulleted brief — think Kaelio / Bain memo, not a slide deck.\n\n"
    "Produce a JSON object — no prose, no code fences:\n"
    "{\n"
    '  "headline": "<concise H1, 5-12 words, what the chart shows. E.g. \\"Ad Spend by Service Line — Last 90 Days\\">",\n'
    '  "period_summary": "<short scope subtitle. E.g. \\"Period: March 6 – June 3, 2026 | 23 classified service lines\\">",\n'
    '  "body_md": "<THE MAIN BRIEF — MARKDOWN BODY of 250-500 words. Multiple paragraphs synthesizing what the data shows. Embed ONE markdown table when listing 5+ items by value. Use **bold** for service-line names, dollar amounts, and decisive phrases. Structure: open with the dominant finding (largest number, biggest gap), then present the supporting data (often as an inline table), then synthesize patterns (\\"X is a clear priority because A and B together…\\"). End by naming what the analysis implies for action.>",\n'
    '  "note_md": "<OPTIONAL conversational caveat in markdown. Use Kaelio\'s natural \\"A note on…\\" + \\"Would you like me to…\\" pattern when there is data ambiguity, missing categorization, or a follow-up worth offering. Empty string if no caveat.>",\n'
    '  "next_questions": ["<follow-up 1 — phrased as a question the user can click to ask>", "<follow-up 2>", "<follow-up 3>"]\n'
    "}\n\n"
    "RULES — these matter:\n"
    "• body_md is the WHOLE brief. It is a NARRATIVE, not bullets. Do not write a header inside it (no leading \"## Key Findings\") — the wrapper supplies that.\n"
    "• Use **bold** for entity names (e.g. **Cardiovascular Surgery**) and dollar amounts (e.g. **$1.1M**, **28%**).\n"
    "• Embed a markdown table when listing 5+ items by value. Format: | Service Line | Spend |\\n|---|---|\\n| Foo | $1,234 |\\n. Right-justify numeric columns naturally by writing them right.\n"
    "• Don't write multiple tables. One per brief is impactful; multiple is noise.\n"
    "• Synthesize patterns: name clusters (Oncology = Breast Cancer + Lung Cancer; Musculoskeletal = Joint Replacement + Sports Medicine + Vascular Surgery), totals, ratios. This is what makes the brief feel sophisticated.\n"
    "• note_md is conversational. Examples:\n"
    "  - \"A note on the unclassified bucket: The **$1.08M** in 'Unclassified / Brand' spend likely contains meaningful service lines (Bariatrics, Urology) that aren't tagged with an entity_name. Would you like me to parse service-line names directly from campaign_name to give you a more complete picture?\"\n"
    "  - \"One caveat: this excludes paid social spend. Should I pull Facebook and TikTok ad spend together with this view?\"\n"
    "• next_questions are the user's most likely next 3 questions. Phrase them naturally as questions.\n"
    "• Cite real numbers from the data. Never invent. If data is empty, set headline to a clear 'No data' message and explain in note_md."
)


def _format_value(v, fmt):
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        try:
            if fmt == "currency":
                return f"${v:,.2f}"
            if fmt == "percent":
                return f"{v*100:.2f}%"
            if fmt == "ratio":
                return f"{v:.2f}x"
            return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
        except Exception:
            return str(v)
    return str(v)


def _rows_for_prompt(columns, rows, formats, max_rows: int = 40) -> str:
    """Render rows as a compact table for the LLM, with metrics formatted."""
    if not rows:
        return "(no rows returned)"
    n_dims = max(0, len(columns) - len(formats))
    head = " | ".join(columns)
    out = [head, "-" * len(head)]
    for r in rows[:max_rows]:
        cells = []
        for i, v in enumerate(r):
            fmt = formats[i - n_dims] if i >= n_dims and (i - n_dims) < len(formats) else None
            cells.append(_format_value(v, fmt))
        out.append(" | ".join(cells))
    if len(rows) > max_rows:
        out.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════
#  AUTO-INSIGHTS — Claude proposes high-value questions about the
#  curated layer, server answers each via the existing /ask pipeline.
#  Cached for an hour so the Ask Home doesn't re-spend on every visit.
# ═══════════════════════════════════════════════════════════════════

_AUTO_INSIGHTS_SYSTEM = (
    "You are a senior marketing analyst surveying a curated BI semantic layer. "
    "Propose 4 to 6 SHORT business questions that would be most insightful for a marketing manager "
    "to see answered RIGHT NOW given this data model. Pick a diverse mix: at least one trend over time, "
    "one breakdown by service line / category, one ratio/efficiency metric, and one outlier-finder. "
    "Each question should be answerable from a single query over the curated layer.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"questions": ["<q1>","<q2>","<q3>","<q4>"]}\n'
    "Rules: questions must be in plain English (no SQL, no field keys). Keep each under 12 words. "
    "Prefer questions whose answer is a concrete number or short ranked list (not vague \"how is X doing\")."
)


async def _propose_insight_questions(client, model) -> list[str]:
    """Ask Claude to propose 4-6 insightful questions about the curated layer."""
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_AUTO_INSIGHTS_SYSTEM,
            messages=[{"role": "user", "content": _ask_context(model)}],
        )
        raw = resp.content[0].text if resp.content else ""
        spec = _extract_json(raw) or {}
        qs = spec.get("questions") or []
        return [q for q in qs if isinstance(q, str) and q.strip()][:6]
    except Exception as e:
        log.warning(f"auto-insight questions failed: {e}")
        return []


async def _answer_one(client, model, question: str) -> Optional[dict]:
    """Run the same NL→query→brief pipeline /ask uses, but in-process. Returns a
    {question, widget, brief, columns, rows} dict, or None on any failure."""
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            system=_ASK_SYSTEM,
            messages=[{"role": "user", "content": _ask_context(model, question) + "\n\nQuestion: " + question}],
        )
        spec = _extract_json(resp.content[0].text if resp.content else "")
        if not spec or not spec.get("primary_table"):
            return None
        chart_type = spec.get("chart_type", "table")
        title = spec.get("title") or question
        qdict = {
            "primary_table": spec["primary_table"],
            "dimensions": spec.get("dimensions", []) if chart_type != "kpi" else [],
            "metrics": spec.get("metrics", []),
            "filters": spec.get("filters", []),
            "limit": 50,
        }
        if qdict["dimensions"] and qdict["metrics"]:
            qdict["order_by"] = [{"field": qdict["metrics"][0]["field"], "direction": "desc"}]
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
        formats = [_metric_format(model, m["table"], m["field"]) for m in qdict["metrics"]]
        brief = await _generate_brief(client, question, result.columns, result.rows, formats)
        widget = {
            "type": chart_type, "title": title, "query": qdict, "metricFormats": formats,
            "layout": {"w": 6 if chart_type != "kpi" else 3},
        }
        return {
            "question": question, "widget": widget, "brief": brief,
            "columns": result.columns, "rows": result.rows, "row_count": result.row_count,
        }
    except Exception as e:
        log.warning(f"auto-insight answer for '{question}' failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
#  RESEARCH MODE — Kaelio-style multi-section deep brief
#  Plans 3-5 sub-questions, runs each as its own /ask, then synthesizes
#  a Bain-style memo: H1 + intro + N chart-bearing sections + Verdict
#  block + clickable follow-up chips. This is what makes the brief feel
#  comprehensive instead of a single-chart blurb.
# ═══════════════════════════════════════════════════════════════════

_RESEARCH_PLANNER_SYSTEM = (
    "You are a senior marketing analyst at Orlando Health. The user has asked a research question "
    "that deserves a multi-angle answer — like a Bain memo with multiple charts.\n\n"
    "Plan 3 to 5 sub-questions that, together, build a comprehensive picture. Each sub-question must "
    "be answerable as a single chart/table from the curated semantic layer.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    "{\n"
    '  "final_headline": "<the H1 for the whole brief, 5-12 words. Punchy. E.g. \\"Best Performing Campaigns — Last 12 Months\\">",\n'
    '  "final_subtitle": "<period or scope, e.g. \\"Jun 2025 – Jun 2026\\". Empty if not applicable>",\n'
    '  "intro_md": "<2-3 sentence opening that frames the analysis. End with a colon if you list dimensions.>",\n'
    '  "sections": [\n'
    '     {"heading":"1. <Section Title — Punchy Insight>", "intent":"<plain English data question, ≤ 14 words>"},\n'
    '     {"heading":"2. ..."},\n'
    "     ...\n"
    "  ]\n"
    "}\n\n"
    "RULES:\n"
    "• Each section attacks a DIFFERENT angle (volume vs efficiency vs revenue vs trend vs outliers).\n"
    "• Headings follow Kaelio's pattern: \"1. Best by Lead Volume — Google Paid Search Dominates\".\n"
    "• intent must be a complete plain-English question. No section labels in intent.\n"
    "• Use 3 sections for narrow questions, 4-5 for broad ones (\"best\", \"how are we doing\", \"compare\").\n"
    "• Sections build narratively: baseline → cuts by dimension → synthesis.\n"
    "• Tailor section angles to the question. Examples:\n"
    "   - \"What's our best campaign?\" → by leads, by CTR vs benchmark, by revenue, combined ranking.\n"
    "   - \"How is paid search?\" → spend over time, by service line, vs other channels, top campaigns.\n"
    "   - \"Why are leads down?\" → trend, by channel, by service line, by source comparison vs prior period."
)


_RESEARCH_SYNTH_SYSTEM = (
    "You are writing the synthesis of a multi-part research brief. You've just been shown several "
    "sub-analyses; write the closing 'Verdict' block (numbered reasons) and a list of follow-up questions.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    "{\n"
    '  "verdict": {\n'
    '    "heading": "The Verdict: <punchy conclusion>",\n'
    '    "reasons_md": "1. **<bold reason 1>** — <plain explanation>\\n2. **<bold reason 2>** — ...\\n3. ...\\n4. ...",\n'
    '    "concern_md": "**One area of concern:** <single paragraph caveat>. Empty string if no concern."\n'
    "  },\n"
    '  "next_questions": ["<follow-up 1 — natural question phrasing>", "<2>", "<3>", "<4>"]\n'
    "}\n\n"
    "RULES:\n"
    "• reasons_md: 3-5 numbered reasons. Each starts with **bold label**, em-dash, plain explanation.\n"
    "• concern_md: ONE paragraph identifying a caveat, gap, or area underperforming. Empty if everything looks good.\n"
    "• next_questions: 3-4 clickable follow-ups that drill deeper. Phrase them as full natural-language questions, not labels."
)


class ResearchRequest(BaseModel):
    prompt: str
    history: Optional[list] = None
    dashboard_id: Optional[str] = None


async def _research_callout(client, heading: str, intent: str, columns, rows, formats) -> str:
    """Generate the bolded 'Best efficiency award'-style closing line for one section."""
    if not rows:
        return ""
    table_str = _rows_for_prompt(columns, rows, formats, max_rows=8)
    prompt = (
        f"Section heading: {heading}\n"
        f"Section question: {intent}\n"
        f"Section data (top rows):\n{table_str}\n\n"
        "Write ONE bolded callout line in markdown highlighting the single most interesting insight. "
        "Examples of the EXACT style:\n"
        "  • \"**Best efficiency award** goes to **Lake Mary FSED – ER (5969)** at just **$34/lead** — among the highest volume AND lowest cost per lead in the portfolio.\"\n"
        "  • \"**Standout:** Women's Institute – Obstetrics generates the **highest revenue per patient ($2,786)** in the top 5, driven by high-value OB procedures — quality over quantity.\"\n\n"
        "Return JUST the markdown line, no JSON, no preamble. Keep it under 35 words."
    )
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=180,
            messages=[{"role": "user", "content": prompt}],
        )
        line = (resp.content[0].text if resp.content else "").strip()
        # Strip stray quotes
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        return line
    except Exception as e:
        log.warning(f"section callout failed: {e}")
        return ""


async def _research_run_section(client, model, sp: dict, dashboard_id: Optional[str]) -> Optional[dict]:
    """Run one sub-question through the standard /ask pipeline and decorate with a callout line."""
    intent = (sp.get("intent") or "").strip()
    heading = (sp.get("heading") or "").strip()
    if not intent:
        return None
    try:
        sub_resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            system=_ASK_SYSTEM,
            messages=[{"role": "user", "content": _ask_context(model, intent) + "\n\nQuestion: " + intent}],
        )
        spec = _extract_json(sub_resp.content[0].text if sub_resp.content else "")
        if not spec or not spec.get("primary_table"):
            log.warning(f"research section '{heading}': planner returned no spec")
            return None
        chart_type = spec.get("chart_type", "table")
        title = spec.get("title") or intent
        qdict = {
            "primary_table": spec["primary_table"],
            "dimensions": spec.get("dimensions", []) if chart_type != "kpi" else [],
            "metrics": spec.get("metrics", []),
            "filters": spec.get("filters", []),
            "limit": 30,
        }
        _inject_resolved_date_filter(qdict, intent, model)
        _inject_dashboard_scope(qdict, dashboard_id, model)
        prim_table = model.table(spec["primary_table"])
        if prim_table:
            prim_fields = {f.key for f in prim_table.dimensions + prim_table.metrics + prim_table.dates}
            for bucket in ("dimensions", "metrics", "filters"):
                for entry in qdict[bucket]:
                    if entry.get("table") != spec["primary_table"] and entry.get("field") in prim_fields:
                        entry["table"] = spec["primary_table"]
        if not qdict["dimensions"] and not qdict["metrics"]:
            prim = model.table(spec["primary_table"])
            if prim and prim.metrics:
                qdict["metrics"] = [{"table": spec["primary_table"], "field": prim.metrics[0].key}]
                if chart_type == "table":
                    chart_type = "kpi"
        if qdict["dimensions"] and qdict["metrics"]:
            qdict["order_by"] = [{"field": qdict["metrics"][0]["field"], "direction": "desc"}]
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
        formats = [_metric_format(model, m["table"], m["field"]) for m in qdict["metrics"]]
        widget = {
            "type": chart_type, "title": title, "query": qdict, "metricFormats": formats,
            "layout": {"w": 12},
        }
        callout = await _research_callout(client, heading, intent, result.columns, result.rows, formats)
        # Derive a sensible noun for the row-count subtitle ("23 service lines analyzed").
        dim_label = None
        if qdict["dimensions"]:
            d0 = qdict["dimensions"][0]
            fld = d0.get("field") if isinstance(d0, dict) else None
            if fld:
                dim_label = str(fld).replace("_", " ")
        return {
            "heading": heading,
            "intent": intent,
            "widget": widget,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "dimension_label": dim_label,
            "callout_md": callout,
        }
    except Exception as e:
        log.warning(f"research section '{heading}' failed: {e}")
        return None


@router.post("/research")
async def research(body: ResearchRequest):
    """Multi-section research brief — Kaelio parity.

    Phases:
      1. Plan: ask Claude for 3-5 sub-questions covering different angles.
      2. Execute: run each sub-question through the /ask pipeline in parallel
         (capped at 3 concurrent to avoid hammering BQ).
      3. Synthesize: ask Claude for a Verdict block + follow-up chips
         grounded in the sub-answers it just produced.
    """
    import time as _t
    t_start = _t.time()
    if not body.prompt.strip():
        return _err(400, "empty prompt", "Type a question.")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return _err(503, "AI not configured", "ANTHROPIC_API_KEY is not set.")
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
    except Exception as e:
        return _err(502, "anthropic client failed", str(e))

    # Phase 1 — plan
    try:
        plan_resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            system=_RESEARCH_PLANNER_SYSTEM,
            messages=[{
                "role": "user",
                "content": _ask_context(model, body.prompt) + "\n\nResearch question: " + body.prompt,
            }],
        )
        plan = _extract_json(plan_resp.content[0].text if plan_resp.content else "")
    except Exception as e:
        return _err(502, "research planning failed", str(e))
    if not plan or not isinstance(plan.get("sections"), list) or not plan["sections"]:
        return _err(422, "could not plan research", "The AI couldn't decompose the question.")

    section_plans = plan["sections"][:5]

    # Phase 2 — execute (concurrent, capped at 3)
    sem = asyncio.Semaphore(3)
    async def _bounded(sp):
        async with sem:
            return await _research_run_section(client, model, sp, body.dashboard_id)
    sections_raw = await asyncio.gather(*[_bounded(sp) for sp in section_plans])
    sections = [s for s in sections_raw if s and s.get("columns")]
    if not sections:
        return _err(422, "research failed", "All sub-queries failed to return data.")

    # Phase 3 — synthesize verdict + next_questions
    summary_blocks = []
    for s in sections:
        formats = [_metric_format(model, m["table"], m["field"]) for m in s["widget"]["query"].get("metrics", [])]
        summary_blocks.append(
            f"Section: {s['heading']}\n"
            f"Question: {s['intent']}\n"
            f"Top rows:\n{_rows_for_prompt(s['columns'], s['rows'], formats, max_rows=8)}"
        )
    synth = {}
    try:
        synth_resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            system=_RESEARCH_SYNTH_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Original research question: {body.prompt}\n\n"
                    f"Findings from {len(sections)} sub-analyses:\n\n"
                    + "\n\n---\n\n".join(summary_blocks)
                ),
            }],
        )
        synth = _extract_json(synth_resp.content[0].text if synth_resp.content else "") or {}
    except Exception as e:
        log.warning(f"research synthesis failed: {e}")

    elapsed = round(_t.time() - t_start, 1)
    return {
        "ok": True,
        "research": {
            "headline": (plan.get("final_headline") or body.prompt)[:140],
            "subtitle": plan.get("final_subtitle") or "",
            "intro_md": plan.get("intro_md") or "",
            "sections": sections,
            "verdict": synth.get("verdict") or {},
            "next_questions": (synth.get("next_questions") or [])[:4],
            "tool_count": len(sections),
            "thinking_steps": 1,
            "time_elapsed": elapsed,
            "engine": "BigQuery via curated semantic layer",
        },
    }


# In-memory cache (process-local) — 1 hour TTL
_AI_CACHE = {"at": 0.0, "data": None}
_AI_TTL = 3600


@router.get("/auto_insights")
async def auto_insights(refresh: bool = False):
    """Discover and answer 4-6 insightful questions about the curated layer."""
    import time as _t
    if not refresh and _AI_CACHE["data"] and (_t.monotonic() - _AI_CACHE["at"]) < _AI_TTL:
        return _AI_CACHE["data"]

    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return _err(503, "AI not configured", "ANTHROPIC_API_KEY is not set.")
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
    except Exception as e:
        return _err(502, "AI client init failed", str(e))

    questions = await _propose_insight_questions(client, model)
    if not questions:
        return _err(502, "could not propose insights", "AI did not return questions.")

    # Answer them all in parallel
    answers = await asyncio.gather(*[_answer_one(client, model, q) for q in questions])
    insights = [a for a in answers if a]

    payload = {
        "ok": True, "generated_at": _t.time(),
        "count": len(insights), "questions": questions, "insights": insights,
    }
    _AI_CACHE["data"] = payload
    _AI_CACHE["at"] = _t.monotonic()
    return payload


# ═══════════════════════════════════════════════════════════════════
#  FULL-FUNNEL ATTRIBUTION v1 — vision stage from the North Star
#  Builds the canonical marketing funnel (spend → impressions → clicks
#  → conversions → revenue) by reading the curated semantic layer.
#  Only as good as what you've curated and published — exactly the
#  intended architecture.
# ═══════════════════════════════════════════════════════════════════

# Default marketing funnel — looks for these metric keys, in this order, on
# whichever table you point it at. Any present become a stage; rest are skipped.
DEFAULT_FUNNEL_STAGES = [
    {"key": "spend",              "label": "Spend",          "kind": "investment"},
    {"key": "impressions",        "label": "Impressions",    "kind": "reach"},
    {"key": "active_view_impressions", "label": "Viewable",  "kind": "reach"},
    {"key": "clicks",             "label": "Clicks",         "kind": "engagement"},
    {"key": "interactions",       "label": "Interactions",   "kind": "engagement"},
    {"key": "sessions",           "label": "Sessions",       "kind": "engagement"},
    {"key": "engaged_sessions",   "label": "Engaged Sessions","kind": "engagement"},
    {"key": "conversions",        "label": "Conversions",    "kind": "outcome"},
    {"key": "conversion_value",   "label": "Revenue",        "kind": "outcome"},
]


def _funnel_date_filter(model, table_key: str, period: str) -> Optional[dict]:
    """Build a between-filter on the table's primary date field for a period."""
    today = __import__("datetime").date.today()
    ranges = {
        "day":     (today, today),
        "week":    (today - __import__("datetime").timedelta(days=today.weekday()), today),
        "month":   (today.replace(day=1), today),
        "quarter": (today.replace(month=((today.month-1)//3)*3+1, day=1), today),
        "year":    (today.replace(month=1, day=1), today),
        "ytd":     (today.replace(month=1, day=1), today),
        "all":     None,
    }
    rng = ranges.get(period)
    if not rng:
        return None
    t = model.table(table_key)
    if not t or not t.dates:
        return None
    # Prefer field named 'date'; else first date field
    df = next((f.key for f in t.dates if f.key.lower() == "date"), t.dates[0].key)
    return {"table": table_key, "field": df, "op": "between",
            "value": [rng[0].isoformat(), rng[1].isoformat()]}


@router.get("/funnel")
async def funnel(
    table: str = "ad_performance",
    group_by_table: Optional[str] = None,
    group_by_field: Optional[str] = None,
    period: str = "month",
    draft: bool = False,
):
    """Build a marketing funnel from the curated layer.

    Stages = any of {spend, impressions, clicks, sessions, conversions, conversion_value}
    that exist as metrics on the `table`. Optionally groups by a dimension on
    another curated table (e.g. campaigns.service_line) using the engine's joins.
    """
    try:
        model = _loadm(draft=draft)
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    t = model.table(table)
    if not t:
        return _err(404, "table not found", f"No curated table '{table}'")

    # Native metrics + calculated metrics available on this table
    native = {f.key: f for f in t.metrics}
    calc = {m.key: m for m in model.calc_metrics_for(table)}
    available_metric_keys = set(native.keys()) | set(calc.keys())

    stages_present = [s for s in DEFAULT_FUNNEL_STAGES if s["key"] in available_metric_keys]
    if not stages_present:
        return _err(400, "no funnel metrics", f"None of the standard funnel metrics found on '{table}'.")

    # Build the structured query: all funnel metrics + optional dim, scoped to period.
    qdict = {
        "primary_table": table,
        "metrics": [{"table": table, "field": s["key"]} for s in stages_present],
        "filters": [],
        "limit": 100,
    }
    date_flt = _funnel_date_filter(model, table, period)
    if date_flt:
        qdict["filters"].append(date_flt)

    if group_by_table and group_by_field:
        if not model.table(group_by_table):
            return _err(400, "unknown group_by_table", group_by_table)
        qdict["dimensions"] = [{"table": group_by_table, "field": group_by_field}]
        qdict["order_by"] = [{"field": stages_present[0]["key"], "direction": "desc"}]

    try:
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
    except (QueryBuildError, ResolverError, KeyError) as e:
        return _err(400, "could not build funnel query", str(e))
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return _err(503, "BigQuery not configured", str(e))
    except Exception as e:
        return _err(500, "funnel query failed", str(e))

    # Process rows into funnel structure
    dim_count = 1 if (group_by_table and group_by_field) else 0
    metric_keys = [s["key"] for s in stages_present]

    def _safe_float(v):
        try: return float(v) if v is not None else 0.0
        except (TypeError, ValueError): return 0.0

    # Overall (no group_by, or aggregated across groups): sum each metric
    overall = {k: 0.0 for k in metric_keys}
    for row in result.rows:
        for i, k in enumerate(metric_keys):
            overall[k] += _safe_float(row[dim_count + i])

    # Build stages with conversion rates and drop-off
    out_stages = []
    prev_val = None
    first_val = overall[metric_keys[0]] if metric_keys else 0
    for s in stages_present:
        v = overall[s["key"]]
        stage = {
            "key": s["key"],
            "label": s["label"],
            "kind": s["kind"],
            "value": v,
            "format": _metric_format(model, table, s["key"]),
            "conversion_from_prev": None,
            "conversion_from_start": None,
            "drop_off_from_prev": None,
        }
        if prev_val is not None and prev_val > 0:
            stage["conversion_from_prev"] = v / prev_val
            stage["drop_off_from_prev"] = max(0.0, prev_val - v)
        if first_val and first_val > 0 and metric_keys[0] != s["key"]:
            stage["conversion_from_start"] = v / first_val
        out_stages.append(stage)
        prev_val = v

    # Per-group breakdown (if group_by used) — funnel per group, top 8
    groups = []
    if dim_count and result.rows:
        for row in result.rows[:8]:
            label = str(row[0]) if row[0] is not None else "∅"
            gstages = []
            gprev = None; gfirst = None
            for i, k in enumerate(metric_keys):
                v = _safe_float(row[dim_count + i])
                if i == 0: gfirst = v
                gs = {"key": k, "value": v,
                      "conversion_from_prev": (v / gprev) if (gprev is not None and gprev > 0) else None,
                      "conversion_from_start": (v / gfirst) if (gfirst and gfirst > 0 and i > 0) else None}
                gstages.append(gs)
                gprev = v
            groups.append({"label": label, "stages": gstages})

    return {
        "ok": True,
        "table": table,
        "period": period,
        "period_range": date_flt["value"] if date_flt else None,
        "stages": out_stages,
        "groups": groups,
        "group_by": {"table": group_by_table, "field": group_by_field} if dim_count else None,
        "total_rows": result.row_count,
        "bytes_processed": result.bytes_processed,
    }


# ═══════════════════════════════════════════════════════════════════
#  AI COACH — actionable recommendations based on goals + recent data
#  Goes one layer past Pulse: not just "what's happening" but "what to do."
# ═══════════════════════════════════════════════════════════════════

_COACH_SYSTEM = (
    "You are a senior marketing operations consultant advising a marketing manager. "
    "Given the user's GOALS with current evaluations and the curated semantic layer, "
    "produce 3 to 5 SPECIFIC, ACTIONABLE recommendations they should consider this week. "
    "Each recommendation must cite real numbers from the evaluations. Avoid vague advice. "
    "If a goal is on track and there is nothing pressing, say so honestly — do not invent issues.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"recommendations": [\n'
    '  {"title":"<imperative phrase, under 12 words>", '
    '   "rationale":"<one or two sentences citing specific numbers>", '
    '   "action":"<concrete step, under 20 words>", '
    '   "expected_impact":"<short outcome sentence>", '
    '   "confidence":"high|medium|low", '
    '   "urgency":"now|this_week|when_convenient", '
    '   "related_metric":"<table.metric of the goal it ties to or null>", '
    '   "ask_question":"<a natural-language question they could ask Ask AI to dig deeper>"}\n'
    "]}\n"
    "Rules: pick recommendations with the highest leverage first. Use concrete dollar amounts and percentages. "
    "Reference the metric and the gap. Be direct — no hedging filler. "
    "Recommendations should be diverse: not all about the same metric."
)


def _coach_context_for_llm(goals: list, curated_summary: dict) -> str:
    lines = ["Curated semantic layer:"]
    lines.append(f"  Tables: {curated_summary.get('tables', [])}")
    lines.append(f"  Metrics available: {curated_summary.get('metrics', [])}")
    lines.append("")
    lines.append(f"Goals ({len(goals)} total):")
    for g in goals:
        ev = g.get("evaluation") or {}
        actual = ev.get("actual")
        delta_pct = ev.get("delta_pct")
        ot = ev.get("on_track")
        ot_s = "ON TRACK" if ot is True else ("OFF TRACK" if ot is False else "NO DATA")
        dp_s = f"{delta_pct:+.1f}%" if isinstance(delta_pct, (int, float)) else "?"
        act_s = f"{actual:,.0f}" if isinstance(actual, (int, float)) else "?"
        target_s = f"{g.get('target'):,.0f}" if isinstance(g.get('target'), (int, float)) else "?"
        lines.append(
            f"  - {g.get('name')} [{ot_s}] {g.get('metric')} on {g.get('table')} | "
            f"actual {act_s} vs target ({g.get('comparison','ge')}) {target_s} | delta {dp_s} | period {g.get('period')}"
        )
    return "\n".join(lines)


@router.get("/coach/recommendations")
async def coach_recommendations():
    """Read goals + their evaluations, return 3-5 actionable AI recommendations."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return _err(503, "AI not configured", "ANTHROPIC_API_KEY is not set.")
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    # Pull goals with evaluations (uses goals_api in-process)
    goals = []
    try:
        from goals_api import list_goals as _list_goals
        goals = (await _list_goals(evaluate=True)).get("goals", [])
    except Exception as e:
        log.warning(f"coach goals load failed: {e}")

    # Compact curated-layer summary for the prompt
    curated_summary = {
        "tables": list(model.tables.keys()),
        "metrics": [
            f"{t.key}.{f.key}"
            for t in model.tables.values()
            for f in (t.metrics + [type("F", (), {"key": k})() for k in model.calculated_metrics.keys()
                                   if t.key in model.calculated_metrics[k].available_in])
        ][:80],   # cap so prompt stays light
    }

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=_COACH_SYSTEM,
            messages=[{"role": "user", "content": _coach_context_for_llm(goals, curated_summary)}],
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:
        return _err(502, "AI request failed", str(e))

    spec = _extract_json(raw) or {}
    recs = spec.get("recommendations") or []
    # Lightweight validation: drop entries missing the core fields
    cleaned = []
    for r in recs:
        if not isinstance(r, dict): continue
        if not (r.get("title") and r.get("rationale")): continue
        cleaned.append({
            "title": r.get("title", "").strip(),
            "rationale": r.get("rationale", "").strip(),
            "action": (r.get("action") or "").strip(),
            "expected_impact": (r.get("expected_impact") or "").strip(),
            "confidence": (r.get("confidence") or "medium"),
            "urgency": (r.get("urgency") or "this_week"),
            "related_metric": r.get("related_metric"),
            "ask_question": (r.get("ask_question") or "").strip(),
        })
    return {"ok": True, "count": len(cleaned), "recommendations": cleaned,
            "goals_considered": len(goals)}


# ═══════════════════════════════════════════════════════════════════
#  STAGE 4 — CURATOR JARVIS (text + voice chat in the curation workspace)
#  Own channel — does NOT touch /ws/voice. Browser uses Web Speech API
#  for STT; this endpoint returns the curator's response. Personality is
#  distinct from analysis Jarvis: proactive, proposing, opinionated.
# ═══════════════════════════════════════════════════════════════════

_CURATOR_SYSTEM = (
    "You are JARVIS in Curator Mode — a British butler-style AI helping a marketing data "
    "manager design the semantic layer over a BigQuery warehouse. You ARE distinct from "
    "the Analysis Jarvis: more proactive, opinionated, and direct. You speak in 1–2 short sentences. "
    "Dry wit, economy of language. Address the user as 'sir'.\n\n"
    "You can SUGGEST and EXPLAIN, but cannot directly modify the layer — you propose actions "
    "and the user confirms. Respond with a JSON object only, no prose, no code fences:\n"
    '{"reply": "<one or two short sentences for voice>", '
    '"action": null | {"kind":"open_table"|"rename_table"|"rename_field"|"add_metric"|"show_curated"|"show_raw_schema","args":{...}}}\n'
    "Action kinds:\n"
    "  open_table          args: {table:'<raw_table_name>'} — wake a raw table in the center panel\n"
    "  rename_table        args: {table_key:'<curated_key>', display_name:'<new>'} — propose a rename\n"
    "  rename_field        args: {table_key:'<key>', field_key:'<key>', display_name:'<new>'} — propose a rename\n"
    "  add_metric          args: {table_key:'<key>', name:'<display>', formula:'<formula>'} — propose a new metric\n"
    "  show_curated        args: {} — summarize what's in the curated layer\n"
    "  show_raw_schema     args: {table:'<raw_table_name>'} — show raw schema for a table\n"
    "If no clear action is implied, return action:null and just reply conversationally.\n"
    "If the user asks 'what's curated' or similar, return action 'show_curated'."
)


def _curator_context(model, ctx: dict) -> str:
    lines = ["Curated layer summary:"]
    lines.append(f"  Curated tables: {len(model.tables)} — keys: {list(model.tables.keys())}")
    lines.append(f"  Calculated metrics: {len(model.calculated_metrics)} — keys: {list(model.calculated_metrics.keys())}")
    lines.append(f"  Combined dimensions: {len(model.combined_dimensions)} — keys: {list(model.combined_dimensions.keys())}")
    if ctx:
        if ctx.get("active_raw_table"):
            lines.append(f"  Active raw table on screen: {ctx['active_raw_table']}")
        if ctx.get("active_curated_key"):
            lines.append(f"  Active curated table: {ctx['active_curated_key']}")
    return "\n".join(lines)


class CuratorRequest(BaseModel):
    message: str
    context: Optional[dict] = None
    history: Optional[list] = None  # [{role:'user'|'assistant', text}]


@router.post("/curator/chat")
async def curator_chat(body: CuratorRequest):
    if not body.message.strip():
        return _err(400, "empty message", "Say something.")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        # No AI configured — degrade to a useful canned response so the UI still works.
        return {"ok": True, "reply": "AI isn't configured, sir, but I'm listening. Set ANTHROPIC_API_KEY to enable me.", "action": None}
    try:
        model = load_model()
    except SemanticLoadError as e:
        return _err(400, "semantic.yaml failed to load", str(e))

    msgs = []
    for t in (body.history or [])[-6:]:
        role = t.get("role"); txt = (t.get("text") or "").strip()
        if not role or not txt: continue
        msgs.append({"role": role, "content": txt})
    user_content = _curator_context(model, body.context or {}) + "\n\nUser: " + body.message
    msgs.append({"role": "user", "content": user_content})

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_CURATOR_SYSTEM,
            messages=msgs,
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:
        return _err(502, "AI request failed", str(e))

    j = _extract_json(raw) or {}
    reply = (j.get("reply") or "").strip() or "Noted, sir."
    action = j.get("action") if isinstance(j.get("action"), dict) else None
    return {"ok": True, "reply": reply, "action": action}


# ═══════════════════════════════════════════════════════════════════
#  PULSE — executive morning briefing across goals + briefs + dashboards
# ═══════════════════════════════════════════════════════════════════

_PULSE_SYSTEM = (
    "You are a marketing analyst writing a one-sentence daily executive headline. "
    "Given a status snapshot of goals, recent briefs, and dashboard counts, return ONLY a JSON object "
    "with no prose, no code fences:\n"
    '{"headline": "<one decisive sentence under 22 words>"}\n'
    "Lead with the most important fact — typically the most off-track goal or the biggest change. "
    "If everything is on track and there's nothing dramatic, say so plainly. No hedging."
)


def _pulse_summary_for_llm(goals: list, briefs: list, dashboards: list) -> str:
    off = [g for g in goals if (g.get("evaluation") or {}).get("on_track") is False]
    on  = [g for g in goals if (g.get("evaluation") or {}).get("on_track") is True]
    lines = [f"Goals: {len(goals)} total, {len(on)} on track, {len(off)} off track."]
    for g in off[:5]:
        ev = g.get("evaluation") or {}
        dp = ev.get("delta_pct")
        dp_s = f"{dp:+.1f}%" if isinstance(dp, (int, float)) else "?"
        lines.append(f"  OFF: {g.get('name')} — actual {ev.get('actual')} vs target {g.get('target')} ({dp_s})")
    for g in on[:3]:
        ev = g.get("evaluation") or {}
        dp = ev.get("delta_pct")
        dp_s = f"{dp:+.1f}%" if isinstance(dp, (int, float)) else "?"
        lines.append(f"  ON: {g.get('name')} — actual {ev.get('actual')} vs target {g.get('target')} ({dp_s})")
    lines.append(f"Saved briefs: {len(briefs)}. Dashboards: {len(dashboards)}.")
    if briefs[:3]:
        lines.append("Recent briefs:")
        for b in briefs[:3]:
            lines.append(f"  - {b.get('title') or b.get('prompt') or '(untitled)'}")
    return "\n".join(lines)


@router.get("/pulse")
async def pulse():
    """One-screen executive summary across goals, briefs, dashboards, with an AI headline."""
    # Pull everything in parallel from the in-process modules.
    try:
        from goals_api import list_goals as _list_goals
    except Exception:
        _list_goals = None
    try:
        from briefs_api import list_briefs as _list_briefs
    except Exception:
        _list_briefs = None
    try:
        from views_api import list_views as _list_views
    except Exception:
        _list_views = None

    goals = []
    if _list_goals:
        try: goals = (await _list_goals(evaluate=True)).get("goals", [])
        except Exception: goals = []
    briefs = []
    if _list_briefs:
        try: briefs = (await _list_briefs()).get("briefs", [])
        except Exception: briefs = []
    views = []
    if _list_views:
        try: views = (await _list_views()).get("views", [])
        except Exception: views = []

    off = [g for g in goals if (g.get("evaluation") or {}).get("on_track") is False]
    on  = [g for g in goals if (g.get("evaluation") or {}).get("on_track") is True]

    # Biggest-deviation off-track goal (by abs delta_pct)
    def _abs_delta(g):
        d = (g.get("evaluation") or {}).get("delta_pct")
        return abs(d) if isinstance(d, (int, float)) else -1
    biggest_miss = max(off, key=_abs_delta) if off else None

    # Optional AI headline — best-effort, never fails the response
    headline = None
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key and (goals or briefs or views):
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=key)
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=_PULSE_SYSTEM,
                messages=[{"role": "user", "content": _pulse_summary_for_llm(goals, briefs, views)}],
            )
            raw = resp.content[0].text if resp.content else ""
            j = _extract_json(raw) or {}
            headline = j.get("headline") or None
        except Exception as e:
            log.warning(f"pulse headline failed: {e}")

    return {
        "ok": True,
        "headline": headline,
        "goals": {
            "total": len(goals),
            "on_track": len(on),
            "off_track": len(off),
            "biggest_miss": biggest_miss,
            "off": off[:6],
            "on": on[:6],
        },
        "briefs": briefs[:6],
        "dashboards": views[:6],
        "generated_at": time.time() if False else None,  # client adds timestamp from response time
    }


async def _generate_brief(client, question: str, columns, rows, formats) -> Optional[dict]:
    """Ask Claude to interpret the actual data and write a strategic brief.
    Returns None on failure rather than failing the whole /ask response."""
    try:
        rows_txt = _rows_for_prompt(columns, rows, formats)
        # Richer grounding: give the model the shape + a lightweight data-quality read
        # so it can call out gaps (nulls, unclassified buckets) instead of glossing them.
        rc = len(rows or [])
        dq = _brief_data_quality(columns, rows)
        dq_line = ("\n\nData-quality signals: " + dq) if dq else ""
        msg = (f"Question: {question}\n\nColumns: {', '.join(columns)}\nRow count: {rc}\n"
               f"Rows:\n{rows_txt}{dq_line}")
        # Briefs are written AFTER the data returns, so latency is non-critical —
        # use a stronger model for materially better synthesis. Configurable.
        brief_model = os.getenv("JARVIS_BRIEF_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=brief_model,
            max_tokens=1500,
            system=_BRIEF_SYSTEM,
            messages=[{"role": "user", "content": msg}],
        )
        raw = resp.content[0].text if resp.content else ""
        return _extract_json(raw)
    except Exception as e:
        log.warning(f"brief generation failed: {e}")
        # Fall back to the fast model if the stronger one is unavailable/over quota.
        try:
            rows_txt = _rows_for_prompt(columns, rows, formats)
            msg = f"Question: {question}\n\nColumns: {', '.join(columns)}\nRows:\n{rows_txt}"
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=900,
                system=_BRIEF_SYSTEM, messages=[{"role": "user", "content": msg}],
            )
            return _extract_json(resp.content[0].text if resp.content else "")
        except Exception as e2:
            log.warning(f"brief fallback failed: {e2}")
            return None


def _brief_data_quality(columns, rows) -> str:
    """A compact data-quality read for the brief: null-heavy columns + unclassified
    buckets in the leading dimension. Cheap, deterministic, no LLM."""
    try:
        if not rows:
            return "the result set is empty."
        notes = []
        n = len(rows)
        # Null share per column (flag columns >30% null).
        for ci, col in enumerate(columns or []):
            nulls = sum(1 for r in rows if ci < len(r) and (r[ci] is None or r[ci] == ""))
            if n and nulls / n > 0.3:
                notes.append(f"'{col}' is {round(100*nulls/n)}% null")
        # 'Unclassified/Other/Brand/null' style buckets in the first column.
        if columns:
            junk = {"unclassified", "other", "none", "null", "n/a", "unknown", "(direct)", "brand"}
            for r in rows[:60]:
                v = str(r[0]).strip().lower() if r and r[0] is not None else ""
                if any(j in v for j in junk):
                    notes.append(f"the leading dimension contains a catch-all bucket ('{r[0]}')")
                    break
        return "; ".join(notes[:3]) if notes else ""
    except Exception:
        return ""
