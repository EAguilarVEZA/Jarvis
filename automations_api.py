"""
automations_api — Jarvis Automations module (Reports + Actions)
─────────────────────────────────────────────────────────────────
Mirrors the Kaelio /automations pattern:

  • Scheduled Reports — run a report on a cadence, deliver via email / mobile push.
  • Actions          — watch a data source (trigger), and when conditions met,
                       fire an action (email, task, API call, pause-campaign, …).

Storage: ~/.config/jarvis/automations.json (atomic write + timestamped backup).

Routes (prefix /api/automations):
  GET    /reports                  — list scheduled reports
  POST   /reports                  — create a scheduled report
  GET    /reports/{id}             — get one
  PUT    /reports/{id}             — update
  DELETE /reports/{id}             — delete
  POST   /reports/{id}/run         — fire a manual run right now

  GET    /actions                  — list action automations
  POST   /actions                  — create an action automation
  GET    /actions/{id}             — get one
  PUT    /actions/{id}             — update
  DELETE /actions/{id}             — delete
  POST   /actions/{id}/test        — dry-run the trigger evaluation
"""

from __future__ import annotations
import json, os, time, uuid, shutil, threading
from pathlib import Path
from typing import Optional, Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/automations", tags=["automations"])

CFG_DIR = Path(os.path.expanduser("~/.config/jarvis"))
STORE   = CFG_DIR / "automations.json"
_lock   = threading.Lock()


# ─── Storage helpers ─────────────────────────────────────────────────
def _load() -> dict:
    if not STORE.exists():
        return {"reports": [], "actions": []}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"reports": [], "actions": []}


def _save(data: dict):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    if STORE.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try: shutil.copy2(STORE, STORE.with_suffix(f".json.bak.{ts}"))
        except OSError: pass
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE)
    try: os.chmod(STORE, 0o600)
    except OSError: pass


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


# ─── Pydantic models ────────────────────────────────────────────────
class ReportIn(BaseModel):
    """
    Full payload the report-automation modal sends. Every UI field has a matching
    backend field here — otherwise Pydantic silently drops the value and the
    modal feels "saved" while the user's choices vanish.
    """
    model_config = {"extra": "ignore"}

    name: str
    description: Optional[str] = ""
    # Source: which Jarvis view / report to send
    view_id: Optional[str] = None        # e.g. "hub-report-cancer"
    page_id: Optional[str] = None        # optional specific page
    # Schedule
    frequency: str = "weekly"            # daily | weekly | monthly | hourly
    day_of_week: Optional[str] = None    # Mon..Sun (weekly)
    day_of_month: Optional[int] = None   # 1..31 (monthly)
    time_utc: Optional[str] = "09:00"    # HH:MM
    # Delivery — recipients
    email_recipients: list[str] = Field(default_factory=list)   # explicit emails
    recipient_users: list[str] = Field(default_factory=list)    # user ids (optional)
    recipient_groups: list[str] = Field(default_factory=list)   # group ids (resolved at send)
    slack_channels: list[str] = Field(default_factory=list)
    mobile_push: bool = False            # send to PWA mobile installs
    notify_recipients: bool = True       # raise a notification on arrival
    # Conditional reporting
    conditional: bool = False
    conditional_expr: Optional[str] = ""
    # Date range to apply when running
    date_range_mode: str = "rolling"     # rolling | absolute | last_n_days
    last_n_days: Optional[int] = 60      # for date_range_mode='last_n_days'
    absolute_start: Optional[str] = None # YYYY-MM-DD for 'absolute'
    absolute_end: Optional[str] = None   # YYYY-MM-DD for 'absolute'
    date_range_value: Any = 60           # legacy; kept for back-compat
    # Output format
    output_format: str = "pdf"           # pdf | csv | both
    # Status
    active: bool = True


class ActionIn(BaseModel):
    name: str
    description: Optional[str] = ""
    active: bool = False
    # Trigger
    trigger: dict                        # {source, table, conditions:[{field,op,value}], logic:'AND'|'OR'}
    # Action type & config
    action_type: str                     # email_notification | task | webhook | pause_campaign | resume_campaign
    action_config: dict                  # depends on action_type
    # Cadence — how often to re-evaluate the trigger
    poll_interval_minutes: int = 15


# ─── Reports endpoints ──────────────────────────────────────────────
@router.get("/reports")
async def list_reports():
    return {"reports": _load().get("reports", [])}


@router.post("/reports")
async def create_report(body: ReportIn):
    with _lock:
        data = _load()
        rep = body.model_dump()
        rep["id"]         = str(uuid.uuid4())
        rep["created_at"] = time.time()
        rep["updated_at"] = time.time()
        rep["last_run_at"]= None
        rep["next_run_at"]= _compute_next_run(rep)
        rep["run_count"]  = 0
        data["reports"].append(rep)
        _save(data)
    return rep


