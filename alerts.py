"""
alerts
──────
Metric threshold alerts (v1). Each alert runs a saved query on a schedule,
reduces one column to a single value (last/sum/avg/min/max), compares it to a
threshold, and emails recipients when the condition is met.

To avoid spamming, an alert only emails on a state *transition* into "firing"
(unless notify_every is set), tracked via last_fired flag.

Storage: ~/jarvis/semantic/alerts.json
Routes (prefix /api/alerts):
  GET    /api/alerts                 — list
  POST   /api/alerts                 — create
  PUT    /api/alerts/{aid}            — update
  DELETE /api/alerts/{aid}            — delete
  POST   /api/alerts/{aid}/check      — evaluate now (returns value + fired)

The brain scheduler calls tick(now) every minute (see brain/runner._loop).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

log = logging.getLogger("jarvis.alerts")
router = APIRouter(prefix="/api/alerts", tags=["alerts"])

_PATH = SEMANTIC_DIR / "alerts.json"
_lock = threading.Lock()
_last_minute: dict[str, str] = {}

_OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "alerts" in d else {"alerts": []}
    except Exception:
        return {"alerts": []}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _post_webhook(url: str, text: str) -> bool:
    """POST a Slack-compatible {text:...} payload to a webhook. Best-effort."""
    if not url:
        return False
    import json as _json
    import urllib.request
    try:
        data = _json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("webhook post failed: %s", e)
        return False


def _push_history(alert: dict, ev: dict) -> None:
    """Append an evaluation point to the alert's bounded history (last 60)."""
    h = alert.setdefault("history", [])
    h.append({"at": time.time(), "value": ev.get("value"), "fired": bool(ev.get("fired"))})
    if len(h) > 60:
        del h[:-60]


# ── evaluation ──────────────────────────────────────────────────────────────
def _reduce(values: list, agg: str):
    nums = []
    for v in values:
        try:
            if v is None or v == "":
                continue
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return None
    agg = (agg or "last").lower()
    if agg == "sum":
        return sum(nums)
    if agg == "avg":
        return sum(nums) / len(nums)
    if agg == "min":
        return min(nums)
    if agg == "max":
        return max(nums)
    if agg == "first":
        return nums[0]
    return nums[-1]  # last


def evaluate(alert: dict) -> dict:
    """Run the alert's query and compute {value, fired, detail}."""
    from report_email import _run_saved_query
    qid = alert.get("query_id")
    if not qid:
        return {"ok": False, "detail": "No query attached."}
    got = _run_saved_query(qid, limit=1000)
    if not got:
        return {"ok": False, "detail": "Query did not run (check the saved query / warehouse)."}
    name, cols, rows = got
    col = alert.get("column")
    # Default to the last column (usually the metric) when none specified.
    idx = cols.index(col) if (col and col in cols) else (len(cols) - 1 if cols else -1)
    if idx < 0:
        return {"ok": False, "detail": "No columns returned."}
    value = _reduce([r[idx] for r in rows], alert.get("agg", "last"))
    if value is None:
        return {"ok": False, "detail": "No numeric values to evaluate."}
    op = _OPS.get(alert.get("op", ">"))
    try:
        thr = float(alert.get("threshold"))
    except (TypeError, ValueError):
        return {"ok": False, "detail": "Invalid threshold."}
    fired = bool(op and op(value, thr))
    return {"ok": True, "value": value, "fired": fired,
            "column": cols[idx], "threshold": thr, "op": alert.get("op", ">")}


def _alert_email(alert: dict, ev: dict) -> tuple[str, str, str]:
    name = alert.get("name") or "Alert"
    cond = f"{ev['column']} {ev['op']} {ev['threshold']}"
    subject = f"🔔 Alert: {name} — {ev['column']} = {ev['value']:g}"
    text = (f"{name}\n\nCondition met: {cond}\nCurrent value: {ev['value']:g}\n"
            f"Checked {time.strftime('%Y-%m-%d %H:%M')}")
    html = (
        "<div style='font-family:system-ui,Segoe UI,sans-serif;max-width:560px;color:#0f172a;'>"
        f"<h2 style='margin:0 0 8px;'>🔔 {_html.escape(name)}</h2>"
        f"<p style='font-size:15px;'>Condition met: <b>{_html.escape(cond)}</b></p>"
        f"<p style='font-size:28px;font-weight:700;color:#b45309;margin:6px 0;'>{ev['value']:g}</p>"
        f"<p style='color:#94a3b8;font-size:12px;'>Checked {time.strftime('%Y-%m-%d %H:%M')} · Jarvis</p>"
        "</div>"
    )
    return subject, text, html


