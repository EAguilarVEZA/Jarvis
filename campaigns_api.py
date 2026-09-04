"""
campaigns_api
─────────────
Campaign registry behind the Phase 3 executive campaign map. Each campaign is a
record on a service line, owned by a manager, positioned on the Ryan Deiss /
Jared Rhode funnel (content → lead magnet → tripwire → core offer → maximizer)
with a lifecycle status (idea → planned → live → measured).

The map's manager views reuse hierarchies_api's OH service-line hierarchy +
saved presets — a manager's "my service lines" view is a saved tree-select
preset. This module only owns the campaign records + the board rollup.

Campaign media / ad creative / briefs are intentionally NOT stored here — those
live in Workfront and arrive via the Workfront MCP. We keep a `workfront_id` +
a light `workfront_assets` list so the detail panel can link out; the heavy
assets stay in Workfront.

Storage: ~/jarvis/dashboards/campaigns.json  (atomic writes + .bak, same pattern
as hierarchies.json / presets.json).

Routes:
  GET    /api/campaigns                 — list (filters: service_line, manager, stage, status)
  GET    /api/campaigns/board           — rollup: KPIs + service-line × stage grid
  GET    /api/campaigns/{cid}           — one campaign
  POST   /api/campaigns                 — create
  PUT    /api/campaigns/{cid}           — update
  DELETE /api/campaigns/{cid}           — delete
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

JARVIS_HOME = Path.home() / "jarvis"
DASHBOARDS_DIR = JARVIS_HOME / "dashboards"
CAMPAIGNS_PATH = DASHBOARDS_DIR / "campaigns.json"

_lock = threading.Lock()
router = APIRouter(tags=["campaigns"])

# Funnel stages in canonical order, with display labels for the board columns.
STAGES = ["content", "lead_magnet", "tripwire", "core_offer", "maximizer"]
STAGE_LABELS = {
    "content": "Content",
    "lead_magnet": "Lead magnet",
    "tripwire": "Tripwire",
    "core_offer": "Core offer",
    "maximizer": "Maximizer",
}
STATUSES = ["idea", "planned", "live", "measured"]


# ─── helpers ───────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not CAMPAIGNS_PATH.exists():
        return {"campaigns": []}
    try:
        with open(CAMPAIGNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"campaigns": []}
    except (json.JSONDecodeError, OSError):
        return {"campaigns": []}


def _atomic_write(data: dict):
    CAMPAIGNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CAMPAIGNS_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(CAMPAIGNS_PATH, CAMPAIGNS_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = CAMPAIGNS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(CAMPAIGNS_PATH)


# ─── model ─────────────────────────────────────────────────────────

class CampaignIn(BaseModel):
    name: str
    service_line: str = ""
    manager: str = ""
    manager_initials: str = ""
    stage: str = "content"
    status: str = "idea"
    leads: int = 0
    spend: float = 0.0
    metric_note: str = ""          # human summary e.g. "412 leads · 38% opt-in"
    workfront_id: str = ""
    workfront_assets: list = Field(default_factory=list)
    notes: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    service_line: Optional[str] = None
    manager: Optional[str] = None
    manager_initials: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    leads: Optional[int] = None
    spend: Optional[float] = None
    metric_note: Optional[str] = None
    workfront_id: Optional[str] = None
    workfront_assets: Optional[list] = None
    notes: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


# ─── seed (only if the store is empty) ─────────────────────────────

def _seed() -> list:
    def c(name, sl, mgr, mi, stage, status, leads, spend, note, wf, assets):
        return {
            "id": str(uuid.uuid4()), "name": name, "service_line": sl,
            "manager": mgr, "manager_initials": mi, "stage": stage, "status": status,
            "leads": leads, "spend": spend, "metric_note": note,
            "workfront_id": wf, "workfront_assets": assets, "notes": "",
            "start_date": None, "end_date": None,
            "created_at": _now(), "updated_at": _now(),
        }
    return [
        c("Heart-health blog series", "Heart & vascular", "Maria Reyes", "MR", "content", "live", 0, 1200, "22k views · 6 posts", "WF-4471", ["6 assets"]),
        c("Heart-risk quiz", "Heart & vascular", "Maria Reyes", "MR", "lead_magnet", "live", 412, 3800, "412 leads · 38% opt-in", "WF-4472", ["3 creatives"]),
        c("$0 calcium scan screen", "Heart & vascular", "Maria Reyes", "MR", "tripwire", "live", 96, 5200, "96 booked", "WF-4473", ["landing", "2 ads"]),
        c("Cardiology consult", "Heart & vascular", "Maria Reyes", "MR", "core_offer", "planned", 0, 0, "launch Aug 4", "WF-4474", []),
        c("CardioCare program", "Heart & vascular", "Maria Reyes", "MR", "maximizer", "idea", 0, 0, "concept", "", []),
        c("Joint-pain guide", "Orthopedics", "Jon Tran", "JT", "content", "live", 0, 900, "9k views", "WF-5102", ["4 assets"]),
        c("Joint-pain ebook", "Orthopedics", "Jon Tran", "JT", "lead_magnet", "live", 260, 2600, "260 leads", "WF-5103", []),
        c("Free X-ray offer", "Orthopedics", "Jon Tran", "JT", "tripwire", "measured", 180, 4100, "measured · 3.1x ROAS", "WF-5104", []),
        c("Ortho surgeon visit", "Orthopedics", "Jon Tran", "JT", "core_offer", "live", 40, 0, "live", "WF-5105", []),
        c("New-parent hub", "Women's & children's", "Ana Lopez", "AL", "content", "live", 0, 1500, "14k views", "WF-6011", []),
        c("Due-date calculator", "Women's & children's", "Ana Lopez", "AL", "lead_magnet", "live", 531, 3300, "531 leads", "WF-6012", []),
        c("Hospital tour", "Women's & children's", "Ana Lopez", "AL", "tripwire", "planned", 0, 0, "Aug", "WF-6013", []),
        c("Screening checklist", "Cancer", "Dev Patel", "DP", "lead_magnet", "measured", 210, 3900, "measured", "WF-7202", []),
        c("Second opinion", "Cancer", "Dev Patel", "DP", "core_offer", "planned", 0, 0, "Sep", "", []),
        c("Migraine tracker", "Neuroscience", "Maria Reyes", "MR", "lead_magnet", "idea", 0, 0, "concept", "", []),
        c("Neuro consult", "Neuroscience", "Maria Reyes", "MR", "core_offer", "planned", 0, 0, "Sep", "", []),
        c("Wellness score", "Primary care", "Sara Kim", "SK", "lead_magnet", "live", 640, 2900, "640 leads", "WF-8802", []),
        c("$25 annual physical", "Primary care", "Sara Kim", "SK", "tripwire", "live", 120, 3100, "live", "WF-8803", []),
        c("Find a doctor", "Primary care", "Sara Kim", "SK", "core_offer", "live", 90, 0, "live", "WF-8804", []),
    ]


def _ensure_seeded(data: dict) -> dict:
    if not data.get("campaigns"):
        data["campaigns"] = _seed()
        try:
            _atomic_write(data)
        except OSError:
            pass
    return data


# ─── routes ────────────────────────────────────────────────────────

@router.get("/api/campaigns")
async def list_campaigns(
    service_line: Optional[str] = None,
    manager: Optional[str] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
):
    items = _ensure_seeded(_load()).get("campaigns", [])
    if service_line:
        items = [c for c in items if c.get("service_line") == service_line]
    if manager:
        items = [c for c in items if c.get("manager") == manager]
    if stage:
        items = [c for c in items if c.get("stage") == stage]
    if status:
        items = [c for c in items if c.get("status") == status]
    return {"campaigns": items, "count": len(items),
            "stages": STAGES, "stage_labels": STAGE_LABELS, "statuses": STATUSES}


@router.get("/api/campaigns/board")
async def board(manager: Optional[str] = None, service_lines: Optional[str] = None):
    """Rollup for the executive map. Optional filters let a manager view scope to
    their own manager name and/or a comma-separated set of service lines."""
    items = _ensure_seeded(_load()).get("campaigns", [])
    if manager:
        items = [c for c in items if c.get("manager") == manager]
    wanted = [s.strip() for s in service_lines.split(",")] if service_lines else None
    if wanted:
        items = [c for c in items if c.get("service_line") in wanted]

    # group by service line, then stage (preserve first-seen order of lines)
    lines: dict = {}
    for c in items:
        sl = c.get("service_line") or "Unassigned"
        row = lines.setdefault(sl, {"service_line": sl, "managers": [],
                                    "cells": {s: [] for s in STAGES}})
        mi = c.get("manager_initials") or ""
        if mi and mi not in row["managers"]:
            row["managers"].append(mi)
        st = c.get("stage") if c.get("stage") in STAGES else "content"
        row["cells"][st].append({
            "id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
            "stage": st, "manager": c.get("manager"), "metric_note": c.get("metric_note"),
            "leads": c.get("leads", 0), "spend": c.get("spend", 0),
            "workfront_id": c.get("workfront_id", ""),
            "workfront_assets": c.get("workfront_assets", []),
        })

    kpis = {
        "service_lines": len(lines),
        "active": sum(1 for c in items if c.get("status") in ("planned", "live")),
        "live": sum(1 for c in items if c.get("status") == "live"),
        "leads": sum(int(c.get("leads") or 0) for c in items),
        "spend": sum(float(c.get("spend") or 0) for c in items),
        "total": len(items),
    }
    return {
        "kpis": kpis,
        "stages": STAGES,
        "stage_labels": STAGE_LABELS,
        "statuses": STATUSES,
        "rows": list(lines.values()),
    }


@router.get("/api/campaigns/{cid}")
async def get_campaign(cid: str):
    items = _ensure_seeded(_load()).get("campaigns", [])
    c = next((x for x in items if x.get("id") == cid), None)
    if not c:
        return _err(404, "not found", f"No campaign '{cid}'")
    return c


@router.post("/api/campaigns")
async def create_campaign(body: CampaignIn):
    if not body.name.strip():
        return _err(400, "name required", "Campaign needs a name.")
    if body.stage not in STAGES:
        return _err(400, "bad stage", f"stage must be one of {STAGES}")
    if body.status not in STATUSES:
        return _err(400, "bad status", f"status must be one of {STATUSES}")
    with _lock:
        data = _ensure_seeded(_load())
        c = body.model_dump()
        c["id"] = str(uuid.uuid4())
        c["created_at"] = _now()
        c["updated_at"] = _now()
        data.setdefault("campaigns", []).append(c)
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return c


@router.put("/api/campaigns/{cid}")
async def update_campaign(cid: str, body: CampaignUpdate):
    with _lock:
        data = _ensure_seeded(_load())
        c = next((x for x in data.get("campaigns", []) if x.get("id") == cid), None)
        if not c:
            return _err(404, "not found", f"No campaign '{cid}'")
        for field, v in body.model_dump(exclude_unset=True).items():
            if v is not None:
                c[field] = v
        if c.get("stage") not in STAGES:
            return _err(400, "bad stage", f"stage must be one of {STAGES}")
        if c.get("status") not in STATUSES:
            return _err(400, "bad status", f"status must be one of {STATUSES}")
        c["updated_at"] = _now()
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return c


@router.delete("/api/campaigns/{cid}")
async def delete_campaign(cid: str):
    with _lock:
        data = _ensure_seeded(_load())
        before = len(data.get("campaigns", []))
        data["campaigns"] = [x for x in data.get("campaigns", []) if x.get("id") != cid]
        if len(data["campaigns"]) == before:
            return _err(404, "not found", f"No campaign '{cid}'")
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": cid}
