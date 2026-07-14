"""
settings_api — platform settings (Kaelio /admin/settings parity).

Covers the practical, self-contained sections:
  • Appearance — app name, accent color, logo URL
  • General    — simple mode, research mode
  • Email      — SMTP status (from env) + a test action
  • API keys   — create (shown once), list, delete; salted-hash storage + verify

State under the semantic dir: app_settings.json, api_keys.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

_lock = threading.Lock()
_SETTINGS_PATH = SEMANTIC_DIR / "app_settings.json"
_KEYS_PATH = SEMANTIC_DIR / "api_keys.json"

_DEFAULTS = {
    "appearance": {"app_name": "Jarvis", "accent": "#3a7a9b", "logo_url": ""},
    "general": {"simple_mode": False, "research_mode": "balanced"},
    "notifications": {"recipients": "", "email_reports": True},
}


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> dict:
    s = _read(_SETTINGS_PATH, {})
    out = {k: dict(v) for k, v in _DEFAULTS.items()}
    if isinstance(s, dict):
        for section, vals in s.items():
            if section in out and isinstance(vals, dict):
                out[section].update(vals)
    return out


# ── API keys ────────────────────────────────────────────────────────────────
def _keys() -> list:
    d = _read(_KEYS_PATH, [])
    return d if isinstance(d, list) else []


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_api_key(raw: str) -> Optional[dict]:
    """Return the key record if valid + not expired, else None. Updates last_used."""
    if not raw:
        return None
    h = _hash_key(raw)
    with _lock:
        keys = _keys()
        rec = next((k for k in keys if k.get("hash") == h), None)
        if not rec:
            return None
        if rec.get("expires") and time.time() > rec["expires"]:
            return None
        rec["last_used"] = time.time()
        _write(_KEYS_PATH, keys)
        return {k: v for k, v in rec.items() if k != "hash"}


# ── API ──────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/admin/settings", tags=["settings"])


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


@router.get("")
async def get_settings():
    return load_settings()


class SettingsBody(BaseModel):
    appearance: Optional[dict] = None
    general: Optional[dict] = None
    notifications: Optional[dict] = None


@router.put("")
async def put_settings(body: SettingsBody):
    with _lock:
        cur = load_settings()
        if body.appearance:
            cur["appearance"].update({k: v for k, v in body.appearance.items() if k in _DEFAULTS["appearance"]})
        if body.general:
            cur["general"].update({k: v for k, v in body.general.items() if k in _DEFAULTS["general"]})
        if body.notifications:
            cur["notifications"].update({k: v for k, v in body.notifications.items() if k in _DEFAULTS["notifications"]})
        _write(_SETTINGS_PATH, cur)
    return cur


@router.get("/email")
async def email_status():
    host = os.getenv("SMTP_HOST") or os.getenv("EMAIL_HOST")
    return {
        "configured": bool(host),
        "from": os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM") or "",
        "host": host or "",
        "port": os.getenv("SMTP_PORT") or os.getenv("EMAIL_PORT") or "",
        "auth": bool(os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")),
    }


class EmailTestBody(BaseModel):
    to: Optional[str] = None


@router.post("/email/test")
async def email_test(body: EmailTestBody = None):
    import asyncio
    import smtp_mailer
    if not smtp_mailer.is_configured():
        return _err(400, "not configured",
                    "No SMTP server configured. Set SMTP_HOST / SMTP_PORT / SMTP_FROM (and SMTP_USER/SMTP_PASS) in the environment.")
    to = (body.to.strip() if body and body.to else "") or smtp_mailer.default_from()
    try:
        loop = asyncio.get_running_loop()
        sent = await loop.run_in_executor(
            None, smtp_mailer.send, to, "Jarvis — SMTP test",
            "This is a test email from Jarvis. If you received it, scheduled report delivery is ready.",
            "<p>This is a test email from <b>Jarvis</b>. If you received it, scheduled report delivery is ready.</p>",
        )
        return {"ok": True, "detail": "Sent to " + ", ".join(sent)}
    except Exception as e:
        return _err(500, "send failed", str(e))


@router.get("/api-keys")
async def list_keys():
    return {"keys": [{k: v for k, v in rec.items() if k != "hash"} for rec in _keys()]}


class KeyBody(BaseModel):
    name: str
    expires_days: Optional[int] = None


@router.post("/api-keys")
async def create_key(body: KeyBody):
    if not body.name.strip():
        return _err(400, "name required", "Give the key a name.")
    raw = "jvs_" + secrets.token_urlsafe(24)
    rec = {
        "id": secrets.token_hex(6),
        "name": body.name.strip(),
        "prefix": raw[:12] + "…",
        "hash": _hash_key(raw),
        "created_at": time.time(),
        "last_used": None,
        "expires": (time.time() + body.expires_days * 86400) if body.expires_days else None,
    }
    with _lock:
        keys = _keys()
        keys.append(rec)
        _write(_KEYS_PATH, keys)
    # The full key is returned ONCE — it is never stored or shown again.
    return {"ok": True, "key": raw, "record": {k: v for k, v in rec.items() if k != "hash"}}


@router.delete("/api-keys/{kid}")
async def delete_key(kid: str):
    with _lock:
        keys = [k for k in _keys() if k.get("id") != kid]
        _write(_KEYS_PATH, keys)
    return {"ok": True, "deleted": kid}


# ── Maintenance — server logs + chat export ──────────────────────────────────
from pathlib import Path as _Path  # noqa: E402

_LOG_FILE = _Path(__file__).parent / "logs" / "backend.log"
_ASK_STORE = _Path.home() / ".config" / "jarvis" / "ask_history.json"


@router.get("/maintenance/logs")
async def maintenance_logs(lines: int = 300):
    try:
        if not _LOG_FILE.exists():
            return {"lines": [], "detail": f"No log file at {_LOG_FILE}"}
        content = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        n = max(10, min(int(lines or 300), 2000))
        return {"lines": content[-n:], "path": str(_LOG_FILE), "total": len(content)}
    except Exception as e:
        return _err(500, "log read failed", str(e))


@router.get("/maintenance/chat-export")
async def chat_export(format: str = "json"):
    from fastapi.responses import Response
    try:
        items = []
        if _ASK_STORE.exists():
            d = json.loads(_ASK_STORE.read_text(encoding="utf-8"))
            items = d.get("items", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    except Exception:
        items = []
    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["created_at", "user", "scope", "question", "answer"])
        for it in items:
            w.writerow([it.get("created_at", ""), it.get("user", ""), it.get("scope", ""),
                        (it.get("question", "") or "")[:2000], (it.get("answer", "") or "")[:4000]])
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=jarvis_chat_export.csv"})
    return {"count": len(items), "items": items}