async def run_alert(alert: dict, force_send: bool = False) -> dict:
    """Evaluate + (conditionally) email. Returns evaluation dict + sent flag."""
    ev = evaluate(alert)
    if not ev.get("ok"):
        return ev
    fired = ev["fired"]
    was_firing = bool(alert.get("last_fired"))
    notify_every = bool(alert.get("notify_every"))
    notify_recovery = alert.get("notify_recovery", True)
    firing_now = force_send or (fired and (notify_every or not was_firing))
    # Recovery: transition from firing → not firing (never on force_send / manual check).
    recovered = (not force_send) and was_firing and (not fired) and bool(notify_recovery)
    # Snooze/mute window suppresses scheduled notifications (manual checks bypass).
    snoozed = (not force_send) and float(alert.get("snooze_until") or 0) > time.time()
    if snoozed:
        firing_now = recovered = False
        ev["snoozed"] = True
    sent_detail = ""
    if firing_now or recovered:
        loop = asyncio.get_running_loop()
        name = alert.get("name", "Alert")
        cond = f"{ev['column']} {ev['op']} {ev['threshold']}"
        if recovered:
            subject = f"✅ Recovered: {name} — back to {ev['value']:g}"
            text = f"{name} recovered.\n\n{cond} is no longer met. Current value: {ev['value']:g}\nChecked {time.strftime('%Y-%m-%d %H:%M')}"
            html = (f"<div style='font-family:system-ui,sans-serif;max-width:560px;color:#0f172a;'>"
                    f"<h2 style='margin:0 0 8px;'>✅ {_html.escape(name)} recovered</h2>"
                    f"<p style='font-size:15px;'>No longer meeting <b>{_html.escape(cond)}</b>.</p>"
                    f"<p style='font-size:28px;font-weight:700;color:#166534;margin:6px 0;'>{ev['value']:g}</p>"
                    f"<p style='color:#94a3b8;font-size:12px;'>Checked {time.strftime('%Y-%m-%d %H:%M')} · Jarvis</p></div>")
            wmsg = f"✅ *{name}* recovered — {cond} no longer met (now *{ev['value']:g}*)"
        else:
            subject, text, html = _alert_email(alert, ev)
            wmsg = f"🔔 *{name}* — {cond} (now *{ev['value']:g}*)"
        # Email (if recipients + SMTP)
        if (alert.get("recipients") or "").strip():
            try:
                import smtp_mailer
                if smtp_mailer.is_configured():
                    rec = await loop.run_in_executor(None, smtp_mailer.send,
                                                     alert["recipients"], subject, text, html)
                    sent_detail = "emailed " + ", ".join(rec)
                else:
                    sent_detail = "SMTP not configured"
            except Exception as e:
                sent_detail = f"email failed: {e}"
        # Slack / generic webhook
        if (alert.get("webhook_url") or "").strip():
            ok = await loop.run_in_executor(None, _post_webhook, alert["webhook_url"], wmsg)
            sent_detail = (sent_detail + " · " if sent_detail else "") + ("webhook ok" if ok else "webhook failed")
        if recovered:
            sent_detail = (sent_detail or "recovery") + " (recovery)"
    ev["sent"] = bool(sent_detail and "failed" not in sent_detail and "not configured" not in sent_detail)
    ev["sent_detail"] = sent_detail
    return ev


# ── digest (one email summarizing all firing alerts) ────────────────────────
def _digest_cfg() -> dict:
    d = _load().get("digest") or {}
    return {"enabled": bool(d.get("enabled")), "cron": d.get("cron") or "0 8 * * *",
            "recipients": d.get("recipients") or "", "webhook_url": d.get("webhook_url") or ""}


