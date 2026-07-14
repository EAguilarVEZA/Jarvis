"""Embedded analytics + multi-tenant provisioning API (prefix /api/embed).

Lets a partner (e.g. Ours Privacy) drop our governed analytics into their app for
each of their customers, with hard per-tenant isolation:

  Tenant admin (session-authed):
    GET/POST/DELETE /api/embed/tenants           — manage tenants
    POST            /api/embed/token             — mint a short-lived embed token
    GET             /api/embed/verify            — inspect a token (debug)

  Partner provisioning (SCIM/JIT, per-tenant bearer token):
    POST            /api/embed/provision         — create/attach a tenant user

  Embed runtime (embed token):
    GET             /api/embed/context           — tenant theme + who am I

Isolation model: an embed token carries a tenant-scoped user email whose
row-level-security props (tenant_id + any tenant filters) are stored via
sql_security. Because every governed query runs `sql_security.enforce(sql, email)`,
a tenant's users can only ever see their own rows — enforced in SQL, not the UI.

Embed tokens are HMAC-signed with the app secret (reusing auth._secret()). NOTE for
production: switch to RS256 asymmetric signing and put embed reads behind a
read-only role — see OURS_PRIVACY_INTEGRATION_PLAN.md §7.
"""
from __future__ import annotations

import time
import json
import hmac
import base64
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

import tenancy

router = APIRouter(prefix="/api/embed", tags=["embed"])

_EMBED_PREFIX = "emb1"   # token version marker


def _secret() -> bytes:
    try:
        from auth import _secret as _auth_secret
        return _auth_secret()
    except Exception:
        import os
        return (os.getenv("JARVIS_AUTH_SECRET") or "jarvis-dev-secret-change-me").encode()


def make_embed_token(claims: dict, ttl_minutes: int = 60) -> str:
    payload = dict(claims)
    payload["x"] = int(time.time()) + int(ttl_minutes) * 60
    payload["v"] = _EMBED_PREFIX
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), "sha256").hexdigest()[:32]
    return raw + "." + sig


def verify_embed_token(token: Optional[str]) -> Optional[dict]:
    """Return the claims dict if the token is valid + unexpired, else None."""
    if not token or "." not in token:
        return None
    try:
        raw, sig = token.rsplit(".", 1)
        good = hmac.new(_secret(), raw.encode(), "sha256").hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        pad = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
        if payload.get("v") != _EMBED_PREFIX:
            return None
        if int(payload.get("x", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def email_from_embed(token: Optional[str]) -> Optional[str]:
    """Hook used by auth.identify — resolve an embed token to its scoped user email."""
    claims = verify_embed_token(token)
    if claims and claims.get("email"):
        return claims["email"]
    return None


def _err(status, error, detail=""):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


# ── Tenant management (session-authed via the app's normal gate) ──────────────
class TenantBody(BaseModel):
    id: Optional[str] = None
    name: str
    theme: Optional[dict] = None
    warehouse_dataset: Optional[str] = ""
    rls_props: Optional[dict] = None
    allowed_domains: Optional[list] = None
    sso: Optional[dict] = None


@router.get("/tenants")
async def list_tenants():
    # Never leak scim tokens in the list view.
    out = []
    for t in tenancy.list_tenants():
        r = dict(t); r.pop("scim_token", None)
        out.append(r)
    return {"tenants": out}


@router.post("/tenants")
async def upsert_tenant(body: TenantBody):
    if not (body.name or "").strip():
        return _err(400, "name required", "A tenant needs a name.")
    t = tenancy.upsert_tenant(body.dict(exclude_none=True))
    return {"ok": True, "tenant": t}   # includes scim_token so admin can copy it once


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str):
    ok = tenancy.delete_tenant(tenant_id)
    return {"ok": ok}


# ── Mint an embed token for a tenant user ─────────────────────────────────────
class TokenBody(BaseModel):
    tenant_id: str
    email: Optional[str] = None       # the end-user's identity in the tenant
    role: Optional[str] = "viewer"
    ttl_minutes: Optional[int] = 60
    extra_props: Optional[dict] = None


@router.post("/token")
async def mint_token(body: TokenBody):
    t = tenancy.get_tenant(body.tenant_id)
    if not t:
        return _err(404, "unknown tenant", body.tenant_id)
    email = (body.email or f"embed@{t['id']}").strip().lower()
    props = dict(t.get("rls_props") or {})
    if body.extra_props:
        props.update(body.extra_props)
    # Persist the scoped user's props so sql_security enforces isolation on every query.
    try:
        import sql_security
        sql_security.set_props(email, props)
    except Exception:
        pass
    token = make_embed_token({"email": email, "tenant_id": t["id"], "role": body.role or "viewer"},
                             ttl_minutes=body.ttl_minutes or 60)
    return {"ok": True, "token": token, "email": email, "tenant_id": t["id"],
            "expires_in_minutes": body.ttl_minutes or 60, "props_applied": props}


@router.get("/verify")
async def verify(token: str):
    claims = verify_embed_token(token)
    return {"valid": bool(claims), "claims": claims or {}}


# ── SCIM / JIT provisioning (partner-authenticated per tenant) ────────────────
class ProvisionBody(BaseModel):
    scim_token: str
    email: str
    role: Optional[str] = "viewer"
    extra_props: Optional[dict] = None


@router.post("/provision")
async def provision(body: ProvisionBody):
    """JIT-provision a tenant user. Authenticated by the tenant's own scim_token, so
    one tenant can never provision users into another (per-tenant scoping)."""
    t = tenancy.tenant_by_scim_token(body.scim_token)
    if not t:
        return _err(401, "bad provisioning token", "scim_token did not match a tenant")
    email = (body.email or "").strip().lower()
    if not email:
        return _err(400, "email required")
    props = dict(t.get("rls_props") or {})
    if body.extra_props:
        props.update(body.extra_props)
    try:
        import sql_security
        sql_security.set_props(email, props)
    except Exception as e:
        return _err(500, "provisioning failed", str(e))
    return {"ok": True, "tenant_id": t["id"], "email": email, "role": body.role or "viewer",
            "props_applied": props}


# ── Per-tenant SSO config scaffold ────────────────────────────────────────────
class SSOBody(BaseModel):
    type: str                          # 'oidc' | 'saml'
    issuer: Optional[str] = ""
    client_id: Optional[str] = ""
    metadata_url: Optional[str] = ""
    entity_id: Optional[str] = ""


@router.post("/tenants/{tenant_id}/sso")
async def set_sso(tenant_id: str, body: SSOBody):
    t = tenancy.get_tenant(tenant_id)
    if not t:
        return _err(404, "unknown tenant", tenant_id)
    t = dict(t); t["sso"] = body.dict(exclude_none=True)
    tenancy.upsert_tenant(t)
    return {"ok": True, "sso": t["sso"],
            "note": "Config stored. Live IdP handshake (OIDC/SAML assertion validation) is a hardening step; see plan §7."}


# ── Embed runtime context (embed-token authed) ────────────────────────────────
@router.get("/context")
async def embed_context(request: Request):
    tok = request.headers.get("X-Embed-Token") or request.query_params.get("embed_token") or ""
    claims = verify_embed_token(tok)
    if not claims:
        return _err(401, "invalid embed token")
    t = tenancy.get_tenant(claims.get("tenant_id"))
    return {"ok": True, "tenant": tenancy.public_view(t), "role": claims.get("role", "viewer"),
            "user": claims.get("email")}
