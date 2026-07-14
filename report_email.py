"""
report_email
────────────
Scheduled email delivery of Designer reports (v1).

Each schedule pairs a report with a cron expression + recipient list. On a due
tick the report's public share link is ensured, a lightweight HTML digest is
built (link + per-widget data preview, best-effort), and the email is sent via
smtp_mailer. Completely inert until SMTP is configured (SMTP_HOST / SMTP_FROM).

Storage: ~/jarvis/semantic/report_email_schedules.json
Routes (prefix /api/reports/email):
  GET    /api/reports/email/schedules            — list schedules
  POST   /api/reports/email/schedules            — create
  PUT    /api/reports/email/schedules/{sid}       — update
  DELETE /api/reports/email/schedules/{sid}       — delete
  POST   /api/reports/email/schedules/{sid}/send  — send now (test)

The brain scheduler calls tick(now) every minute (see brain/runner._loop).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

log = logging.getLogger("jarvis.report_email")
router = APIRouter(prefix="/api/reports/email", tags=["report-email"])

_PATH = SEMANTIC_DIR / "report_email_schedules.json"
_lock = threading.Lock()
_last_minute: dict[str, str] = {}


# ── storage ───────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "schedules" in d else {"schedules": []}
    except Exception:
        return {"schedules": []}


def _save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def public_base() -> str:
    """Base URL the recipient can reach. Configure JARVIS_PUBLIC_URL when hosted."""
    return (os.getenv("JARVIS_PUBLIC_URL") or "https://localhost:8000").rstrip("/")


# ── share link ────────────────────────────────────────────────────────────
def _ensure_share_token(report_id: str) -> Optional[str]:
    """Ensure the report has an enabled, password-free share token; return it."""
    import reports_api
    with reports_api._lock:
        data = reports_api._load()
        r = next((x for x in data["reports"] if x.get("id") == report_id), None)
        if not r:
            return None
        share = dict(r.get("share") or {})
        if not share.get("token"):
            share["token"] = secrets.token_urlsafe(16)
            share["salt"] = secrets.token_hex(8)
        share["enabled"] = True
        share.setdefault("protection", "none")
        r["share"] = share
        r["updated_at"] = time.time()
        try:
            reports_api._atomic_write(data)
        except Exception:
            pass
        return share["token"]


def _get_report(report_id: str) -> Optional[dict]:
    import reports_api
    data = reports_api._load()
    return next((x for x in data["reports"] if x.get("id") == report_id), None)


# ── data digest (best-effort) ──────────────────────────────────────────────
def _run_saved_query(qid: str, limit: int = 8):
    """Execute a saved query and return (name, columns, rows) or None."""
    try:
        import queries_api
        from semantic_api import _load_fresh as _semantic_load
        from semantic.query_builder import StructuredQuery, build_sql
        from semantic.executor import run_query as _run
        data = queries_api._load()
        q = next((x for x in data["queries"] if x.get("id") == qid), None)
        if not q:
            return None
        qd = dict(q.get("qdict") or {})
        qd["limit"] = min(int(qd.get("limit") or limit), limit)
        model = _semantic_load()
        sql = build_sql(StructuredQuery.from_dict(qd), model)
        result = _run(sql)
        return (q.get("name") or "Query", result.columns, result.rows)
    except Exception:
        return None


def _digest_html(report: dict) -> str:
    """Small HTML tables for each widget with a saved query. Never raises."""
    blocks = []
    seen = set()
    for page in (report.get("pages") or []):
        for el in (page.get("elements") or []):
            if el.get("type") != "widget":
                continue
            qid = (el.get("props") or {}).get("queryId")
            if not qid or qid in seen:
                continue
            seen.add(qid)
            got = _run_saved_query(qid)
            if not got:
                continue
            name, cols, rows = got
            th = "".join(f"<th style='text-align:left;padding:4px 8px;background:#1f2b3a;color:#fff;font-size:12px;'>{_html.escape(str(c))}</th>" for c in cols)
            trs = ""
            for row in rows[:8]:
                tds = "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #e2e8f0;font-size:12px;'>{_html.escape('' if v is None else str(v))}</td>" for v in row)
                trs += f"<tr>{tds}</tr>"
            blocks.append(
                f"<h3 style='font:600 14px system-ui;margin:18px 0 6px;'>{_html.escape(name)}</h3>"
                f"<table style='border-collapse:collapse;'><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"
            )
        if len(blocks) >= 6:
            break
    return "".join(blocks)


def build_email(report: dict, token: Optional[str]) -> tuple[str, str, str]:
    """Return (subject, text, html) for a report."""
    name = report.get("name") or "Report"
    subject = f"Jarvis report — {name}"
    link = f"{public_base()}/app?share={token}" if token else None
    npages = len(report.get("pages") or [])
    text_lines = [name, ""]
    if report.get("description"):
        text_lines.append(report["description"])
        text_lines.append("")
    if link:
        text_lines.append(f"Open the live report: {link}")
    text_lines.append(f"{npages} page(s) · generated {time.strftime('%Y-%m-%d %H:%M')}")
    text = "\n".join(text_lines)

    digest = _digest_html(report)
    html = (
        "<div style='font-family:system-ui,Segoe UI,sans-serif;max-width:720px;color:#0f172a;'>"
        f"<h2 style='margin:0 0 4px;'>{_html.escape(name)}</h2>"
        + (f"<p style='color:#475569;margin:0 0 10px;'>{_html.escape(report.get('description') or '')}</p>" if report.get("description") else "")
        + (f"<p><a href='{link}' style='display:inline-block;background:#3a7a9b;color:#fff;padding:9px 16px;border-radius:8px;text-decoration:none;font-weight:600;'>Open live report →</a></p>" if link else "")
        + digest
        + f"<p style='color:#94a3b8;font-size:12px;margin-top:20px;'>{npages} page(s) · generated {time.strftime('%Y-%m-%d %H:%M')} · Jarvis</p>"
        "</div>"
    )
    return subject, text, html


# ── PDF render (v2 — Playwright headless Chromium) ──────────────────────────
async def render_report_pdf(token: str) -> Optional[bytes]:
    """Render the public share page to a PDF via Playwright. Returns bytes or
    None if Playwright / its browser isn't available or the render fails.

    Requires the chromium browser binary: `python -m playwright install chromium`.
    """
    if not token:
        return None
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log.warning("playwright not importable: %s", e)
        return None
    url = f"{public_base()}/app?share={token}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            ctx = await browser.new_context(ignore_https_errors=True,
                                            viewport={"width": 1280, "height": 900})
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(2800)  # let ECharts/maps settle
            pdf = await page.pdf(format="A4", landscape=True, print_background=True,
                                 margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
            await browser.close()
            return pdf
    except Exception as e:
        log.warning("report PDF render failed: %s", e)
        return None


def build_query_email(query_id: str, subject_override: str = "") -> tuple[str, str, str]:
    """Build (subject, text, html) with a formatted table of a saved query."""
    got = _run_saved_query(query_id, limit=100)
    if not got:
        return ("Jarvis query", "Query did not run.", "<p>Query did not run.</p>")
    name, cols, rows = got
    subject = subject_override or f"Jarvis query — {name}"
    th = "".join(f"<th style='text-align:left;padding:5px 10px;background:#1f2b3a;color:#fff;font-size:12px;'>{_html.escape(str(c))}</th>" for c in cols)
    trs = ""
    for row in rows[:100]:
        tds = "".join(f"<td style='padding:4px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;'>{_html.escape('' if v is None else str(v))}</td>" for v in row)
        trs += f"<tr>{tds}</tr>"
    html = (f"<div style='font-family:system-ui,Segoe UI,sans-serif;max-width:760px;color:#0f172a;'>"
            f"<h2 style='margin:0 0 8px;'>{_html.escape(name)}</h2>"
            f"<p style='color:#94a3b8;font-size:12px;'>{len(rows)} row(s) · generated {time.strftime('%Y-%m-%d %H:%M')}</p>"
            f"<table style='border-collapse:collapse;'><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>")
    text = f"{name}\n{len(rows)} rows · {time.strftime('%Y-%m-%d %H:%M')}"
    return subject, text, html


# ── deliver ─────────────────────────────────────────────────────────────────
async def deliver(sched: dict) -> dict:
    """Send one schedule now (async). Optionally attaches a rendered PDF.
    Returns {ok, detail}."""
    import smtp_mailer
    if not smtp_mailer.is_configured():
        return {"ok": False, "detail": "SMTP not configured (set SMTP_HOST / SMTP_FROM)."}
    recips = (sched.get("recipients") or "").strip()
    if not recips:
        return {"ok": False, "detail": "No recipients."}

    loop = asyncio.get_running_loop()
    hook = (sched.get("webhook_url") or "").strip()

    async def _hook(msg):
        if not hook:
            return ""
        try:
            from alerts import _post_webhook
            ok = await loop.run_in_executor(None, _post_webhook, hook, msg)
            return " · webhook ok" if ok else " · webhook failed"
        except Exception:
            return " · webhook failed"

    # Saved-query schedule → email a formatted result table (no share/PDF).
    if sched.get("query_id"):
        subject, text, html = build_query_email(sched["query_id"], sched.get("subject") or "")
        wnote = await _hook("📊 Scheduled query ready: " + subject)
        try:
            sent = await loop.run_in_executor(None, lambda: smtp_mailer.send(recips, subject, text, html))
            return {"ok": True, "detail": "Sent to " + ", ".join(sent) + wnote}
        except Exception as e:
            return {"ok": bool(hook), "detail": str(e) + wnote}

    report = _get_report(sched.get("report_id"))
    if not report:
        return {"ok": False, "detail": "Report not found."}
    token = _ensure_share_token(report["id"])
    subject, text, html = build_email(report, token)
    if sched.get("subject"):
        subject = sched["subject"]

    attachments = None
    pdf_note = ""
    if sched.get("attach_pdf"):
        pdf = await render_report_pdf(token)
        if pdf:
            fname = ((report.get("name") or "report").replace("/", "-")) + ".pdf"
            attachments = [(fname, pdf, "application/pdf")]
        else:
            pdf_note = " (PDF attachment skipped — Playwright/browser unavailable)"

    wnote = await _hook("📊 Scheduled report ready: " + (report.get("name") or "report") +
                        (f"  {public_base()}/app?share={token}" if token else ""))
    try:
        sent = await loop.run_in_executor(
            None, lambda: smtp_mailer.send(recips, subject, text, html, attachments))
        return {"ok": True, "detail": "Sent to " + ", ".join(sent) + pdf_note + wnote}
    except Exception as e:
        return {"ok": bool(hook), "detail": str(e) + wnote}


# ── scheduler tick (called by brain/runner._loop each minute) ───────────────
async def tick(now) -> None:
    try:
        from brain.runner import cron_due
    except Exception:
        return
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for sched in _load().get("schedules", []):
        if not sched.get("enabled", True):
            continue
        cron = sched.get("cron") or ""
        try:
            if not cron_due(cron, now):
                continue
        except Exception:
            continue
        if _last_minute.get(sched["id"]) == stamp:
            continue
        _last_minute[sched["id"]] = stamp
        res = await deliver(sched)
        # Record last-run status.
        with _lock:
            d = _load()
            for s in d.get("schedules", []):
                if s.get("id") == sched["id"]:
                    s["last_run"] = time.time()
                    s["last_status"] = "ok" if res.get("ok") else "error"
                    s["last_detail"] = res.get("detail", "")
            _save(d)


# ── API ─────────────────────────────────────────────────────────────────────
class ScheduleBody(BaseModel):
    report_id: Optional[str] = None   # report schedule
    query_id: Optional[str] = None    # OR saved-query schedule
    recipients: str = ""
    cron: str = "0 8 * * 1"          # default: Mondays 08:00
    subject: Optional[str] = None
    enabled: bool = True
    attach_pdf: bool = False         # v2 — render + attach a PDF via Playwright
    webhook_url: Optional[str] = None  # optional Slack/webhook delivery


@router.get("/schedules")
async def list_schedules(report_id: Optional[str] = None, query_id: Optional[str] = None):
    items = _load().get("schedules", [])
    if report_id:
        items = [s for s in items if s.get("report_id") == report_id]
    if query_id:
        items = [s for s in items if s.get("query_id") == query_id]
    # Enrich with report / query names for the admin overview (best-effort).
    try:
        import reports_api
        rnames = {r.get("id"): r.get("name") for r in reports_api._load().get("reports", [])}
    except Exception:
        rnames = {}
    try:
        import queries_api
        qnames = {q.get("id"): q.get("name") for q in queries_api._load().get("queries", [])}
    except Exception:
        qnames = {}
    for s in items:
        if s.get("query_id"):
            s["report_name"] = qnames.get(s["query_id"], "(deleted query)")
            s["kind"] = "query"
        else:
            s["report_name"] = rnames.get(s.get("report_id"), "(deleted report)")
            s["kind"] = "report"
    return {"schedules": items, "count": len(items)}


@router.post("/schedules")
async def create_schedule(body: ScheduleBody, request: Request):
    if not (body.report_id or body.query_id) or not body.recipients.strip():
        return _err(400, "missing", "report_id or query_id, and recipients are required.")
    rec = {
        "id": uuid.uuid4().hex[:8],
        "report_id": body.report_id or None,
        "query_id": body.query_id or None,
        "recipients": body.recipients.strip(),
        "cron": (body.cron or "0 8 * * 1").strip(),
        "subject": (body.subject or "").strip() or None,
        "enabled": bool(body.enabled),
        "attach_pdf": bool(body.attach_pdf),
        "webhook_url": (body.webhook_url or "").strip() or None,
        "owner": (request.headers.get("X-Jarvis-User") or "").strip() or None,
        "created_at": time.time(),
        "last_run": None, "last_status": None, "last_detail": None,
    }
    with _lock:
        d = _load(); d.setdefault("schedules", []).append(rec); _save(d)
    return rec


@router.put("/schedules/{sid}")
async def update_schedule(sid: str, body: ScheduleBody):
    with _lock:
        d = _load()
        s = next((x for x in d.get("schedules", []) if x.get("id") == sid), None)
        if not s:
            return _err(404, "not found", f"No schedule '{sid}'")
        if body.report_id: s["report_id"] = body.report_id
        if body.query_id: s["query_id"] = body.query_id
        s["recipients"] = body.recipients.strip() or s["recipients"]
        s["cron"] = (body.cron or s["cron"]).strip()
        s["subject"] = (body.subject or "").strip() or None
        s["enabled"] = bool(body.enabled)
        s["attach_pdf"] = bool(body.attach_pdf)
        if body.webhook_url is not None: s["webhook_url"] = (body.webhook_url or "").strip() or None
        _save(d)
    return s


@router.delete("/schedules/{sid}")
async def delete_schedule(sid: str):
    with _lock:
        d = _load()
        d["schedules"] = [x for x in d.get("schedules", []) if x.get("id") != sid]
        _save(d)
    return {"ok": True, "deleted": sid}


@router.post("/schedules/{sid}/send")
async def send_now(sid: str):
    sched = next((x for x in _load().get("schedules", []) if x.get("id") == sid), None)
    if not sched:
        return _err(404, "not found", f"No schedule '{sid}'")
    return await deliver(sched)