async def run_digest() -> dict:
    """Evaluate every enabled alert (without sending individual emails), then
    email one summary of those currently firing."""
    cfg = _digest_cfg()
    recips = (cfg.get("recipients") or "").strip()
    hook = (cfg.get("webhook_url") or "").strip()
    if not recips and not hook:
        return {"ok": False, "detail": "No digest recipients or webhook."}
    firing, checked = [], 0
    for a in _load().get("alerts", []):
        if not a.get("enabled", True):
            continue
        ev = evaluate(a)
        checked += 1
        if ev.get("ok") and ev.get("fired"):
            firing.append((a, ev))
    # Webhook summary (independent of SMTP).
    if hook:
        if firing:
            lines = "\n".join(f"• {a.get('name','')}: {ev['column']} {ev['op']} {ev['threshold']} (now {ev['value']:g})" for a, ev in firing)
            wtext = f"🔔 *Alert digest* — {len(firing)} firing\n{lines}"
        else:
            wtext = f"✅ *Alert digest* — all clear ({checked} checked)"
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _post_webhook, hook, wtext)
        except Exception:
            pass
    import smtp_mailer
    if not recips:
        return {"ok": True, "detail": "webhook sent", "firing": len(firing)}
    if not smtp_mailer.is_configured():
        return {"ok": bool(hook), "detail": "SMTP not configured" + (" (webhook sent)" if hook else ""), "firing": len(firing)}
    subject = f"🔔 Alert digest — {len(firing)} firing" if firing else "🔔 Alert digest — all clear"
    if firing:
        rows = "".join(
            f"<tr><td style='padding:5px 10px;border-bottom:1px solid #e2e8f0;'>{_html.escape(a.get('name',''))}</td>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #e2e8f0;'>{ev['column']} {ev['op']} {ev['threshold']}</td>"
            f"<td style='padding:5px 10px;border-bottom:1px solid #e2e8f0;font-weight:700;color:#b45309;'>{ev['value']:g}</td></tr>"
            for a, ev in firing)
        html = (f"<div style='font-family:system-ui,sans-serif;max-width:640px;'>"
                f"<h2>🔔 {len(firing)} alert(s) firing</h2>"
                f"<table style='border-collapse:collapse;'><thead><tr>"
                f"<th style='text-align:left;padding:5px 10px;background:#1f2b3a;color:#fff;'>Alert</th>"
                f"<th style='text-align:left;padding:5px 10px;background:#1f2b3a;color:#fff;'>Condition</th>"
                f"<th style='text-align:left;padding:5px 10px;background:#1f2b3a;color:#fff;'>Value</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")
    else:
        html = ("<div style='font-family:system-ui,sans-serif;'><h2>✅ All clear</h2>"
                f"<p>Checked {checked} alert(s); none are firing.</p></div>")
    text = f"{len(firing)} firing of {checked} checked."
    try:
        loop = asyncio.get_running_loop()
        rec = await loop.run_in_executor(None, smtp_mailer.send, recips, subject, text, html)
        return {"ok": True, "detail": "emailed " + ", ".join(rec), "firing": len(firing)}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ── scheduler tick ──────────────────────────────────────────────────────────
async def tick(now) -> None:
    try:
        from brain.runner import cron_due
    except Exception:
        return
    stamp = now.strftime("%Y-%m-%d %H:%M")
    # Digest run (independent cron).
    dc = _digest_cfg()
    if dc.get("enabled") and _last_minute.get("__digest__") != stamp:
        try:
            if cron_due(dc.get("cron") or "", now):
                _last_minute["__digest__"] = stamp
                await run_digest()
        except Exception:
            pass
    for alert in _load().get("alerts", []):
        if not alert.get("enabled", True):
            continue
        try:
            if not cron_due(alert.get("cron") or "", now):
                continue
        except Exception:
            continue
        if _last_minute.get(alert["id"]) == stamp:
            continue
        _last_minute[alert["id"]] = stamp
        ev = await run_alert(alert)
        with _lock:
            d = _load()
            for a in d.get("alerts", []):
                if a.get("id") == alert["id"]:
                    a["last_run"] = time.time()
                    if ev.get("ok"):
                        a["last_value"] = ev.get("value")
                        a["last_fired"] = ev.get("fired")
                        a["last_status"] = "fired" if ev.get("fired") else "ok"
                        _push_history(a, ev)
                    else:
                        a["last_status"] = "error"
                    a["last_detail"] = ev.get("detail") or ev.get("sent_detail") or ""
            _save(d)


# ── API ─────────────────────────────────────────────────────────────────────
class AlertBody(BaseModel):
    name: str
    query_id: str
    column: Optional[str] = None
    agg: str = "last"
    op: str = ">"
    threshold: float
    cron: str = "0 8 * * *"
    recipients: str = ""
    webhook_url: str = ""
    enabled: bool = True
    notify_every: bool = False
    notify_recovery: bool = True
    tags: list = []


@router.get("")
async def list_alerts():
    items = _load().get("alerts", [])
    try:
        import queries_api
        qn = {q.get("id"): q.get("name") for q in queries_api._load().get("queries", [])}
    except Exception:
        qn = {}
    for a in items:
        a["query_name"] = qn.get(a.get("query_id")) or "(deleted query)"
    return {"alerts": items, "count": len(items)}


