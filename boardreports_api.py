"""
boardreports_api
────────────────
In-app Board Reports: connect a data source (a Looker/GA4 dashboard, or just a
named report), then have Martin build a print-ready, on-brand PDF from confirmed
figures. The orlando-health-reports skill, brought inside Martin.

  GET  /api/boardreports/sources        list connected report sources
  POST /api/boardreports/sources        add/update a source
  POST /api/boardreports/sources/delete remove a source {id}
  POST /api/boardreports/build          build a report -> {id, pdf_url}
  GET  /api/boardreports/file/{id}      stream the rendered PDF
"""
from __future__ import annotations

import os
import re
import json
import time
import logging

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import reports_studio

router = APIRouter(prefix="/api/boardreports", tags=["boardreports"])
log = logging.getLogger("boardreports_api")

_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardreports_sources.json")


def _load():
    try:
        with open(_STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sources": []}


def _save(d):
    try:
        with open(_STORE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log.warning("save sources failed: %s", e)


def _seed():
    """Ship example sources so the flow is discoverable on day one, and BACKFILL the
    GA4 hostname onto any sources saved before the host field existed."""
    d = _load()
    changed = False
    _HOSTS = {"baptist-web-traffic": "www.baptisthealthal.com",
              "orlando-web-traffic": "www.orlandohealth.com"}
    for s in d.get("sources", []):
        if not s.get("host") and s.get("id") in _HOSTS:
            s["host"] = _HOSTS[s["id"]]
            changed = True
    have = {s.get("id") for s in d.get("sources", [])}
    if d.get("sources") and "orlando-web-traffic" not in have:
        d["sources"].append({"id": "orlando-web-traffic", "name": "Orlando Health — Website Traffic",
                              "entity": "Orlando Health", "template": "corporate",
                              "host": "www.orlandohealth.com", "dashboard_url": ""})
        changed = True
    if changed:
        _save(d)
    if not d.get("sources"):
        d["sources"] = [
            {"id": "baptist-web-traffic", "name": "Baptist Health — Website Traffic",
             "entity": "Baptist Health", "template": "corporate",
             "host": "www.baptisthealthal.com",
             "dashboard_url": "https://datastudio.google.com/reporting/6c0e74b3-c126-4775-b861-f93273fb6727"},
            {"id": "orlando-web-traffic", "name": "Orlando Health — Website Traffic",
             "entity": "Orlando Health", "template": "corporate",
             "host": "www.orlandohealth.com", "dashboard_url": ""},
        ]
        _save(d)
    return d


class Source(BaseModel):
    id: str = ""
    name: str = ""
    entity: str = "Orlando Health"
    template: str = "corporate"
    dashboard_url: str = ""
    host: str = ""           # GA4 hostname (e.g. www.baptisthealthal.com) -> auto-pull
    query_id: str = ""


class BuildRequest(BaseModel):
    entity: str = "Orlando Health"
    template: str = "corporate"
    brief: str = ""          # what the report is about (its title/topic)
    data: str = ""           # the confirmed figures to include
    host: str = ""           # if set, auto-pull real GA4 traffic for this hostname
    timeframe: str = "fiscal ytd"   # fiscal ytd | last 30 days | last 90 days | last 12 months | ytd
    page: str = ""           # optional page-path filter, e.g. /careers
    source: str = ""         # e.g. "GA4 via Internal MarTech Looker Studio"
    asof: str = ""           # refresh / as-of date
    source_id: str = ""


@router.get("/sources")
async def list_sources():
    return _seed()


@router.post("/sources")
async def add_source(body: Source):
    d = _load()
    sid = body.id or reports_studio._slug(body.name)
    src = body.dict(); src["id"] = sid
    d["sources"] = [s for s in d.get("sources", []) if s.get("id") != sid] + [src]
    _save(d)
    return {"ok": True, "id": sid, "sources": d["sources"]}


@router.post("/sources/delete")
async def delete_source(body: Source):
    d = _load()
    d["sources"] = [s for s in d.get("sources", []) if s.get("id") != body.id]
    _save(d)
    return {"ok": True, "sources": d["sources"]}


@router.post("/build")
async def build(body: BuildRequest):
    brief = (body.brief or "").strip()
    data = (body.data or "").strip()
    src_line = body.source
    if len(brief) < 3:
        return {"ok": False, "answer": "Tell me what the report should cover."}
    # HOSTNAME path: pull real GA4 traffic and render DETERMINISTICALLY (pure Python
    # from the structured numbers) — NO LLM call, zero tokens.
    if body.host:
        try:
            import ga4_traffic
            ds = ga4_traffic.build_dataset(body.host, timeframe=(body.timeframe or "fiscal ytd"), page=(body.page or None))
            res = await reports_studio.build_traffic_report(body.entity, brief or "Website Traffic", ds)
        except Exception as e:
            log.warning("ga4 traffic report failed for %s: %s", body.host, e)
            return {"ok": False, "answer": f"I couldn’t build the GA4 traffic report for {body.host}: {e}"}
        return {"ok": True, "id": res["id"], "title": f"{body.entity} — {brief or 'Website Traffic'}",
                "has_pdf": res.get("has_pdf"),
                "pdf_url": f"/api/boardreports/file/{res['id']}" if res.get("has_pdf") else None,
                "html_url": f"/api/boardreports/file/{res['id']}?fmt=html"}

    # FREEFORM path (pasted numbers, non-traffic): uses the model to lay it out.
    if len(data) < 8:
        return {"ok": False, "answer": "No figures to build from — give me a GA4 hostname to auto-pull, or paste the key numbers."}
    try:
        res = await reports_studio.generate_report(
            entity=body.entity, template=body.template, brief=brief,
            data=data, source=src_line, asof=body.asof)
    except Exception as e:
        log.warning("build failed: %s", e)
        return {"ok": False, "error": str(e)}
    if not res.get("ok"):
        return res
    return {"ok": True, "id": res["id"], "title": f"{body.entity} — {brief}",
            "has_pdf": res.get("has_pdf"),
            "pdf_url": f"/api/boardreports/file/{res['id']}" if res.get("has_pdf") else None,
            "html_url": f"/api/boardreports/file/{res['id']}?fmt=html"}


@router.get("/file/{rid}")
async def file(rid: str, fmt: str = "pdf"):
    # Use the EXACT id (only strip path-traversal chars) — do NOT re-slug/truncate,
    # or the lookup won't match the saved filename.
    safe = re.sub(r"[^A-Za-z0-9._-]", "", rid or "")
    base = reports_studio.REPORTS_DIR
    if fmt == "html":
        p = os.path.join(base, safe + ".html")
        if os.path.exists(p):
            return FileResponse(p, media_type="text/html")
    p = os.path.join(base, safe + ".pdf")
    if os.path.exists(p):
        return FileResponse(p, media_type="application/pdf", filename=safe + ".pdf")
    ph = os.path.join(base, safe + ".html")   # fall back to HTML if the PDF didn't render
    if os.path.exists(ph):
        return FileResponse(ph, media_type="text/html")
    return JSONResponse({"error": "not found", "id": safe}, status_code=404)
