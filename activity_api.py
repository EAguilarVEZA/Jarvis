"""
activity_api
────────────
A cross-cutting activity feed: recent comments, alert fires, and report email
deliveries in one reverse-chronological stream. Read-only aggregation over the
existing JSON stores — no new storage.

Routes (prefix /api/activity):
  GET /api/activity?limit=50   — merged, newest-first activity items
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _reports():
    try:
        import reports_api
        return reports_api._load().get("reports", [])
    except Exception:
        return []


def _alerts():
    try:
        import alerts
        return alerts._load().get("alerts", [])
    except Exception:
        return []


def _schedules():
    try:
        import report_email
        return report_email._load().get("schedules", [])
    except Exception:
        return []


@router.get("")
async def activity(limit: int = 50, report_id: Optional[str] = None, format: Optional[str] = None):
    items = []
    rname = {}

    for r in _reports():
        rid = r.get("id")
        rname[rid] = r.get("name")
        for c in (r.get("comments") or []):
            items.append({
                "kind": "comment",
                "at": c.get("at") or 0,
                "report_id": rid,
                "report_name": r.get("name"),
                "author": c.get("author"),
                "text": (c.get("text") or "")[:240],
                "resolved": bool(c.get("resolved")),
                "is_reply": bool(c.get("parent_id")),
            })

    for a in _alerts():
        # Emit an item for each firing point in the alert's recent history.
        for h in (a.get("history") or []):
            if not h.get("fired"):
                continue
            items.append({
                "kind": "alert",
                "at": h.get("at") or 0,
                "alert_id": a.get("id"),
                "alert_name": a.get("name"),
                "value": h.get("value"),
                "condition": f"{a.get('column') or 'value'} {a.get('op')} {a.get('threshold')}",
            })

    for s in _schedules():
        if s.get("last_run"):
            items.append({
                "kind": "email",
                "at": s.get("last_run") or 0,
                "report_id": s.get("report_id"),
                "report_name": rname.get(s.get("report_id")) or s.get("report_name") or "(report)",
                "recipients": s.get("recipients"),
                "status": s.get("last_status"),
                "detail": s.get("last_detail"),
            })

    if report_id:
        items = [x for x in items if x.get("report_id") == report_id]

    items.sort(key=lambda x: x.get("at") or 0, reverse=True)
    n = max(1, min(int(limit or 50), 1000))
    items = items[:n]

    if (format or "").lower() == "csv":
        import csv
        import io
        import time as _t
        from fastapi.responses import Response
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["when", "kind", "report_or_alert", "who", "detail"])
        for it in items:
            when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(it.get("at") or 0))
            if it.get("kind") == "comment":
                w.writerow([when, "comment", it.get("report_name", ""), it.get("author", ""), (it.get("text") or "")[:500]])
            elif it.get("kind") == "alert":
                w.writerow([when, "alert", it.get("alert_name", ""), "", f"{it.get('condition','')} = {it.get('value','')}"])
            elif it.get("kind") == "email":
                w.writerow([when, "email", it.get("report_name", ""), it.get("recipients", ""), it.get("status", "")])
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=activity.csv"})

    return {"items": items, "count": len(items)}
