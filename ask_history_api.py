"""
ask_history_api — persistent memory for Ask AI questions (per-user + shared)
─────────────────────────────────────────────────────────────────────────────
Backs the memory sidebar on Home like Kaelio's: "My" / "All" tabs.

Every Ask AI exchange is recorded with:
  • author (user_id + email)
  • question + answer
  • scope: private | group | corporate | pending
  • shared_with: list of group_ids (when scope=group)
  • visible_to: computed access list

Visibility rules (enforced in /list):
  • private     → only the author sees it
  • group       → author + anyone in any shared_with group sees it
  • corporate   → everyone sees it
  • pending     → only the author + admins see it (queued for approval)

Routes (prefix /api/ask_history):
  GET   /                       — list questions, scoped by caller (?filter=mine|all|shared)
  POST  /                       — record a question/answer
  GET   /{id}                   — get one (visibility-checked)
  PUT   /{id}/share             — change the scope and groups
  DELETE/{id}                   — delete (author-only or admin)
"""

from __future__ import annotations
import json, os, time, uuid, shutil, threading
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Reuse the users_api helpers for current-user resolution
from users_api import _current_user, _load_users   # noqa

router = APIRouter(prefix="/api/ask_history", tags=["ask_history"])

CFG_DIR = Path(os.path.expanduser("~/.config/jarvis"))
STORE   = CFG_DIR / "ask_history.json"
_lock   = threading.Lock()


def _load() -> dict:
    if not STORE.exists(): return {"items": []}
    try: return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError): return {"items": []}


def _save(data: dict):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE)
    try: os.chmod(STORE, 0o600)
    except OSError: pass


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _visible(item: dict, me: dict) -> bool:
    if me.get("role") == "admin": return True   # admins see everything
    if item.get("author_id") == me.get("id"): return True
    scope = item.get("scope", "private")
    if scope == "corporate": return True
    if scope == "group":
        my_groups = set(me.get("groups") or [])
        return bool(set(item.get("shared_with") or []) & my_groups)
    return False   # private + pending hidden from non-author non-admin


class AskItemIn(BaseModel):
    question: str
    answer: Optional[str] = ""
    scope: str = "private"               # private | group | corporate | pending
    shared_with: list[str] = []          # group ids (when scope='group')
    chart_spec: Optional[dict] = None    # optional preview (chart type, data, etc.)


class ShareIn(BaseModel):
    scope: str
    shared_with: list[str] = []


@router.get("")
async def list_history(
    filter: str = "mine",          # mine | all | shared | pending
    limit: int = 200,
    x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")
):
    me = _current_user(x_jarvis_user)
    items = _load().get("items", [])
    # Most-recent first
    items = sorted(items, key=lambda x: -x.get("at", 0))
    out = []
    for it in items:
        if filter == "mine":
            if it.get("author_id") != me.get("id"): continue
        elif filter == "all":
            if me.get("role") != "admin": continue   # only admins see "all"
        elif filter == "shared":
            if it.get("author_id") == me.get("id"): continue
            if not _visible(it, me): continue
        elif filter == "pending":
            if me.get("role") != "admin": continue
            if it.get("scope") != "pending": continue
        else:
            if not _visible(it, me): continue
        out.append(it)
        if len(out) >= limit: break
    return {"items": out, "count": len(out), "filter": filter, "me": {"id": me.get("id"), "role": me.get("role")}}


@router.post("")
async def record_question(body: AskItemIn, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    if body.scope not in ("private","group","corporate","pending"):
        return _err(400, "invalid scope")
    # Non-admins cannot publish corporate directly — convert to pending
    scope = body.scope
    if scope == "corporate" and me.get("role") != "admin":
        scope = "pending"
    with _lock:
        data = _load()
        it = {
            "id": str(uuid.uuid4()),
            "at": time.time(),
            "author_id": me.get("id"),
            "author_email": me.get("email"),
            "author_name": me.get("name"),
            "question": body.question,
            "answer": body.answer,
            "scope": scope,
            "shared_with": body.shared_with,
            "chart_spec": body.chart_spec,
        }
        data["items"].append(it)
        _save(data)
    return it


@router.get("/{aid}")
async def get_item(aid: str, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    it = next((x for x in _load().get("items", []) if x["id"] == aid), None)
    if not it: return _err(404, "not found")
    if not _visible(it, me): return _err(403, "forbidden")
    return it


@router.put("/{aid}/share")
async def share_item(aid: str, body: ShareIn, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    if body.scope not in ("private","group","corporate","pending"):
        return _err(400, "invalid scope")
    with _lock:
        data = _load()
        it = next((x for x in data["items"] if x["id"] == aid), None)
        if not it: return _err(404, "not found")
        # Author or admin only
        if it.get("author_id") != me.get("id") and me.get("role") != "admin":
            return _err(403, "forbidden", "Only the author or an admin can change share scope")
        # Non-admin upgrading to corporate → queue as pending
        new_scope = body.scope
        if new_scope == "corporate" and me.get("role") != "admin":
            new_scope = "pending"
        it["scope"] = new_scope
        it["shared_with"] = body.shared_with
        it["updated_at"] = time.time()
        _save(data)
    return it


@router.delete("/{aid}")
async def delete_item(aid: str, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    with _lock:
        data = _load()
        it = next((x for x in data["items"] if x["id"] == aid), None)
        if not it: return _err(404, "not found")
        if it.get("author_id") != me.get("id") and me.get("role") != "admin":
            return _err(403, "forbidden")
        data["items"] = [x for x in data["items"] if x["id"] != aid]
        _save(data)
    return {"ok": True, "deleted": aid}