@router.get("/reports/{rid}")
async def get_report(rid: str):
    rep = next((r for r in _load().get("reports", []) if r.get("id") == rid), None)
    if not rep: return _err(404, "not found")
    return rep


@router.put("/reports/{rid}")
async def update_report(rid: str, body: ReportIn):
    with _lock:
        data = _load()
        rep = next((r for r in data["reports"] if r.get("id") == rid), None)
        if not rep: return _err(404, "not found")
        rep.update(body.model_dump())
        rep["updated_at"] = time.time()
        rep["next_run_at"] = _compute_next_run(rep)
        _save(data)
    return rep


@router.delete("/reports/{rid}")
async def delete_report(rid: str):
    with _lock:
        data = _load()
        before = len(data["reports"])
        data["reports"] = [r for r in data["reports"] if r.get("id") != rid]
        if len(data["reports"]) == before: return _err(404, "not found")
        _save(data)
    return {"ok": True, "deleted": rid}


@router.post("/reports/{rid}/run")
async def run_report(rid: str):
    """Manually trigger a report run. Records the run + bumps next_run_at."""
    with _lock:
        data = _load()
        rep = next((r for r in data["reports"] if r.get("id") == rid), None)
        if not rep: return _err(404, "not found")
        # Stub — actual PDF render + email send is scaffolded; wire when scheduler comes online
        rep["last_run_at"] = time.time()
        rep["run_count"]   = (rep.get("run_count") or 0) + 1
        rep["next_run_at"] = _compute_next_run(rep)
        rep["last_status"] = "queued"
        rep["last_recipient_count"] = len(_resolve_recipients(rep).get("emails", []))
        _save(data)
    return {"ok": True, "ran_at": rep["last_run_at"], "next_run_at": rep["next_run_at"],
            "recipient_count": rep["last_recipient_count"]}


@router.post("/reports/preview")
async def preview_report(body: ReportIn):
    """
    Dry-run the report distribution: resolve groups → emails, validate the
    dashboard, compute the date window, and return everything the modal needs
    to render a "this is what will go out" confirmation card.

    The UI's Test Send Now button hits this — no email is actually sent.
    """
    rep = body.model_dump()
    issues: list[str] = []
    # Validate dashboard
    view = _resolve_view(rep.get("view_id"))
    if not view:
        issues.append(f"Dashboard '{rep.get('view_id')}' not found — pick one from the dropdown.")
    # Resolve recipients
    resolved = _resolve_recipients(rep)
    if not resolved["emails"]:
        issues.append("No recipients resolved. Add an email, a user, or a group with members.")
    # Compute date window
    window = _compute_date_window(rep)
    # Compute next 3 runs
    upcoming = _next_n_runs(rep, n=3)
    return {
        "ok": not issues,
        "issues": issues,
        "view": {"id": (view or {}).get("id"), "name": (view or {}).get("name"),
                 "page_count": (view or {}).get("page_count"),
                 "widget_count": (view or {}).get("widget_count")},
        "recipients": resolved,
        "date_window": window,
        "upcoming_runs": upcoming,
        "format": rep.get("output_format", "pdf"),
        "notify": rep.get("notify_recipients", True),
        "mobile_push": rep.get("mobile_push", False),
    }


# ─── Actions endpoints ──────────────────────────────────────────────
@router.get("/actions")
async def list_actions():
    return {"actions": _load().get("actions", [])}


@router.post("/actions")
async def create_action(body: ActionIn):
    with _lock:
        data = _load()
        act = body.model_dump()
        act["id"]         = str(uuid.uuid4())
        act["created_at"] = time.time()
        act["updated_at"] = time.time()
        act["last_eval_at"] = None
        act["last_fire_at"] = None
        act["fire_count"]   = 0
        data["actions"].append(act)
        _save(data)
    return act


@router.get("/actions/{aid}")
async def get_action(aid: str):
    act = next((a for a in _load().get("actions", []) if a.get("id") == aid), None)
    if not act: return _err(404, "not found")
    return act


@router.put("/actions/{aid}")
async def update_action(aid: str, body: ActionIn):
    with _lock:
        data = _load()
        act = next((a for a in data["actions"] if a.get("id") == aid), None)
        if not act: return _err(404, "not found")
        act.update(body.model_dump())
        act["updated_at"] = time.time()
        _save(data)
    return act


@router.delete("/actions/{aid}")
async def delete_action(aid: str):
    with _lock:
        data = _load()
        before = len(data["actions"])
        data["actions"] = [a for a in data["actions"] if a.get("id") != aid]
        if len(data["actions"]) == before: return _err(404, "not found")
        _save(data)
    return {"ok": True, "deleted": aid}


