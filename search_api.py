"""
search_api
──────────
Global search across saved queries, reports, knowledge, and metrics. Powers the
Cmd/Ctrl+K command palette. Pure read-only aggregation over existing stores.

Routes (prefix /api/search):
  GET /api/search?q=...&limit=20   — ranked, typed results
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/search", tags=["search"])


def _score(q: str, *fields) -> int:
    """Cheap relevance: exact/prefix/substring on the joined fields."""
    hay = " ".join(str(f or "") for f in fields).lower()
    if not q:
        return 0
    if q == hay:
        return 100
    s = 0
    for tok in q.split():
        if not tok:
            continue
        if hay.startswith(tok):
            s += 12
        elif (" " + tok) in hay:
            s += 8
        elif tok in hay:
            s += 4
    return s


@router.get("")
async def search(q: str = "", limit: int = 20, request: Request = None):
    ql = (q or "").strip().lower()
    if not ql:
        return {"results": [], "count": 0}
    viewer = (request.headers.get("X-Jarvis-User") or "").strip() if request else ""
    out = []

    # Saved queries
    try:
        import queries_api
        for x in queries_api._load().get("queries", []):
            sc = _score(ql, x.get("name"), x.get("description"), (x.get("qdict") or {}).get("primary_table"))
            if sc:
                out.append({"type": "query", "id": x.get("id"), "title": x.get("name") or "(query)",
                            "subtitle": (x.get("qdict") or {}).get("primary_table") or "saved query", "score": sc})
    except Exception:
        pass

    # Reports
    try:
        import reports_api
        for x in reports_api._load().get("reports", []):
            sc = _score(ql, x.get("name"), x.get("description"))
            if sc:
                out.append({"type": "report", "id": x.get("id"), "title": x.get("name") or "(report)",
                            "subtitle": f"{len(x.get('pages') or [])} page(s)", "score": sc})
    except Exception:
        pass

    # Knowledge
    try:
        import knowledge_api
        for x in knowledge_api._load().get("items", []):
            sc = _score(ql, x.get("summary"), x.get("content"))
            if sc:
                out.append({"type": "knowledge", "id": x.get("id"), "title": x.get("summary") or "(note)",
                            "subtitle": "knowledge · " + (x.get("usage_mode") or "auto"), "score": sc})
    except Exception:
        pass

    # Alerts
    try:
        import alerts as _alerts
        for a in _alerts._load().get("alerts", []):
            sc = _score(ql, a.get("name"), " ".join(a.get("tags") or []), a.get("query_name"))
            if sc:
                out.append({"type": "alert", "id": a.get("id"), "title": a.get("name") or "(alert)",
                            "subtitle": "alert · " + (a.get("last_status") or "idle"), "score": sc})
    except Exception:
        pass

    # Metrics (calculated + per-table metric fields from the semantic model)
    try:
        from semantic import load_model
        model = load_model()
        for m in (getattr(model, "calculated_metrics", {}) or {}).values():
            sc = _score(ql, getattr(m, "key", ""), getattr(m, "display_name", ""), getattr(m, "description", ""))
            if sc:
                out.append({"type": "metric", "id": getattr(m, "key", ""),
                            "title": getattr(m, "display_name", "") or getattr(m, "key", ""),
                            "subtitle": "calculated metric", "score": sc + 1})
    except Exception:
        pass

    out.sort(key=lambda r: r["score"], reverse=True)
    n = max(1, min(int(limit or 20), 50))
    return {"results": out[:n], "count": len(out)}
