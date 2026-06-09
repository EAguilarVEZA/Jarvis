"""
queries_api
───────────
R40 — Saved Query Panel queries. Persisted to ~/jarvis/semantic/queries.json
(atomic writes + timestamped backups, same pattern as briefs_api / views_api).

A saved "query" is a reusable dataset definition: a StructuredQuery dict
({primary_table, dimensions, metrics, filters, limit, order_by}) plus
ownership/sharing metadata. The Report Designer (Workspace 3) consumes
these as datasets.

Routes (prefix /api/queries):
  GET    /api/queries            — list summaries
  GET    /api/queries/{id}       — full query payload
  POST   /api/queries            — create
  PUT    /api/queries/{id}       — update fields
  DELETE /api/queries/{id}       — remove
  POST   /api/queries/{id}/run   — re-execute against the live semantic layer
"""

from __future__ import annotations

import json
import os
import time
import uuid
import shutil
import threading
from typing import Optional, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

router = APIRouter(prefix="/api/queries", tags=["queries"])

QUERIES_PATH = SEMANTIC_DIR / "queries.json"
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not QUERIES_PATH.exists():
        return {"queries": []}
    try:
        with open(QUERIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "queries" not in data:
            return {"queries": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"queries": []}


def _atomic_write(data: dict):
    QUERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if QUERIES_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(QUERIES_PATH, QUERIES_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = QUERIES_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(QUERIES_PATH)


def _summary(q: dict) -> dict:
    """Lightweight projection for list views."""
    qd = q.get("qdict") or {}
    return {
        "id": q.get("id"),
        "name": q.get("name"),
        "description": q.get("description"),
        "primary_table": qd.get("primary_table"),
        "dim_count": len(qd.get("dimensions") or []),
        "metric_count": len(qd.get("metrics") or []),
        "filter_count": len(qd.get("filters") or []),
        "owner_email": q.get("owner_email"),
        "scope": q.get("scope") or "private",
        "created_at": q.get("created_at"),
        "updated_at": q.get("updated_at"),
    }


def _viewer_email(request: Request) -> Optional[str]:
    """Identity comes from the X-Jarvis-User header that the frontend already sends."""
    return (request.headers.get("X-Jarvis-User") or "").strip() or None


def _can_see(q: dict, viewer: Optional[str]) -> bool:
    """Sharing rules: private→owner only, group/company/corporate→anyone with the header."""
    scope = (q.get("scope") or "private").lower()
    if scope != "private":
        return True
    return bool(viewer) and viewer == q.get("owner_email")


class QueryCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    qdict: dict
    scope: Optional[str] = "private"  # private | group | corporate


class QueryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    qdict: Optional[dict] = None
    scope: Optional[str] = None


@router.get("")
async def list_queries(request: Request):
    viewer = _viewer_email(request)
    data = _load()
    items = [_summary(q) for q in data["queries"] if _can_see(q, viewer)]
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return {"queries": items, "count": len(items)}


@router.get("/{query_id}")
async def get_query(query_id: str, request: Request):
    viewer = _viewer_email(request)
    data = _load()
    q = next((x for x in data["queries"] if x.get("id") == query_id), None)
    if not q:
        return _err(404, "not found", f"No query '{query_id}'")
    if not _can_see(q, viewer):
        return _err(403, "forbidden", "This query is private to its owner.")
    return q


@router.post("")
async def create_query(body: QueryCreate, request: Request):
    if not body.name.strip():
        return _err(400, "name required", "Give the query a name so you can find it later.")
    if not body.qdict or not body.qdict.get("primary_table"):
        return _err(400, "qdict required", "The saved query needs at least a primary_table.")
    scope = (body.scope or "private").lower()
    if scope not in ("private", "group", "corporate"):
        scope = "private"
    owner = _viewer_email(request)
    with _lock:
        data = _load()
        q = {
            "id": str(uuid.uuid4()),
            "name": body.name.strip(),
            "description": (body.description or "").strip(),
            "qdict": body.qdict,
            "owner_email": owner,
            "scope": scope,
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["queries"].append(q)
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return q


@router.put("/{query_id}")
async def update_query(query_id: str, body: QueryUpdate, request: Request):
    viewer = _viewer_email(request)
    with _lock:
        data = _load()
        q = next((x for x in data["queries"] if x.get("id") == query_id), None)
        if not q:
            return _err(404, "not found", f"No query '{query_id}'")
        # Only the owner can edit
        if q.get("owner_email") and viewer != q.get("owner_email"):
            return _err(403, "forbidden", "Only the owner can edit a saved query.")
        if body.name is not None:        q["name"] = body.name.strip() or q["name"]
        if body.description is not None: q["description"] = body.description.strip()
        if body.qdict is not None:       q["qdict"] = body.qdict
        if body.scope is not None:
            scope = body.scope.lower()
            if scope in ("private", "group", "corporate"):
                q["scope"] = scope
        q["updated_at"] = _now()
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return q


@router.delete("/{query_id}")
async def delete_query(query_id: str, request: Request):
    viewer = _viewer_email(request)
    with _lock:
        data = _load()
        before = len(data["queries"])
        # Owner check
        target = next((x for x in data["queries"] if x.get("id") == query_id), None)
        if not target:
            return _err(404, "not found", f"No query '{query_id}'")
        if target.get("owner_email") and viewer != target.get("owner_email"):
            return _err(403, "forbidden", "Only the owner can delete a saved query.")
        data["queries"] = [x for x in data["queries"] if x.get("id") != query_id]
        if len(data["queries"]) == before:
            return _err(404, "not found", f"No query '{query_id}'")
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": query_id}


@router.post("/{query_id}/run")
async def run_query(query_id: str, request: Request, limit: Optional[int] = None):
    """Re-execute a saved query against the live semantic layer.

    Delegates to semantic_api's internal pipeline so the query goes through
    the same date-resolution / fallback rules as everything else.
    """
    viewer = _viewer_email(request)
    data = _load()
    q = next((x for x in data["queries"] if x.get("id") == query_id), None)
    if not q:
        return _err(404, "not found", f"No query '{query_id}'")
    if not _can_see(q, viewer):
        return _err(403, "forbidden", "This query is private to its owner.")
    qdict = dict(q.get("qdict") or {})
    if limit is not None and limit > 0:
        qdict["limit"] = int(limit)
    # Build + execute. Imported here to avoid a top-level circular dep.
    try:
        from semantic_api import _load_fresh as _semantic_load
        from semantic.query_builder import StructuredQuery, build_sql
        from semantic.executor import run_query as _run_query
    except Exception as e:
        return _err(500, "engine import failed", str(e))
    try:
        model = _semantic_load()
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
        import asyncio
        result = await asyncio.get_running_loop().run_in_executor(None, _run_query, sql)
    except Exception as e:
        return _err(500, "query failed", str(e))
    return {
        "query": _summary(q),
        "columns": result.columns,
        "rows": result.rows,
        "row_count": getattr(result, "row_count", len(result.rows or [])),
        "sql": sql,
    }
