"""
auth — login + signed session tokens + an opt-in gate for hosting.

Reuses the existing user store (~/.config/jarvis/users.json), whose records hold
`password_salt` + `password_hash` = sha256(salt + password). Login verifies the
password and issues an HMAC-signed session token (no external deps).

The gate is OFF by default so local use is unchanged. Set JARVIS_REQUIRE_AUTH=1
(for hosting) and server.py's middleware will require a valid session token or a
valid API key on /api/* — except public paths (auth, health, the app shell, and
public share links).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

CFG_DIR = Path.home() / ".config" / "jarvis"
USERS_STORE = CFG_DIR / "users.json"
_SECRET_FILE = CFG_DIR / "auth_secret"

REQUIRED = os.getenv("JARVIS_REQUIRE_AUTH", "").strip() not in ("", "0", "false", "False")

# Paths reachable without a session (prefix match).
_PUBLIC_PREFIXES = (
    "/api/auth/", "/api/health", "/app", "/api/reports/public/",
)
_PUBLIC_EXACT = ("/", "/app")


def _secret() -> bytes:
    env = os.getenv("JARVIS_AUTH_SECRET")
    if env:
        return env.encode("utf-8")
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_bytes()
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        s = os.urandom(32)
        _SECRET_FILE.write_bytes(s)
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except Exception:
            pass
        return s
    except Exception:
        # Last resort — ephemeral (tokens won't survive restart)
        return b"jarvis-dev-secret-change-me"


def _users() -> list:
    try:
        d = json.loads(USERS_STORE.read_text(encoding="utf-8"))
        return d.get("users", []) if isinstance(d, dict) else []
    except Exception:
        return []


def verify_password(email: str, password: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    for u in _users():
        if (u.get("email", "").lower() == email) and u.get("password_hash"):
            salt = u.get("password_salt", "")
            if hashlib.sha256((salt + (password or "")).encode()).hexdigest() == u["password_hash"]:
                return u
    return None


def make_token(email: str, days: int = 7) -> str:
    payload = {"e": email, "x": int(time.time()) + days * 86400}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), "sha256").hexdigest()[:32]
    return raw + "." + sig


def verify_token(token: Optional[str]) -> Optional[str]:
    if not token or "." not in token:
        return None
    try:
        raw, sig = token.rsplit(".", 1)
        good = hmac.new(_secret(), raw.encode(), "sha256").hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        pad = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        if int(payload.get("x", 0)) < time.time():
            return None
        return payload.get("e")
    except Exception:
        return None


def is_public_path(path: str, method: str = "GET") -> bool:
    if method == "OPTIONS":
        return True
    if path in _PUBLIC_EXACT:
        return True
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    # Only /api/* is gated; static assets & the SPA are public.
    return not path.startswith("/api/")


def identify(request: Request) -> Optional[str]:
    """Return the authenticated email (or api-key name) for a request, or None."""
    auth = request.headers.get("Authorization") or ""
    tok = auth[7:].strip() if auth.lower().startswith("bearer ") else (request.headers.get("X-Jarvis-Token") or "")
    email = verify_token(tok)
    if email:
        return email
    key = request.headers.get("X-API-Key")
    if key:
        try:
            from settings_api import verify_api_key
            rec = verify_api_key(key)
            if rec:
                return "apikey:" + rec.get("name", "key")
        except Exception:
            pass
    # Embedded analytics: an embed token resolves to a tenant-scoped user whose
    # row-level-security props enforce isolation on every governed query.
    embed = request.headers.get("X-Embed-Token")
    if embed:
        try:
            from embed_api import email_from_embed
            e = email_from_embed(embed)
            if e:
                return e
        except Exception:
            pass
    return None


# ── API ──────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/config")
async def auth_config():
    return {"required": REQUIRED}


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginBody):
    u = verify_password(body.email, body.password)
    if not u:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    if (u.get("status") or "active") not in ("active",):
        return JSONResponse(status_code=403, content={"error": "account not active"})
    token = make_token(u.get("email"))
    return {"token": token, "user": {"email": u.get("email"), "name": u.get("name") or u.get("first_name"),
                                     "role": u.get("role") or "user"}}


@router.get("/me")
async def me(request: Request):
    email = identify(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    u = next((x for x in _users() if x.get("email", "").lower() == email.lower()), None)
    return {"email": email, "name": (u or {}).get("name"), "role": (u or {}).get("role") or "user"}


@router.post("/logout")
async def logout():
    # Stateless tokens — the client just discards it.
    return {"ok": True}