@router.post("/actions/{aid}/test")
async def test_action(aid: str):
    """Dry-run the trigger evaluation. Reports whether conditions match right now."""
    act = next((a for a in _load().get("actions", []) if a.get("id") == aid), None)
    if not act: return _err(404, "not found")
    # Stub evaluator — real impl would query the trigger source and apply conditions
    return {
        "ok": True,
        "matched": False,
        "evaluated_at": time.time(),
        "message": "Trigger evaluation scaffolded. Wire SQL/HTTP evaluator in the next pass."
    }


@router.post("/actions/{aid}/run")
async def run_action(aid: str):
    """Manually execute an action's downstream effect — used when a trigger fires
    OR when the operator wants to test the action body in isolation.

    For action_type='airbyte_sync', this calls /api/airbyte/connections/{id}/sync
    so the chosen Airbyte connection syncs immediately. Other action types are
    scaffolded — they record the invocation but don't yet ship to their external
    target (Slack/webhook/email require credentials we don't auto-store)."""
    act = next((a for a in _load().get("actions", []) if a.get("id") == aid), None)
    if not act: return _err(404, "not found")

    action_type = act.get("action_type")
    cfg = act.get("action_config") or {}
    result = {"action_type": action_type, "started_at": time.time()}

    if action_type == "airbyte_sync":
        cid = cfg.get("connection_id")
        if not cid:
            return _err(400, "no_connection", "action_config.connection_id is required for airbyte_sync")
        try:
            import requests
            from airbyte_api import _airbyte_url, _bearer   # type: ignore
            url = f"{_airbyte_url()}/api/public/v1/jobs"
            headers = {"Content-Type": "application/json"}
            tok = _bearer()
            if tok: headers["Authorization"] = f"Bearer {tok}"
            r = requests.post(url, json={"connectionId": cid, "jobType": "sync"}, headers=headers, timeout=30)
            result["airbyte_status"] = r.status_code
            result["airbyte_response"] = r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:300]
            result["ok"] = 200 <= r.status_code < 300
        except Exception as e:
            return _err(502, "airbyte_unreachable", str(e))
    else:
        result["ok"] = True
        result["message"] = f"action_type='{action_type}' execution is scaffolded — no external delivery happened."

    # Record the fire on the action so the UI can show last_fire_at
    with _lock:
        data = _load()
        a = next((x for x in data["actions"] if x.get("id") == aid), None)
        if a:
            a["last_fire_at"] = time.time()
            a["fire_count"] = (a.get("fire_count") or 0) + 1
            a["last_fire_result"] = {k:v for k,v in result.items() if k != "airbyte_response"}
            _save(data)
    return result


# ─── Helpers ────────────────────────────────────────────────────────
def _compute_next_run(rep: dict) -> float:
    """Best-effort compute of the next-run epoch from frequency + time_utc."""
    import datetime as _dt
    freq = (rep.get("frequency") or "weekly").lower()
    now  = _dt.datetime.utcnow()
    try:
        hh, mm = (rep.get("time_utc") or "09:00").split(":")
        hh, mm = int(hh), int(mm)
    except Exception:
        hh, mm = 9, 0
    if freq == "hourly":
        nxt = now.replace(minute=mm, second=0, microsecond=0) + _dt.timedelta(hours=1)
    elif freq == "daily":
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now: nxt += _dt.timedelta(days=1)
    elif freq == "weekly":
        days = ["mon","tue","wed","thu","fri","sat","sun"]
        try: target = days.index((rep.get("day_of_week") or "mon").lower()[:3])
        except ValueError: target = 0
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (target - nxt.weekday()) % 7
        nxt = nxt + _dt.timedelta(days=delta)
        if nxt <= now: nxt += _dt.timedelta(days=7)
    elif freq == "monthly":
        dom = int(rep.get("day_of_month") or 1)
        y, m = now.year, now.month
        try:
            nxt = _dt.datetime(y, m, dom, hh, mm)
            if nxt <= now: m, y = (m+1, y) if m < 12 else (1, y+1); nxt = _dt.datetime(y, m, dom, hh, mm)
        except ValueError:
            nxt = now + _dt.timedelta(days=30)
    else:
        nxt = now + _dt.timedelta(days=1)
    return nxt.timestamp()


# ─── Recipient / view / window resolvers ────────────────────────────
def _resolve_view(view_id: Optional[str]) -> Optional[dict]:
    """
    Look the dashboard up in semantic/views.json — same source the UI uses.
    Returns None when the file exists but the id isn't found, so the caller
    can raise a "dashboard not found" issue. Returns a soft fallback only when
    views.json itself is missing (dev environments).
    """
    if not view_id: return None
    try:
        from pathlib import Path as _P
        import json as _j
        p = _P(__file__).resolve().parent / "semantic" / "views.json"
        if not p.exists():
            return {"id": view_id, "name": view_id, "page_count": None, "widget_count": None}
        data = _j.loads(p.read_text(encoding="utf-8"))
        for v in (data.get("views") or []):
            if v.get("id") == view_id:
                pages = v.get("pages") or []
                widgets = sum(len(pg.get("widgets") or []) for pg in pages)
                return {"id": v.get("id"), "name": v.get("name", view_id),
                        "page_count": len(pages), "widget_count": widgets}
        return None   # views.json loaded but the id is not in it
    except Exception:
        return None


