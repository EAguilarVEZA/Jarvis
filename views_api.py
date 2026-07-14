"""
views_api
─────────
Presentation layer — persistent dashboards ("Views").

A View is a saved dashboard: one or more pages, each holding widgets.
Every widget carries a StructuredQuery (the same shape /api/semantic/query
accepts) plus its chart type, title, and grid layout. The dashboards app
loads a View, runs each widget's query through the semantic engine, and
renders the result.

Storage: a single JSON file (views.json) next to semantic.yaml, written
atomically with a timestamped backup — same safety pattern as the writer.

Routes (prefix /api/views):
  GET    /api/views            — list views (summaries)
  GET    /api/views/{id}       — full view
  POST   /api/views            — create
  PUT    /api/views/{id}       — replace/update (save canvas)
  DELETE /api/views/{id}       — delete
"""

from __future__ import annotations

import json
import os
import time
import uuid
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from semantic.loader import SEMANTIC_DIR

router = APIRouter(prefix="/api/views", tags=["views"])

VIEWS_PATH = SEMANTIC_DIR / "views.json"
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not VIEWS_PATH.exists():
        return {"views": []}
    try:
        with open(VIEWS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "views" not in data:
            return {"views": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"views": []}


def _atomic_write(data: dict):
    VIEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if VIEWS_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(VIEWS_PATH, VIEWS_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = VIEWS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(VIEWS_PATH)


def _summary(v: dict) -> dict:
    pages = v.get("pages", [])
    widget_count = sum(len(p.get("widgets", [])) for p in pages)
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "description": v.get("description", ""),
        "scope": v.get("scope", "personal"),
        "page_count": len(pages),
        "widget_count": widget_count,
        "updated_at": v.get("updated_at"),
        "created_at": v.get("created_at"),
    }


# ─── Pydantic request models (loose — widgets are free-form dicts) ───
class ViewCreate(BaseModel):
    name: str
    description: str = ""
    scope: str = "personal"
    pages: Optional[list] = None


class ViewUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    scope: Optional[str] = None
    pages: Optional[list] = None
    filters: Optional[list] = None


def _blank_page() -> dict:
    return {"id": str(uuid.uuid4()), "name": "Page 1", "widgets": []}


class AIDashRequest(BaseModel):
    description: str
    start: str
    end: str


@router.post("/ai")
async def ai_dashboard(body: AIDashRequest):
    """Plan a dashboard from a plain-English description (Haiku over approved measures).
    Returns {name, widgets:[{title, chart_type, query}]} — the builder renders it and
    the user saves via POST /api/views."""
    if not body.description.strip():
        return _err(400, "description required", "Describe the dashboard you want.")
    if not body.start or not body.end:
        return _err(400, "date range required", "Provide start and end dates.")
    try:
        from dashboard_engine import plan_dashboard
        plan = await plan_dashboard(body.description.strip(), body.start, body.end)
        return {"ok": True, **plan}
    except Exception as e:  # noqa: BLE001
        return _err(500, "ai planning failed", str(e))


# ─── Endpoints ───────────────────────────────────────────────────────
@router.get("")
async def list_views():
    data = _load()
    return {"views": [_summary(v) for v in data["views"]], "count": len(data["views"])}


@router.get("/{view_id}")
async def get_view(view_id: str):
    data = _load()
    v = next((x for x in data["views"] if x.get("id") == view_id), None)
    if not v:
        return _err(404, "not found", f"No view '{view_id}'")
    return v


@router.post("")
async def create_view(body: ViewCreate):
    with _lock:
        data = _load()
        v = {
            "id": str(uuid.uuid4()),
            "name": body.name,
            "description": body.description,
            "scope": body.scope if body.scope in ("personal", "company") else "personal",
            "pages": body.pages if body.pages else [_blank_page()],
            "filters": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["views"].append(v)
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return v


@router.put("/{view_id}")
async def update_view(view_id: str, body: ViewUpdate):
    with _lock:
        data = _load()
        v = next((x for x in data["views"] if x.get("id") == view_id), None)
        if not v:
            return _err(404, "not found", f"No view '{view_id}'")
        if body.name is not None: v["name"] = body.name
        if body.description is not None: v["description"] = body.description
        if body.scope is not None and body.scope in ("personal", "company"): v["scope"] = body.scope
        if body.pages is not None: v["pages"] = body.pages
        if body.filters is not None: v["filters"] = body.filters
        v["updated_at"] = _now()
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return v


@router.delete("/{view_id}")
async def delete_view(view_id: str):
    with _lock:
        data = _load()
        before = len(data["views"])
        data["views"] = [x for x in data["views"] if x.get("id") != view_id]
        if len(data["views"]) == before:
            return _err(404, "not found", f"No view '{view_id}'")
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": view_id}
