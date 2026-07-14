"""Multi-tenancy for embedded analytics (P1 of the Ours Privacy integration).

A *tenant* is one of the CDP's customers (e.g. a health system) whose users see
our analytics embedded in the partner's platform. Each tenant carries:
  • rls_props   — the row-level-security properties every tenant user inherits
                  (e.g. {"tenant_id": "acme", "region": "FL"}). These flow into
                  sql_security's {{user.<key>}} substitution, so a tenant's users
                  can ONLY ever see their own rows — isolation is enforced in SQL.
  • warehouse_dataset — where this tenant's Ours-landed data lives.
  • theme       — white-label look (brand name, accent color, logo).
  • sso         — per-tenant IdP config (OIDC/SAML) for their users.
  • scim_token  — a per-tenant bearer token for JIT/SCIM user provisioning
                  (scoped so one tenant can never provision into another).

Store: tenants.json. Pure store + helpers; embed_api.py exposes the HTTP surface.
Security note: production should move rls enforcement reviews + RS256 embed-token
signing + a real IdP handshake through a security review (see integration plan).
"""
from __future__ import annotations

import os
import json
import time
import secrets

_DIR = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_DIR, "tenants.json")


def _load() -> list:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("tenants", [])
    except Exception:
        return []


def _save(tenants: list):
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tenants": tenants}, f, indent=2)
    os.replace(tmp, _PATH)


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or ("t-" + secrets.token_hex(3))


def list_tenants() -> list:
    return _load()


def get_tenant(tenant_id: str) -> dict | None:
    for t in _load():
        if t.get("id") == tenant_id:
            return t
    return None


def upsert_tenant(d: dict) -> dict:
    tenants = _load()
    d = dict(d)
    tid = d.get("id") or _slug(d.get("name", ""))
    d["id"] = tid
    # rls_props always carries tenant_id so isolation holds even if caller forgets.
    props = dict(d.get("rls_props") or {})
    props.setdefault("tenant_id", tid)
    d["rls_props"] = props
    d.setdefault("theme", {})
    d.setdefault("sso", {})
    d.setdefault("allowed_domains", [])
    d.setdefault("created_at", time.time())
    d["updated_at"] = time.time()
    # Mint a SCIM/provisioning token once, keep it stable across updates.
    for existing in tenants:
        if existing.get("id") == tid:
            d.setdefault("scim_token", existing.get("scim_token"))
            d.setdefault("created_at", existing.get("created_at", d["created_at"]))
            break
    if not d.get("scim_token"):
        d["scim_token"] = "scim_" + secrets.token_urlsafe(24)
    for i, t in enumerate(tenants):
        if t.get("id") == tid:
            tenants[i] = d
            break
    else:
        tenants.append(d)
    _save(tenants)
    return d


def delete_tenant(tenant_id: str) -> bool:
    tenants = _load()
    kept = [t for t in tenants if t.get("id") != tenant_id]
    _save(kept)
    return len(kept) != len(tenants)


def tenant_by_scim_token(token: str) -> dict | None:
    if not token:
        return None
    for t in _load():
        if t.get("scim_token") and secrets.compare_digest(str(t["scim_token"]), str(token)):
            return t
    return None


def tenant_for_domain(domain: str) -> dict | None:
    """Resolve a tenant from an email domain (for SSO/JIT tenant discovery)."""
    domain = (domain or "").lower().strip()
    if not domain:
        return None
    for t in _load():
        if domain in [d.lower().strip() for d in (t.get("allowed_domains") or [])]:
            return t
    return None


def public_view(t: dict) -> dict:
    """Tenant record safe to expose to a browser embed (no scim_token / secrets)."""
    if not t:
        return {}
    return {"id": t.get("id"), "name": t.get("name"), "theme": t.get("theme", {}),
            "warehouse_dataset": t.get("warehouse_dataset", "")}
