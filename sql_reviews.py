"""
sql_reviews — a log of AI/user-generated SQL for admin review (Kaelio /admin/sql-reviews parity).

Every statement run through the raw-SQL endpoint is recorded here (deduped by
normalized SQL) with a status, a complexity estimate, the tables it touches, the
user who ran it, and any error / policy-block reason. Admins can browse, filter,
and approve / reject / mark-reviewed.

State: sql_reviews.json under the semantic dir. Capped at _MAX entries (oldest
non-pending entries drop first) so it never grows unbounded.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

_lock = threading.Lock()
_PATH = SEMANTIC_DIR / "sql_reviews.json"
_MAX = 500
_STATUSES = ("pending", "approved", "rejected", "reviewed")

_TABLE_RE = re.compile(r"\b(?:from|join)\s+([`\"'\[]?[\w.\]]+)", re.I)


def _load() -> list:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip()).lower()


def _tables(sql: str) -> list:
    out = []
    for raw in _TABLE_RE.findall(sql or ""):
        name = raw.strip("`\"'[]")
        if name and name not in out:
            out.append(name)
    return out


def _complexity(sql: str) -> str:
    s = (sql or "").lower()
    score = s.count(" join ") * 2 + max(0, s.count("select") - 1) * 2
    score += 1 if len(sql or "") > 600 else 0
    score += s.count(" union ") + s.count(" over(") + s.count(" over (")
    return "high" if score >= 5 else ("medium" if score >= 2 else "low")


def record(sql: str, user: Optional[str] = None, error: Optional[str] = None,
           blocked: bool = False, rows: Optional[int] = None) -> None:
    """Best-effort log of one executed (or blocked) statement. Never raises."""
    try:
        rid = hashlib.sha1(_norm(sql).encode("utf-8")).hexdigest()[:16]
        now = time.time()
        with _lock:
            items = _load()
            existing = next((x for x in items if x.get("id") == rid), None)
            if existing:
                existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
                existing["last_seen"] = now
                existing["last_user"] = user
                existing["error"] = error
                existing["blocked"] = bool(blocked)
                if rows is not None:
                    existing["rows"] = rows
            else:
                items.insert(0, {
                    "id": rid,
                    "sql": sql,
                    "user": user,
                    "status": "pending",
                    "complexity": _complexity(sql),
                    "tables": _tables(sql),
                    "error": error,
                    "blocked": bool(blocked),
                    "rows": rows,
                    "seen_count": 1,
                    "created_at": now,
                    "last_seen": now,
                })
                # Cap: drop oldest non-pending first, then oldest.
                if len(items) > _MAX:
                    items.sort(key=lambda x: (x.get("status") == "pending", x.get("last_seen", 0)), reverse=True)
                    items[:] = items[:_MAX]
            _save(items)
    except Exception:
        pass


# ── admin API ───────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/admin/sql-reviews", tags=["sql-reviews"])


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


@router.get("")
async def list_reviews(status: Optional[str] = None, complexity: Optional[str] = None,
                       search: Optional[str] = None, limit: int = 50, offset: int = 0):
    items = _load()
    if status and status != "all":
        items = [x for x in items if x.get("status") == status]
    if complexity and complexity != "all":
        items = [x for x in items if x.get("complexity") == complexity]
    if search:
        s = search.lower()
        items = [x for x in items if s in (x.get("sql", "").lower())
                 or any(s in t.lower() for t in x.get("tables", []))]
    items.sort(key=lambda x: x.get("last_seen", 0), reverse=True)
    total = len(items)
    page = items[offset:offset + max(1, min(limit, 200))]
    # Trim the SQL in the list payload; the detail endpoint returns the full text.
    slim = [{**{k: v for k, v in x.items() if k != "sql"},
             "snippet": (x.get("sql", "")[:160])} for x in page]
    counts = {}
    for x in _load():
        counts[x.get("status", "pending")] = counts.get(x.get("status", "pending"), 0) + 1
    return {"reviews": slim, "total": total, "counts": counts}


@router.get("/{rid}")
async def get_review(rid: str):
    x = next((r for r in _load() if r.get("id") == rid), None)
    if not x:
        return _err(404, "not found", "No such review.")
    return x


class StatusBody(BaseModel):
    status: str


@router.put("/{rid}/status")
async def set_status(rid: str, body: StatusBody):
    st = (body.status or "").lower()
    if st not in _STATUSES:
        return _err(400, "bad status", f"Status must be one of {_STATUSES}.")
    with _lock:
        items = _load()
        x = next((r for r in items if r.get("id") == rid), None)
        if not x:
            return _err(404, "not found", "No such review.")
        x["status"] = st
        x["reviewed_at"] = time.time()
        _save(items)
    return {"ok": True, "id": rid, "status": st}
