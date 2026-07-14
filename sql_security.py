"""
sql_security — governance for raw SQL execution (Kaelio /admin/sql-security parity).

Two mechanisms, both applied server-side before any user-edited SQL runs:

  1. Row-level security via {{user.<key>}} tokens.
     Queries reference the running user's security properties, e.g.
         WHERE region = {{user.region}}
     The value is substituted from that user's properties. If a query references
     a property the user does not have, execution is refused (fail-closed) — a
     user can never see rows they weren't scoped to.

  2. A lightweight SQL policy (enforce toggle + table allowlist + column denylist)
     that blocks queries touching tables/columns outside what's permitted.

State is JSON under the semantic dir:
  - user_security.json : { "email": { "key": "value", ... }, ... }
  - sql_policy.json     : { enforce, allow_tables:[globs|"*"], deny_columns:[...] }
"""
from __future__ import annotations

import json
import re
import threading
from fnmatch import fnmatch
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

_lock = threading.Lock()
_USER_PATH = SEMANTIC_DIR / "user_security.json"
_POLICY_PATH = SEMANTIC_DIR / "sql_policy.json"

_USER_TOKEN_RE = re.compile(r"\{\{\s*user\.([a-zA-Z0-9_]+)\s*\}\}")
_TABLE_RE = re.compile(r"\b(?:from|join)\s+([`\"'\[]?[\w.\]]+)", re.I)
_DEFAULT_POLICY = {"enforce": False, "allow_tables": ["*"], "deny_columns": []}


class SecurityError(Exception):
    """Raised when a query is refused by row-security or policy (→ HTTP 403)."""


# ── stores ────────────────────────────────────────────────────────────────
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


def load_policy() -> dict:
    p = _read(_POLICY_PATH, {})
    return {**_DEFAULT_POLICY, **(p if isinstance(p, dict) else {})}


def save_policy(pol: dict) -> dict:
    clean = {
        "enforce": bool(pol.get("enforce")),
        "allow_tables": [str(t).strip() for t in (pol.get("allow_tables") or ["*"]) if str(t).strip()] or ["*"],
        "deny_columns": [str(c).strip() for c in (pol.get("deny_columns") or []) if str(c).strip()],
    }
    with _lock:
        _write(_POLICY_PATH, clean)
    return clean


def all_user_props() -> dict:
    d = _read(_USER_PATH, {})
    return d if isinstance(d, dict) else {}


def get_props(email: Optional[str]) -> dict:
    if not email:
        return {}
    return dict(all_user_props().get(email.lower(), {}))


def set_props(email: str, props: dict) -> dict:
    email = (email or "").strip().lower()
    if not email:
        raise SecurityError("email required")
    clean = {str(k).strip(): ("" if v is None else str(v)) for k, v in (props or {}).items() if str(k).strip()}
    with _lock:
        store = all_user_props()
        store[email] = clean
        _write(_USER_PATH, store)
    return clean


# ── core enforcement ────────────────────────────────────────────────────────
def _quote(v: str) -> str:
    s = str(v)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):      # numeric → raw
        return s
    return "'" + s.replace("'", "''") + "'"    # else safely single-quoted


def resolve_user_vars(sql: str, props: dict) -> str:
    """Substitute {{user.<key>}} from the user's props. Fail-closed on any missing key."""
    missing: list[str] = []

    def repl(m):
        key = m.group(1)
        val = props.get(key)
        if val is None or val == "":
            missing.append(key)
            return m.group(0)
        return _quote(val)

    out = _USER_TOKEN_RE.sub(repl, sql or "")
    if missing:
        uniq = sorted(set(missing))
        raise SecurityError(
            "Your account is missing required security "
            + ("property" if len(uniq) == 1 else "properties")
            + ": " + ", ".join(uniq)
            + ". Ask an admin to set " + ("it" if len(uniq) == 1 else "them") + " under Admin → Security."
        )
    return out


def _referenced_tables(sql: str) -> set[str]:
    out = set()
    for raw in _TABLE_RE.findall(sql or ""):
        name = raw.strip("`\"'[]").split(".")[-1].lower()
        if name:
            out.add(name)
    return out


def enforce(sql: str, email: Optional[str]) -> str:
    """Resolve {{user.x}} and apply the policy. Returns the safe SQL to execute.
    Raises SecurityError (→ 403) if the query is not permitted."""
    props = get_props(email)
    resolved = resolve_user_vars(sql, props)   # always — tokens must resolve
    pol = load_policy()
    if pol.get("enforce"):
        allow = pol.get("allow_tables") or ["*"]
        if "*" not in allow:
            allowed = [a.lower() for a in allow]
            for t in _referenced_tables(resolved):
                if not any(fnmatch(t, a) for a in allowed):
                    raise SecurityError(f"Table '{t}' is not permitted by the security policy.")
        for col in pol.get("deny_columns") or []:
            if re.search(r"\b" + re.escape(col) + r"\b", resolved, re.I):
                raise SecurityError(f"Column '{col}' is restricted by the security policy.")
    return resolved


# ── admin API ───────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/admin/security", tags=["security"])


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


@router.get("/policy")
async def api_get_policy():
    return load_policy()


class PolicyBody(BaseModel):
    enforce: bool = False
    allow_tables: list[str] = ["*"]
    deny_columns: list[str] = []


@router.put("/policy")
async def api_put_policy(body: PolicyBody):
    return save_policy(body.model_dump())


@router.get("/users")
async def api_list_users():
    store = all_user_props()
    return {"users": [{"email": e, "props": p, "count": len(p)} for e, p in sorted(store.items())]}


@router.get("/user-props/{email}")
async def api_get_user(email: str):
    return {"email": email.lower(), "props": get_props(email)}


class PropsBody(BaseModel):
    props: dict = {}


@router.put("/user-props/{email}")
async def api_set_user(email: str, body: PropsBody):
    try:
        return {"email": email.lower(), "props": set_props(email, body.props)}
    except SecurityError as e:
        return _err(400, "invalid", str(e))


class TestBody(BaseModel):
    sql: str
    email: Optional[str] = None


@router.post("/test")
async def api_test(body: TestBody, request: Request):
    email = body.email or (request.headers.get("X-Jarvis-User") or "").strip() or None
    try:
        resolved = enforce(body.sql, email)
        return {"ok": True, "resolved": resolved, "email": email}
    except SecurityError as e:
        return {"ok": False, "error": str(e), "email": email}
