"""
hierarchies_api
───────────────
Hierarchy + Preset library — mirrors Kaelio's tree-select filter and saved
presets concept (e.g. the OH service-line tree with "OHWI", "All of Maria's
Campaigns" presets, plus saved date-range presets).

A *hierarchy* is a named tree of nodes that filter on a specific BQ field.
Example: "OH Service Lines" hierarchy filters `campaigns.service_line`.
Each leaf carries `match_values` — the raw column values the leaf represents.
When a user picks N leaves, the dashboard generates a WHERE clause:

    campaigns.service_line IN ('Pediatrics','OHCI','...')

A *preset* is a named saved selection. Two flavors:
  • "tree-select" — references a hierarchy + a set of selected node IDs
  • "date-range"  — a (start, end) pair, optionally with a comparison range

Both are scoped per-dashboard (dashboard_id field on the preset). Hierarchies
are global (any dashboard can mount one).

Storage:
  • ~/jarvis/dashboards/hierarchies.json
  • ~/jarvis/dashboards/presets.json
Both with atomic writes + timestamped backups (same pattern as knowledge.json).

Routes:
  Hierarchies:
    GET    /api/hierarchies                 — list (summaries)
    GET    /api/hierarchies/{id}            — full tree
    POST   /api/hierarchies                 — create
    PUT    /api/hierarchies/{id}            — update (whole-tree replace)
    DELETE /api/hierarchies/{id}            — delete
  Presets:
    GET    /api/presets?type=&dashboard_id= — list
    GET    /api/presets/{id}                — full
    POST   /api/presets                     — create
    PUT    /api/presets/{id}                — update
    DELETE /api/presets/{id}                — delete
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Storage lives alongside the other dashboard-scoped JSON files
JARVIS_HOME = Path.home() / "jarvis"
DASHBOARDS_DIR = JARVIS_HOME / "dashboards"
HIERARCHIES_PATH = DASHBOARDS_DIR / "hierarchies.json"
PRESETS_PATH = DASHBOARDS_DIR / "presets.json"

_lock = threading.Lock()

router = APIRouter(tags=["hierarchies"])


# ─── Shared helpers ────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load_json(path: Path, default_shape: dict) -> dict:
    if not path.exists():
        return dict(default_shape)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(default_shape)
        return data
    except (json.JSONDecodeError, OSError):
        return dict(default_shape)


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(path, path.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


# ═══════════════════════════════════════════════════════════════════
# Hierarchies
# ═══════════════════════════════════════════════════════════════════

class HierarchyNode(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None
    # match_values: only meaningful on leaf nodes — the raw column values that
    # this leaf represents. Branch nodes have empty match_values and rely on
    # their descendants.
    match_values: list[str] = Field(default_factory=list)
    sort_order: int = 0
    description: Optional[str] = None


class HierarchyCreate(BaseModel):
    name: str
    description: str = ""
    filter_table: str                     # e.g. "campaigns"
    filter_field: str                     # e.g. "service_line"
    nodes: list[HierarchyNode] = Field(default_factory=list)


class HierarchyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    filter_table: Optional[str] = None
    filter_field: Optional[str] = None
    nodes: Optional[list[HierarchyNode]] = None


def _hierarchy_summary(h: dict) -> dict:
    nodes = h.get("nodes", [])
    return {
        "id": h.get("id"),
        "name": h.get("name"),
        "description": h.get("description", ""),
        "filter_table": h.get("filter_table"),
        "filter_field": h.get("filter_field"),
        "node_count": len(nodes),
        "leaf_count": sum(1 for n in nodes
                          if not any((m.get("parent_id") == n.get("id")) for m in nodes)),
        "created_at": h.get("created_at"),
        "updated_at": h.get("updated_at"),
    }


@router.get("/api/hierarchies")
async def list_hierarchies():
    data = _load_json(HIERARCHIES_PATH, {"hierarchies": []})
    items = data.get("hierarchies", [])
    return {"hierarchies": [_hierarchy_summary(h) for h in items], "count": len(items)}


@router.get("/api/hierarchies/{hid}")
async def get_hierarchy(hid: str):
    data = _load_json(HIERARCHIES_PATH, {"hierarchies": []})
    h = next((x for x in data.get("hierarchies", []) if x.get("id") == hid), None)
    if not h:
        return _err(404, "not found", f"No hierarchy '{hid}'")
    return h


@router.post("/api/hierarchies")
async def create_hierarchy(body: HierarchyCreate):
    if not body.name.strip():
        return _err(400, "name required", "Hierarchy needs a name.")
    if not body.filter_table or not body.filter_field:
        return _err(400, "filter target required",
                    "Provide filter_table and filter_field (e.g. 'campaigns' / 'service_line').")
    with _lock:
        data = _load_json(HIERARCHIES_PATH, {"hierarchies": []})
        h = {
            "id": str(uuid.uuid4()),
            "name": body.name.strip(),
            "description": body.description,
            "filter_table": body.filter_table,
            "filter_field": body.filter_field,
            "nodes": [n.model_dump() for n in body.nodes],
            "created_at": _now(),
            "updated_at": _now(),
        }
        data.setdefault("hierarchies", []).append(h)
        try:
            _atomic_write(HIERARCHIES_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return h


@router.put("/api/hierarchies/{hid}")
async def update_hierarchy(hid: str, body: HierarchyUpdate):
    with _lock:
        data = _load_json(HIERARCHIES_PATH, {"hierarchies": []})
        h = next((x for x in data.get("hierarchies", []) if x.get("id") == hid), None)
        if not h:
            return _err(404, "not found", f"No hierarchy '{hid}'")
        if body.name is not None:        h["name"] = body.name.strip()
        if body.description is not None: h["description"] = body.description
        if body.filter_table is not None:h["filter_table"] = body.filter_table
        if body.filter_field is not None:h["filter_field"] = body.filter_field
        if body.nodes is not None:       h["nodes"] = [n.model_dump() for n in body.nodes]
        h["updated_at"] = _now()
        try:
            _atomic_write(HIERARCHIES_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return h


@router.delete("/api/hierarchies/{hid}")
async def delete_hierarchy(hid: str):
    with _lock:
        data = _load_json(HIERARCHIES_PATH, {"hierarchies": []})
        before = len(data.get("hierarchies", []))
        data["hierarchies"] = [x for x in data.get("hierarchies", []) if x.get("id") != hid]
        if len(data["hierarchies"]) == before:
            return _err(404, "not found", f"No hierarchy '{hid}'")
        try:
            _atomic_write(HIERARCHIES_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": hid}


# ═══════════════════════════════════════════════════════════════════
# Presets
# ═══════════════════════════════════════════════════════════════════

PresetT = Literal["tree-select", "date-range"]


class PresetCreate(BaseModel):
    type: PresetT
    name: str
    dashboard_id: Optional[str] = None    # if scoped to a single dashboard
    # tree-select payload
    hierarchy_id: Optional[str] = None
    selected_node_ids: list[str] = Field(default_factory=list)
    # date-range payload
    start_date: Optional[str] = None      # ISO yyyy-mm-dd
    end_date: Optional[str] = None
    compare_start: Optional[str] = None
    compare_end: Optional[str] = None
    description: str = ""


class PresetUpdate(BaseModel):
    name: Optional[str] = None
    dashboard_id: Optional[str] = None
    hierarchy_id: Optional[str] = None
    selected_node_ids: Optional[list[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    compare_start: Optional[str] = None
    compare_end: Optional[str] = None
    description: Optional[str] = None


@router.get("/api/presets")
async def list_presets(
    type: Optional[str] = Query(None, alias="type"),
    dashboard_id: Optional[str] = None,
):
    data = _load_json(PRESETS_PATH, {"presets": []})
    items = data.get("presets", [])
    if type:
        items = [p for p in items if p.get("type") == type]
    if dashboard_id:
        items = [p for p in items if not p.get("dashboard_id") or p.get("dashboard_id") == dashboard_id]
    return {"presets": items, "count": len(items)}


@router.get("/api/presets/{pid}")
async def get_preset(pid: str):
    data = _load_json(PRESETS_PATH, {"presets": []})
    p = next((x for x in data.get("presets", []) if x.get("id") == pid), None)
    if not p:
        return _err(404, "not found", f"No preset '{pid}'")
    return p


@router.post("/api/presets")
async def create_preset(body: PresetCreate):
    if not body.name.strip():
        return _err(400, "name required", "Presets need a name.")
    if body.type == "tree-select":
        if not body.hierarchy_id:
            return _err(400, "hierarchy_id required", "tree-select presets must reference a hierarchy.")
        if not body.selected_node_ids:
            return _err(400, "empty selection",
                        "Preset has no selected nodes — pick at least one before saving.")
    elif body.type == "date-range":
        if not body.start_date or not body.end_date:
            return _err(400, "date range required",
                        "date-range presets must include start_date and end_date.")
    with _lock:
        data = _load_json(PRESETS_PATH, {"presets": []})
        p = body.model_dump()
        p["id"] = str(uuid.uuid4())
        p["created_at"] = _now()
        p["updated_at"] = _now()
        data.setdefault("presets", []).append(p)
        try:
            _atomic_write(PRESETS_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return p


@router.put("/api/presets/{pid}")
async def update_preset(pid: str, body: PresetUpdate):
    with _lock:
        data = _load_json(PRESETS_PATH, {"presets": []})
        p = next((x for x in data.get("presets", []) if x.get("id") == pid), None)
        if not p:
            return _err(404, "not found", f"No preset '{pid}'")
        for field, v in body.model_dump(exclude_unset=True).items():
            if v is not None:
                p[field] = v
        p["updated_at"] = _now()
        try:
            _atomic_write(PRESETS_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return p


@router.delete("/api/presets/{pid}")
async def delete_preset(pid: str):
    with _lock:
        data = _load_json(PRESETS_PATH, {"presets": []})
        before = len(data.get("presets", []))
        data["presets"] = [x for x in data.get("presets", []) if x.get("id") != pid]
        if len(data["presets"]) == before:
            return _err(404, "not found", f"No preset '{pid}'")
        try:
            _atomic_write(PRESETS_PATH, data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": pid}


# ═══════════════════════════════════════════════════════════════════
# Helper used by the query layer to expand a preset into a filter clause.
# Not currently called from anywhere — the UI translates client-side. Kept
# here so a future server-side path (e.g. /ask) can use it too.
# ═══════════════════════════════════════════════════════════════════

def expand_tree_preset_to_match_values(preset_id: str) -> Optional[dict]:
    """Resolve a tree-select preset into a query filter spec.

    Returns: {"table": str, "field": str, "op": "in", "value": [str, ...]} or None.
    """
    presets = _load_json(PRESETS_PATH, {"presets": []}).get("presets", [])
    preset = next((p for p in presets if p.get("id") == preset_id), None)
    if not preset or preset.get("type") != "tree-select":
        return None
    h_id = preset.get("hierarchy_id")
    h = next((h for h in _load_json(HIERARCHIES_PATH, {"hierarchies": []}).get("hierarchies", [])
              if h.get("id") == h_id), None)
    if not h:
        return None
    selected = set(preset.get("selected_node_ids", []))
    nodes = h.get("nodes", [])
    # Walk: collect match_values from every selected node AND from its descendants
    by_parent: dict[str, list[dict]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_id"), []).append(n)
    values: list[str] = []
    seen_node_ids: set[str] = set()
    def collect(node_id: str):
        if node_id in seen_node_ids:
            return
        seen_node_ids.add(node_id)
        node = next((n for n in nodes if n.get("id") == node_id), None)
        if not node:
            return
        values.extend(node.get("match_values") or [])
        for child in by_parent.get(node_id, []):
            collect(child.get("id"))
    for nid in selected:
        collect(nid)
    # Dedup
    values = list(dict.fromkeys(values))
    return {
        "table": h.get("filter_table"),
        "field": h.get("filter_field"),
        "op": "in",
        "value": values,
    }