def _resolve_recipients(rep: dict) -> dict:
    """
    Turn (email_recipients, recipient_users, recipient_groups) into the actual
    flat email list that will receive the report. Groups expand to all members.

    Returns: { emails:[str], by_source: { direct, users, groups: [{name,count}] },
               total: int, missing_groups: [group_id] }
    """
    direct = list(dict.fromkeys(rep.get("email_recipients") or []))
    user_ids  = rep.get("recipient_users") or []
    group_ids = rep.get("recipient_groups") or []

    user_emails: list[str] = []
    group_breakdown: list[dict] = []
    missing_groups: list[str] = []

    try:
        from users_api import _load_users   # type: ignore
        data = _load_users()
        users  = data.get("users", [])
        groups = data.get("groups", [])
        users_by_id = {u.get("id"): u for u in users}
        groups_by_id = {g.get("id"): g for g in groups}

        for uid in user_ids:
            u = users_by_id.get(uid)
            if u and u.get("email"): user_emails.append(u["email"])

        # Group membership lives on the user (u['groups'] is a list of group ids),
        # so to expand a group we scan users for membership.
        for gid in group_ids:
            g = groups_by_id.get(gid)
            if not g:
                missing_groups.append(gid); continue
            members = [u for u in users if gid in (u.get("groups") or [])]
            emails  = [u["email"] for u in members if u.get("email") and u.get("status","active")=="active"]
            group_breakdown.append({"id": gid, "name": g.get("name", gid),
                                    "member_count": len(members),
                                    "resolved_emails": emails})
    except Exception:
        # users_api may not expose those helpers — keep dry-run usable.
        pass

    flat = list(dict.fromkeys(direct + user_emails + [e for g in group_breakdown for e in g["resolved_emails"]]))
    return {
        "emails": flat,
        "total": len(flat),
        "by_source": {
            "direct": direct,
            "users":  user_emails,
            "groups": group_breakdown,
        },
        "missing_groups": missing_groups,
    }


def _compute_date_window(rep: dict) -> dict:
    """Translate the modal's date_range_mode into a concrete {start,end,label}."""
    import datetime as _dt
    mode = rep.get("date_range_mode", "rolling")
    today = _dt.date.today()
    if mode == "last_n_days":
        n = int(rep.get("last_n_days") or rep.get("date_range_value") or 60)
        start = today - _dt.timedelta(days=n)
        return {"mode": mode, "start": start.isoformat(), "end": today.isoformat(),
                "label": f"Last {n} days"}
    if mode == "absolute":
        s = rep.get("absolute_start") or ""
        e = rep.get("absolute_end")   or ""
        return {"mode": mode, "start": s, "end": e,
                "label": f"{s} → {e}" if s and e else "Absolute (incomplete)"}
    # rolling default — today minus 2 months → today
    start = today - _dt.timedelta(days=60)
    return {"mode": "rolling", "start": start.isoformat(), "end": today.isoformat(),
            "label": "Rolling 2-month window"}


def _next_n_runs(rep: dict, n: int = 3) -> list[float]:
    """Compute the next n scheduled run timestamps starting from now."""
    runs: list[float] = []
    sim = dict(rep)
    for _ in range(n):
        ts = _compute_next_run(sim)
        runs.append(ts)
        # Shift "now" past this run so the next call returns the one after it
        sim = dict(sim); sim["_last_run_at"] = ts
        import datetime as _dt
        # Re-seed by faking now to be just after ts (compute uses utcnow())
        # Easiest approach: shift by a frequency-appropriate delta to force the
        # next-iteration jump in _compute_next_run.
        # Since _compute_next_run uses utcnow(), we approximate by advancing the
        # day_of_week/day_of_month implicitly — kept simple: just add 1s and
        # recompute will land on the same; instead, advance time_utc artificially.
        # For now, manually space the runs:
        pass
    # Simpler/honest approach: space runs by the cadence ourselves.
    freq = (rep.get("frequency") or "weekly").lower()
    base = _compute_next_run(rep)
    if freq == "hourly": step = 3600.0
    elif freq == "daily": step = 86400.0
    elif freq == "weekly": step = 7 * 86400.0
    elif freq == "monthly": step = 30 * 86400.0
    else: step = 86400.0
    return [base + i * step for i in range(n)]
