"""Martin CDP API (prefix /api/cdp).

Collection · identity · profiles · segmentation · activation · journeys — the six
CDP pillars over `cdp_core`. Ingest accepts only de-identified, consent-bearing
events (PHI guard + consent gate applied in the core).

Endpoints:
  POST /api/cdp/track|identify|batch      — ingest events
  GET  /api/cdp/profiles[/{pid}]          — Customer 360 profiles
  GET  /api/cdp/stats                     — CDP overview
  GET/POST/DELETE /api/cdp/segments       — segment builder
  POST /api/cdp/segments/{id}/preview     — live count + sample
  GET/POST/DELETE /api/cdp/destinations   — destinations framework
  POST /api/cdp/activate                  — audience → destination (dry-run default)
  GET/POST/DELETE /api/cdp/journeys       — event-triggered journeys
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

import cdp_core as cdp

router = APIRouter(prefix="/api/cdp", tags=["cdp"])


def _err(status, error, detail=""):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


class Event(BaseModel):
    event: Optional[str] = None
    type: Optional[str] = None
    anonymousId: Optional[str] = None
    userId: Optional[str] = None
    externalId: Optional[str] = None
    email: Optional[str] = None
    time: Optional[object] = None
    traits: Optional[dict] = None
    properties: Optional[dict] = None
    context: Optional[dict] = None
    batch: Optional[list] = None


@router.post("/track")
async def track(e: Event):
    return cdp.ingest(e.dict(exclude_none=True), "track")


@router.post("/identify")
async def identify(e: Event):
    return cdp.ingest(e.dict(exclude_none=True), "identify")


@router.post("/batch")
async def batch(e: Event):
    items = e.batch or []
    out = [cdp.ingest(p, (p.get("type") or "track")) for p in items]
    return {"accepted": sum(1 for o in out if o.get("accepted")), "total": len(out), "results": out}


@router.get("/stats")
async def stats():
    return cdp.stats()


@router.get("/profiles")
async def profiles(limit: int = 100):
    return {"profiles": cdp.list_profiles(limit)}


@router.get("/profiles/{pid}")
async def profile(pid: str):
    p = cdp.get_profile(pid)
    return {"profile": p} if p else _err(404, "not found", pid)


# ── segments ──────────────────────────────────────────────────────────────
class Segment(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    match: Optional[str] = "all"
    rules: list


@router.get("/segments")
async def segments():
    return {"segments": cdp.list_segments()}


@router.post("/segments")
async def save_segment(s: Segment):
    saved = cdp.upsert_segment(s.dict(exclude_none=True))
    ev = cdp.evaluate_segment(saved)
    return {"ok": True, "segment": saved, "count": ev["count"], "meets_min_cohort": ev["meets_min_cohort"]}


@router.delete("/segments/{sid}")
async def del_segment(sid: str):
    return {"ok": cdp.delete_segment(sid)}


class PreviewBody(BaseModel):
    match: Optional[str] = "all"
    rules: list
    require_marketing_consent: Optional[bool] = False


@router.post("/segments/preview")
async def preview_segment(b: PreviewBody):
    return cdp.evaluate_segment(b.dict(exclude_none=True), require_marketing_consent=bool(b.require_marketing_consent))


# ── destinations + activation ───────────────────────────────────────────────
class Destination(BaseModel):
    id: Optional[str] = None
    name: str
    type: Optional[str] = "webhook"
    config: Optional[dict] = None
    consent_required: Optional[bool] = True


@router.get("/destinations")
async def destinations():
    return {"destinations": cdp.list_destinations(), "types": cdp._DEST_TYPES}


@router.post("/destinations")
async def save_destination(d: Destination):
    return {"ok": True, "destination": cdp.upsert_destination(d.dict(exclude_none=True))}


@router.delete("/destinations/{did}")
async def del_destination(did: str):
    return {"ok": cdp.delete_destination(did)}


class ActivateBody(BaseModel):
    destination_id: str
    segment_id: Optional[str] = None
    rules: Optional[list] = None
    dry_run: Optional[bool] = True


@router.post("/activate")
async def activate(b: ActivateBody):
    dest = next((x for x in cdp.list_destinations() if x.get("id") == b.destination_id), None)
    if not dest:
        return _err(404, "unknown destination", b.destination_id)
    if b.segment_id:
        seg = next((s for s in cdp.list_segments() if s.get("id") == b.segment_id), None)
        if not seg:
            return _err(404, "unknown segment", b.segment_id)
        members = cdp.evaluate_segment(seg)["member_ids"]
    elif b.rules:
        members = cdp.evaluate_segment({"rules": b.rules})["member_ids"]
    else:
        return _err(400, "no audience", "provide segment_id or rules")
    return {"audience_size": len(members), "activation": cdp.activate(dest, members, dry_run=bool(b.dry_run))}


# ── journeys ────────────────────────────────────────────────────────────────
class Journey(BaseModel):
    id: Optional[str] = None
    name: str
    trigger_event: Optional[str] = ""
    enabled: Optional[bool] = False
    steps: Optional[list] = None


@router.get("/journeys")
async def journeys():
    return {"journeys": cdp.list_journeys()}


@router.post("/journeys")
async def save_journey(j: Journey):
    return {"ok": True, "journey": cdp.upsert_journey(j.dict(exclude_none=True))}


@router.delete("/journeys/{jid}")
async def del_journey(jid: str):
    return {"ok": cdp.delete_journey(jid)}


@router.get("/journeys/enrollments")
async def enrollments(journey_id: str = None, limit: int = 100):
    return {"enrollments": cdp.list_enrollments(journey_id, limit)}


@router.post("/journeys/tick")
async def tick():
    """Resume any journey enrollments whose wait has elapsed (scheduler hook)."""
    return cdp.tick_journeys()


# ── warehouse-native (BigQuery) ──────────────────────────────────────────────
@router.get("/warehouse/status")
async def warehouse_status():
    return cdp.warehouse_status()


@router.get("/warehouse/profiles_sql")
async def profiles_sql():
    """Governed SQL that builds Customer 360 profiles in the warehouse."""
    return {"sql": cdp._profiles_sql(), "events_table": cdp.EVENTS_TABLE}


class SegSql(BaseModel):
    rules: list
    match: Optional[str] = "all"


@router.post("/warehouse/segment_sql")
async def segment_sql(b: SegSql):
    """Compile a segment to governed BigQuery SQL (runs in-warehouse; PHI stays put)."""
    return cdp.segment_sql(b.rules, match=b.match or "all")
