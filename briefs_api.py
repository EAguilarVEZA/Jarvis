"""
briefs_api
──────────
Briefs Library — saved AI strategic briefs that re-run on open so the
data stays fresh. Persisted to ~/jarvis/semantic/briefs.json (atomic
writes + timestamped backups, same pattern as views.json).

Routes (prefix /api/briefs):
  GET    /api/briefs            — list briefs (summaries)
  GET    /api/briefs/{id}       — full brief (prompt + cached widget/brief)
  POST   /api/briefs            — save a brief (the AI response from /ask)
  DELETE /api/briefs/{id}       — delete

Re-running is the client's job: it takes the brief's `prompt` and POSTs
to /api/semantic/ask, so the brief is always fresh against live BigQuery.
"""

from __future__ import annotations

import json
import os
import time
import uuid
import shutil
import threading
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

router = APIRouter(prefix="/api/briefs", tags=["briefs"])

BRIEFS_PATH = SEMANTIC_DIR / "briefs.json"
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not BRIEFS_PATH.exists():
        return {"briefs": []}
    try:
        with open(BRIEFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "briefs" not in data:
            return {"briefs": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"briefs": []}


def _atomic_write(data: dict):
    BRIEFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BRIEFS_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(BRIEFS_PATH, BRIEFS_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = BRIEFS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(BRIEFS_PATH)


def _summary(b: dict) -> dict:
    return {
        "id": b.get("id"),
        "title": b.get("title"),
        "prompt": b.get("prompt"),
        "headline": (b.get("brief") or {}).get("headline"),
        "created_at": b.get("created_at"),
        "updated_at": b.get("updated_at"),
    }


class BriefCreate(BaseModel):
    title: Optional[str] = None
    prompt: str
    widget: Optional[dict] = None
    brief: Optional[dict] = None


@router.get("")
async def list_briefs():
    data = _load()
    items = [_summary(b) for b in data["briefs"]]
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return {"briefs": items, "count": len(items)}


@router.get("/{brief_id}")
async def get_brief(brief_id: str):
    data = _load()
    b = next((x for x in data["briefs"] if x.get("id") == brief_id), None)
    if not b:
        return _err(404, "not found", f"No brief '{brief_id}'")
    return b


@router.post("")
async def create_brief(body: BriefCreate):
    if not body.prompt.strip():
        return _err(400, "prompt required", "Saved briefs need a prompt to re-run.")
    with _lock:
        data = _load()
        # If we already have a brief with the same prompt, update its title/snapshot instead.
        existing = next((x for x in data["briefs"] if (x.get("prompt") or "").strip() == body.prompt.strip()), None)
        if existing:
            if body.title: existing["title"] = body.title
            if body.widget is not None: existing["widget"] = body.widget
            if body.brief is not None: existing["brief"] = body.brief
            existing["updated_at"] = _now()
            try:
                _atomic_write(data)
            except OSError as e:
                return _err(500, "save failed", str(e))
            return existing
        b = {
            "id": str(uuid.uuid4()),
            "title": (body.title or body.prompt)[:120],
            "prompt": body.prompt.strip(),
            "widget": body.widget or {},
            "brief": body.brief or {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["briefs"].append(b)
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return b


class AskRequest(BaseModel):
    question: str
    start: str
    end: str
    save: bool = False
    title: Optional[str] = None


@router.post("/ask")
async def ask_brief(body: AskRequest):
    """Ask a question in natural language → generate a grounded brief via ktx + Claude.

    Plans the query from approved ktx measures (Haiku), executes through the
    semantic layer (ktx when JARVIS_SQL_ENGINE=ktx), and writes a brief with
    caveats and cited sources (Sonnet). Optionally persists it to the library.
    """
    if not body.question.strip():
        return _err(400, "question required", "Ask a question to generate a brief.")
    if not body.start or not body.end:
        return _err(400, "date range required", "Provide start and end dates (YYYY-MM-DD) — the data spans 2014–2043, so a range is required.")
    try:
        from brief_engine import generate_brief
        brief = await generate_brief(body.question.strip(), body.start, body.end)
    except Exception as e:  # noqa: BLE001
        return _err(500, "brief generation failed", str(e))

    if body.save:
        with _lock:
            data = _load()
            b = {
                "id": str(uuid.uuid4()),
                "title": (body.title or body.question)[:120],
                "prompt": body.question.strip(),
                "widget": {"chart": brief.get("chart")},
                "brief": brief,
                "created_at": _now(),
                "updated_at": _now(),
            }
            data["briefs"].append(b)
            try:
                _atomic_write(data)
            except OSError as e:
                return _err(500, "save failed", str(e))
        return {"saved": True, "brief": b}
    return {"saved": False, "brief": brief}


@router.delete("/{brief_id}")
async def delete_brief(brief_id: str):
    with _lock:
        data = _load()
        before = len(data["briefs"])
        data["briefs"] = [x for x in data["briefs"] if x.get("id") != brief_id]
        if len(data["briefs"]) == before:
            return _err(404, "not found", f"No brief '{brief_id}'")
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": brief_id}