@router.post("")
async def create_alert(body: AlertBody, request: Request):
    if not body.name.strip() or not body.query_id:
        return _err(400, "missing", "name and query_id are required.")
    if body.op not in _OPS:
        return _err(400, "bad op", "op must be one of > < >= <= == !=")
    rec = {
        "id": uuid.uuid4().hex[:8],
        "name": body.name.strip(),
        "query_id": body.query_id,
        "column": (body.column or "").strip() or None,
        "agg": body.agg, "op": body.op, "threshold": body.threshold,
        "cron": (body.cron or "0 8 * * *").strip(),
        "recipients": (body.recipients or "").strip(),
        "webhook_url": (body.webhook_url or "").strip(),
        "enabled": bool(body.enabled),
        "notify_every": bool(body.notify_every),
        "notify_recovery": bool(body.notify_recovery),
        "tags": [str(t).strip() for t in (body.tags or []) if str(t).strip()],
        "snooze_until": 0,
        "owner": (request.headers.get("X-Jarvis-User") or "").strip() or None,
        "created_at": time.time(),
        "last_run": None, "last_value": None, "last_fired": False, "last_status": None, "last_detail": None,
    }
    with _lock:
        d = _load(); d.setdefault("alerts", []).append(rec); _save(d)
    return rec


class DigestBody(BaseModel):
    enabled: bool = False
    cron: str = "0 8 * * *"
    recipients: str = ""
    webhook_url: str = ""


# NOTE: defined before the /{aid} routes so "digest" isn't captured as an id.
@router.get("/digest")
async def get_digest():
    return _digest_cfg()


@router.put("/digest")
async def set_digest(body: DigestBody):
    with _lock:
        d = _load()
        d["digest"] = {"enabled": bool(body.enabled), "cron": (body.cron or "0 8 * * *").strip(),
                       "recipients": (body.recipients or "").strip(),
                       "webhook_url": (body.webhook_url or "").strip()}
        _save(d)
    return d["digest"]


@router.post("/digest/send")
async def send_digest_now():
    return await run_digest()


class WebhookTestBody(BaseModel):
    url: str


@router.post("/test-webhook")
async def test_webhook(body: WebhookTestBody):
    url = (body.url or "").strip()
    if not url:
        return _err(400, "no url", "Provide a webhook URL.")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _post_webhook, url,
                                    "✅ Jarvis test — your alert webhook is working.")
    return {"ok": ok, "detail": "Test message delivered." if ok else "Webhook did not accept the message."}


@router.put("/{aid}")
async def update_alert(aid: str, body: AlertBody):
    if body.op not in _OPS:
        return _err(400, "bad op", "op must be one of > < >= <= == !=")
    with _lock:
        d = _load()
        a = next((x for x in d.get("alerts", []) if x.get("id") == aid), None)
        if not a:
            return _err(404, "not found", f"No alert '{aid}'")
        a.update({
            "name": body.name.strip() or a["name"], "query_id": body.query_id or a["query_id"],
            "column": (body.column or "").strip() or None, "agg": body.agg, "op": body.op,
            "threshold": body.threshold, "cron": (body.cron or a["cron"]).strip(),
            "recipients": (body.recipients or "").strip(), "webhook_url": (body.webhook_url or "").strip(),
            "enabled": bool(body.enabled), "notify_every": bool(body.notify_every),
            "notify_recovery": bool(body.notify_recovery),
            "tags": [str(t).strip() for t in (body.tags or []) if str(t).strip()],
        })
        _save(d)
    return a


@router.delete("/{aid}")
async def delete_alert(aid: str):
    with _lock:
        d = _load()
        d["alerts"] = [x for x in d.get("alerts", []) if x.get("id") != aid]
        _save(d)
    return {"ok": True, "deleted": aid}


class SnoozeBody(BaseModel):
    minutes: int = 60   # 0 clears the snooze


@router.post("/{aid}/snooze")
async def snooze_alert(aid: str, body: SnoozeBody):
    with _lock:
        d = _load()
        a = next((x for x in d.get("alerts", []) if x.get("id") == aid), None)
        if not a:
            return _err(404, "not found", f"No alert '{aid}'")
        mins = int(body.minutes or 0)
        a["snooze_until"] = (time.time() + mins * 60) if mins > 0 else 0
        _save(d)
    return {"ok": True, "snooze_until": a["snooze_until"]}


@router.post("/{aid}/check")
async def check_alert(aid: str):
    alert = next((x for x in _load().get("alerts", []) if x.get("id") == aid), None)
    if not alert:
        return _err(404, "not found", f"No alert '{aid}'")
    ev = await run_alert(alert, force_send=False)
    # Persist last value/status from a manual check too.
    with _lock:
        d = _load()
        for a in d.get("alerts", []):
            if a.get("id") == aid and ev.get("ok"):
                a["last_run"] = time.time(); a["last_value"] = ev.get("value")
                a["last_fired"] = ev.get("fired"); a["last_status"] = "fired" if ev.get("fired") else "ok"
                _push_history(a, ev)
        _save(d)
    return ev
