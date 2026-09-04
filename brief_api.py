"""
brief_api.py
────────────
Delivery channel for Martin's Daily Brief. The scheduled routine reads Outlook,
builds a brief (see daily_brief.py), and POSTs it here. The pill polls /latest
and pops the newest unseen brief as a card at 8am / noon / 5pm.

  POST /api/brief/push     {slot, date_label, as_of, include_calendar, emails, meetings}
                           -> renders deterministically, stores as latest, returns {ok,id}
  POST /api/brief/push_html {slot, html, title}   (already-rendered escape hatch)
  GET  /api/brief/latest   -> newest brief {id, slot, html, ts, seen}
  POST /api/brief/seen     {id}  -> mark shown so it won't pop again
  GET  /api/brief/history  -> last 20 briefs (metadata)
"""
from __future__ import annotations

import os
import json
import time
import logging

from fastapi import APIRouter
from pydantic import BaseModel

import daily_brief
try:
    import teams_webhook
except Exception:
    teams_webhook = None

router = APIRouter(prefix="/api/brief", tags=["brief"])
log = logging.getLogger("brief_api")

_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_store.json")
_MAX = 20


def _load():
    try:
        with open(_STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"briefs": [], "seen": []}


def _save(d):
    try:
        with open(_STORE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log.warning("brief save failed: %s", e)


class PushRequest(BaseModel):
    slot: str = "morning"          # morning | midday | evening
    date_label: str = ""
    as_of: str = ""
    include_calendar: bool = True
    emails: list = []
    meetings: list = []


class PushHtmlRequest(BaseModel):
    slot: str = "morning"
    title: str = ""
    html: str = ""


class SeenRequest(BaseModel):
    id: str = ""


def _maybe_post_teams(d, entry):
    """Post a brief to the Teams channel once (from the Mac backend, which has
    reliable internet). Guarded by teams_posted so we never double-send."""
    if not teams_webhook or not teams_webhook.enabled():
        return False
    posted = d.setdefault("teams_posted", [])
    if entry.get("id") in posted:
        return False
    text = entry.get("text") or ""
    if teams_webhook.post(f"Martin — {entry.get('slot','')} brief", text):
        posted.append(entry["id"]); d["teams_posted"] = posted[-50:]
        return True
    return False


def _store_brief(slot, html, text=""):
    d = _load()
    bid = f"{slot}-{int(time.time())}"
    entry = {"id": bid, "slot": slot, "html": html, "text": text, "ts": time.time()}
    d["briefs"] = ([entry] + d.get("briefs", []))[:_MAX]
    _maybe_post_teams(d, entry)
    _save(d)
    return bid


@router.post("/push")
async def push(body: PushRequest):
    brief = {
        "slot": body.slot, "date_label": body.date_label, "as_of": body.as_of,
        "include_calendar": body.include_calendar,
        "emails": body.emails or [], "meetings": body.meetings or [],
    }
    try:
        html = daily_brief.render(brief)
        text = daily_brief.render_text(brief)
    except Exception as e:
        log.warning("brief render failed: %s", e)
        return {"ok": False, "error": str(e)}
    bid = _store_brief(body.slot, html, text)
    return {"ok": True, "id": bid}


@router.post("/push_html")
async def push_html(body: PushHtmlRequest):
    if len(body.html or "") < 10:
        return {"ok": False, "error": "empty html"}
    bid = _store_brief(body.slot, body.html)
    return {"ok": True, "id": bid}


@router.get("/latest")
async def latest():
    d = _load()
    briefs = d.get("briefs", [])
    if not briefs:
        return {"ok": True, "brief": None}
    b = briefs[0]
    # Scheduled runs write the store file directly (bypassing /push). Post to
    # Teams here — from the Mac, which has internet — the first time we see it.
    if _maybe_post_teams(d, b):
        _save(d)
    return {"ok": True, "brief": {
        "id": b["id"], "slot": b["slot"], "html": b["html"], "ts": b["ts"],
        "seen": b["id"] in d.get("seen", []),
    }}


@router.post("/seen")
async def seen(body: SeenRequest):
    d = _load()
    if body.id and body.id not in d.get("seen", []):
        d.setdefault("seen", []).append(body.id)
        d["seen"] = d["seen"][-50:]
        _save(d)
    return {"ok": True}


class MuteRequest(BaseModel):
    sender: str = ""
    title: str = ""


@router.post("/mute")
async def mute(body: MuteRequest):
    daily_brief.add_muted(body.sender)
    return {"ok": True, "senders": daily_brief.load_muted()}


@router.post("/unmute")
async def unmute(body: MuteRequest):
    daily_brief.remove_muted(body.sender)
    return {"ok": True, "senders": daily_brief.load_muted()}


@router.post("/mute_meeting")
async def mute_meeting(body: MuteRequest):
    daily_brief.add_muted_meeting(body.title)
    return {"ok": True, "meetings": daily_brief.load_muted_meetings()}


@router.post("/unmute_meeting")
async def unmute_meeting(body: MuteRequest):
    daily_brief.remove_muted_meeting(body.title)
    return {"ok": True, "meetings": daily_brief.load_muted_meetings()}


@router.get("/muted")
async def muted():
    return {"ok": True, "senders": daily_brief.load_muted(),
            "meetings": daily_brief.load_muted_meetings()}


@router.get("/history")
async def history():
    d = _load()
    return {"ok": True, "items": [
        {"id": b["id"], "slot": b["slot"], "ts": b["ts"]} for b in d.get("briefs", [])
    ]}
