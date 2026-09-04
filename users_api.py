"""
users_api — Jarvis Team admin (Users + Groups + Usage + Sharing scopes)
───────────────────────────────────────────────────────────────────────────
Mirrors the Kaelio /admin/users layout (Users / Groups / Usage tabs) plus
adds the share-scope queue Edgar specified:

  • Private          — visible only to the author
  • Group            — published to one or more groups the author belongs to
  • Corporate        — visible to everyone in the org (admin-only direct)
  • Pending          — non-admin sends "publish-to-corporate" through an
                       admin approval queue

Storage: ~/.config/jarvis/users.json + ~/.config/jarvis/usage.json
  (atomic write + timestamped backup)

Routes (prefix /api/team):
  GET    /users                   — list users
  POST   /users                   — invite/create user
  GET    /users/{id}              — get one
  PUT    /users/{id}              — update (role, status, name)
  DELETE /users/{id}              — delete
  GET    /users/me                — current user (resolved by header X-Jarvis-User or default)

  GET    /groups                  — list groups
  POST   /groups                  — create group
  PUT    /groups/{id}             — update
  DELETE /groups/{id}             — delete
  POST   /groups/{id}/members     — add members
  DELETE /groups/{id}/members/{uid} — remove member

  GET    /usage                   — usage stats (conversations / messages / active users)
                                    optional ?days=30
  GET    /usage/by_user            — per-user activity ranking
  GET    /usage/breakdown          — capability + feedback breakdown
  POST   /usage/record             — record a usage event (called by other endpoints)

  GET    /publish_queue            — list pending corporate-publish requests (admin only)
  POST   /publish_queue/{id}/approve — approve and publish
  POST   /publish_queue/{id}/reject  — reject and notify the author
"""

from __future__ import annotations
import json, os, time, uuid, shutil, threading
from pathlib import Path
from typing import Optional, Any
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/team", tags=["team"])

CFG_DIR    = Path(os.path.expanduser("~/.config/jarvis"))
USERS_STORE = CFG_DIR / "users.json"
USAGE_STORE = CFG_DIR / "usage.json"
_lock = threading.Lock()

# Sharing scopes — used by dashboards + AI questions + briefs
SCOPES = ("private", "group", "corporate", "pending")
ROLES  = ("user", "admin")


# ─── Storage helpers ─────────────────────────────────────────────────
def _load_users() -> dict:
    if not USERS_STORE.exists():
        return {"users": [], "groups": [], "publish_queue": []}
    try:
        return json.loads(USERS_STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"users": [], "groups": [], "publish_queue": []}


def _save_users(data: dict):
    USERS_STORE.parent.mkdir(parents=True, exist_ok=True)
    if USERS_STORE.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try: shutil.copy2(USERS_STORE, USERS_STORE.with_suffix(f".json.bak.{ts}"))
        except OSError: pass
    tmp = USERS_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(USERS_STORE)
    try: os.chmod(USERS_STORE, 0o600)
    except OSError: pass


def _load_usage() -> dict:
    if not USAGE_STORE.exists():
        return {"events": []}
    try:
        return json.loads(USAGE_STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"events": []}


def _save_usage(data: dict):
    USAGE_STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(USAGE_STORE)
    try: os.chmod(USAGE_STORE, 0o600)
    except OSError: pass


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


# ─── Password policy ─────────────────────────────────────────────────
# Enforced server-side on every path that can set a password (create, update,
# invite/reset). The UI mirrors these rules, but the server is the authority.
PW_MIN_LEN = 10

PW_RULES = [
    f"at least {PW_MIN_LEN} characters",
    "an uppercase letter (A–Z)",
    "a lowercase letter (a–z)",
    "a number (0–9)",
    "a special character (!@#$…)",
    "no spaces",
]


def _password_problems(pw: str, user: Optional[dict] = None) -> list:
    """Return a list of unmet requirements. Empty list == acceptable password."""
    import re
    p = pw or ""
    probs = []
    if len(p) < PW_MIN_LEN:
        probs.append(f"at least {PW_MIN_LEN} characters")
    if not re.search(r"[A-Z]", p):
        probs.append("an uppercase letter (A–Z)")
    if not re.search(r"[a-z]", p):
        probs.append("a lowercase letter (a–z)")
    if not re.search(r"[0-9]", p):
        probs.append("a number (0–9)")
    if not re.search(r"[^A-Za-z0-9\s]", p):
        probs.append("a special character (!@#$…)")
    if re.search(r"\s", p):
        probs.append("no spaces")
    # Must not embed the person's own identifiers — a standard check.
    if user:
        low = p.lower()
        for key in ("email", "first_name", "last_name", "user_id", "name"):
            v = str(user.get(key) or "").split("@")[0].strip().lower()
            if len(v) >= 3 and v in low:
                probs.append("must not contain your own name or email")
                break
    return probs


def _pw_err(probs: list):
    return _err(400, "weak password", "Password must include: " + "; ".join(probs))


@router.get("/password_policy")
async def password_policy():
    """So the UI can render the exact rules the server enforces."""
    return {"min_length": PW_MIN_LEN, "rules": PW_RULES}


def _current_user(x_jarvis_user: Optional[str]) -> dict:
    """Resolve the current user from header. Falls back to the first admin or a default."""
    data = _load_users()
    if x_jarvis_user:
        u = next((u for u in data["users"] if u.get("email") == x_jarvis_user or u.get("id") == x_jarvis_user), None)
        if u: return u
    # Fallback: first admin, else a default admin record
    admin = next((u for u in data["users"] if u.get("role") == "admin"), None)
    if admin: return admin
    return {"id":"system","name":"System","email":"system@local","role":"admin","status":"active","groups":[]}


# ─── Pydantic models ────────────────────────────────────────────────
class UserIn(BaseModel):
    """Create / update payload.

    The Create flow now accepts identity fields (first/last name, user_id) and
    an OPTIONAL admin-set initial password. Invite is a SEPARATE action — see
    POST /users/{id}/invite. When the user clicks the invite link they can
    reset whatever password the admin set."""
    email: str
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    name: Optional[str] = ""           # derived from first+last if blank
    user_id: Optional[str] = None      # custom user id; defaults to email
    password: Optional[str] = None     # OPTIONAL — admin can pre-seed; min 8 chars
    role: str = "user"          # user | admin
    status: str = "active"       # active | inactive | invited | created
    groups: list[str] = []       # group ids


class GroupIn(BaseModel):
    name: str
    description: Optional[str] = ""


class UsageEvent(BaseModel):
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    kind: str                    # message | conversation | sql_query | dashboard_view | report_run | etc.
    metadata: dict = {}


# ─── User endpoints ─────────────────────────────────────────────────
@router.get("/users")
async def list_users():
    data = _load_users()
    return {"users": data.get("users", []), "count": len(data.get("users", []))}


@router.post("/users")
async def create_user(body: UserIn):
    """Create a user. Does NOT send an invite email — that's a separate action
    via POST /users/{id}/invite. The admin may optionally pre-set a password;
    if they don't, the user must be invited and choose one on first sign-in."""
    import secrets, hashlib   # local to keep top of file unchanged
    if body.role not in ROLES:
        return _err(400, "invalid role", f"role must be one of {ROLES}")
    if body.status not in ("active", "inactive", "invited", "created"):
        return _err(400, "invalid status", "status must be active, inactive, invited, or created")
    if body.password:
        probs = _password_problems(body.password, body.model_dump())
        if probs:
            return _pw_err(probs)
    with _lock:
        data = _load_users()
        if any(u.get("email") == body.email for u in data["users"]):
            return _err(409, "already exists", f"User {body.email} already exists")
        if body.user_id and any(u.get("user_id") == body.user_id for u in data["users"]):
            return _err(409, "user_id taken", f"user_id {body.user_id} is already in use")
        u = body.model_dump(exclude={"password"})
        # Derive display name from first/last when 'name' wasn't supplied
        if not u.get("name"):
            u["name"] = " ".join(filter(None, [u.get("first_name"), u.get("last_name")])).strip() or body.email
        # Default user_id to the email address (Kaelio uses the email as the login handle)
        if not u.get("user_id"):
            u["user_id"] = body.email
        u["id"] = str(uuid.uuid4())
        u["created_at"] = time.time()
        u["updated_at"] = time.time()
        # Password handling — admin-set, separate from invite
        if body.password:
            salt = secrets.token_hex(8)
            u["password_salt"] = salt
            u["password_hash"] = hashlib.sha256((salt + body.password).encode()).hexdigest()
            u["password_set_at"] = time.time()
            u["password_set_by"] = "admin"
        else:
            u["password_hash"] = None
        # Sensible default: if the admin didn't seed a password AND didn't
        # explicitly request a status, mark the user 'created' so they can't
        # sign in until the admin sends an invite.
        if not body.password and body.status == "active":
            # body.status is the Pydantic default, treat as "no explicit status"
            u["status"] = "created"
        u["invited_at"] = None  # filled in only when /users/{id}/invite is called
        data["users"].append(u)
        _save_users(data)
    # Strip the secret hash before returning
    safe = dict(u); safe.pop("password_hash", None); safe.pop("password_salt", None)
    return safe


@router.get("/users/me")
async def get_me(x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    return _current_user(x_jarvis_user)


@router.get("/users/{uid}")
async def get_user(uid: str):
    u = next((u for u in _load_users().get("users", []) if u.get("id") == uid or u.get("email") == uid), None)
    if not u: return _err(404, "not found")
    return u


@router.put("/users/{uid}")
async def update_user(uid: str, body: UserIn):
    """Update a user. A password supplied here is HASHED (salt + sha256) exactly
    like the create path — the raw value is never persisted. Omit/blank the
    password to leave the existing one untouched."""
    import secrets, hashlib
    if body.password:
        probs = _password_problems(body.password, body.model_dump())
        if probs:
            return _pw_err(probs)
    with _lock:
        data = _load_users()
        u = next((u for u in data["users"] if u.get("id") == uid or u.get("email") == uid), None)
        if not u: return _err(404, "not found")
        patch = body.model_dump()
        pw = patch.pop("password", None)          # never let a raw password reach the store
        u.update(patch)
        if pw:
            salt = secrets.token_hex(16)
            u["password_salt"] = salt
            u["password_hash"] = hashlib.sha256((salt + pw).encode()).hexdigest()
            u["password_set_by"] = "admin"
            if (u.get("status") or "") in ("created", "invited", ""):
                u["status"] = "active"            # a user with a password can sign in
        u.pop("password", None)                   # belt-and-braces: strip any legacy plaintext
        u["updated_at"] = time.time()
        _save_users(data)
    safe = dict(u); safe.pop("password_hash", None); safe.pop("password_salt", None)
    return safe


@router.delete("/users/{uid}")
async def delete_user(uid: str):
    with _lock:
        data = _load_users()
        before = len(data["users"])
        data["users"] = [u for u in data["users"] if u.get("id") != uid and u.get("email") != uid]
        if len(data["users"]) == before: return _err(404, "not found")
        _save_users(data)
    return {"ok": True, "deleted": uid}


# ─── Invitation + password setup flow ──────────────────────────────
import secrets, hashlib

class InvitePayload(BaseModel):
    email: str
    name: Optional[str] = ""
    role: str = "user"
    groups: list[str] = []
    notes: Optional[str] = ""

class SetupPayload(BaseModel):
    token: str
    password: str


@router.post("/users/invite")
async def invite_user(body: InvitePayload):
    """Generate an invitation token + email link. Creates user in 'invited' state.
    If the user already exists, generates a new token (resend flow)."""
    if body.role not in ROLES:
        return _err(400, "invalid role")
    with _lock:
        data = _load_users()
        u = next((u for u in data["users"] if u.get("email") == body.email), None)
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + 7 * 86400  # 7-day expiry
        invite = {
            "token": token,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": expires_at,
            "issued_at": time.time(),
            "consumed": False,
        }
        if u:
            # Resend — regenerate token
            u["invite"] = invite
            u["status"] = "invited"
            u["updated_at"] = time.time()
            action = "resent"
        else:
            u = {
                "id": str(uuid.uuid4()),
                "email": body.email,
                "name": body.name or "",
                "role": body.role,
                "status": "invited",
                "groups": body.groups,
                "notes": body.notes or "",
                "invite": invite,
                "created_at": time.time(),
                "updated_at": time.time(),
                "invited_at": time.time(),
            }
            data["users"].append(u)
            action = "created"
        _save_users(data)
    # In a real deployment this would send the email; here we return the link so the
    # admin can copy / forward it manually until SMTP is wired.
    invite_link = f"/signup?token={token}&email={body.email}"
    return {
        "ok": True,
        "action": action,
        "user": u,
        "invite_link": invite_link,
        "expires_at": expires_at,
        "note": "Email delivery isn't wired yet — copy this link to the user. POST /api/team/auth/setup-password with token + password to complete signup."
    }


@router.post("/auth/setup-password")
async def setup_password(body: SetupPayload):
    """Consume an invite token and set the user's password."""
    if not body.password or len(body.password) < 8:
        return _err(400, "weak password", "Password must be at least 8 characters")
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    with _lock:
        data = _load_users()
        u = next((u for u in data["users"] if u.get("invite", {}).get("token_hash") == token_hash), None)
        if not u: return _err(404, "invalid token")
        inv = u.get("invite", {})
        if inv.get("consumed"): return _err(400, "already consumed")
        if inv.get("expires_at", 0) < time.time(): return _err(400, "expired")
        # Persist hashed password
        salt = secrets.token_hex(8)
        u["password_salt"] = salt
        u["password_hash"] = hashlib.sha256((salt + body.password).encode()).hexdigest()
        u["status"] = "active"
        u["activated_at"] = time.time()
        inv["consumed"] = True
        inv["consumed_at"] = time.time()
        _save_users(data)
    return {"ok": True, "user_id": u["id"], "email": u["email"], "message": "Password set — you can now sign in"}


def _issue_invite_for(u: dict) -> dict:
    """Mint a fresh invite token on the user record and return the invite envelope.
    Caller must hold _lock and persist. Shared by /users/{id}/invite and the
    legacy /users/{id}/resend_invite alias."""
    token = secrets.token_urlsafe(24)
    invite = {
        "token": token,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": time.time() + 7 * 86400,
        "issued_at": time.time(),
        "consumed": False,
    }
    u["invite"] = invite
    u["status"] = "invited"
    u["invited_at"] = time.time()
    u["updated_at"] = time.time()
    return invite


@router.post("/users/{uid}/invite")
async def send_user_invite(uid: str):
    """Send the invite email for an EXISTING user. The link expires in 7 days
    and lets the user RESET whatever password the admin pre-seeded. Calling
    this twice rotates the token (so a leaked one stops working)."""
    with _lock:
        data = _load_users()
        u = next((u for u in data["users"] if u.get("id") == uid or u.get("email") == uid or u.get("user_id") == uid), None)
        if not u: return _err(404, "not found")
        inv = _issue_invite_for(u)
        _save_users(data)
    invite_link = f"/signup?token={inv['token']}&email={u['email']}"
    body_text = (
        f"Hi {u.get('first_name') or u.get('name') or u.get('email')},\n\n"
        "You've been invited to Jarvis. Click the link below within 7 days to "
        "finish setting up your account and choose your password:\n\n"
        f"{invite_link}\n\n— The Jarvis team"
    )
    return {
        "ok": True,
        "user_id": u["id"],
        "email": u["email"],
        "invite_link": invite_link,
        "expires_at": inv["expires_at"],
        "mailto": f"mailto:{u['email']}?subject=Welcome%20to%20Jarvis&body=" + body_text.replace("\n","%0A").replace(" ","%20"),
        "note": "SMTP isn't wired yet — copy the invite_link to the user, or click the mailto: link to open your mail client pre-filled."
    }


@router.post("/users/{uid}/resend_invite")
async def resend_invite(uid: str):
    """Legacy alias for /users/{id}/invite — rotates the token and returns the link."""
    return await send_user_invite(uid)


# ─── Group endpoints ────────────────────────────────────────────────
@router.get("/groups")
async def list_groups():
    data = _load_users()
    # Annotate each group with current member count
    out = []
    for g in data.get("groups", []):
        n = sum(1 for u in data["users"] if g["id"] in (u.get("groups") or []))
        gg = dict(g); gg["member_count"] = n
        out.append(gg)
    return {"groups": out, "count": len(out)}


@router.post("/groups")
async def create_group(body: GroupIn):
    with _lock:
        data = _load_users()
        if any(g["name"].lower() == body.name.lower() for g in data["groups"]):
            return _err(409, "already exists")
        g = body.model_dump()
        g["id"] = str(uuid.uuid4())
        g["created_at"] = time.time()
        data["groups"].append(g)
        _save_users(data)
    return g


@router.put("/groups/{gid}")
async def update_group(gid: str, body: GroupIn):
    with _lock:
        data = _load_users()
        g = next((g for g in data["groups"] if g["id"] == gid), None)
        if not g: return _err(404, "not found")
        g.update(body.model_dump())
        g["updated_at"] = time.time()
        _save_users(data)
    return g


@router.delete("/groups/{gid}")
async def delete_group(gid: str):
    with _lock:
        data = _load_users()
        before = len(data["groups"])
        data["groups"] = [g for g in data["groups"] if g["id"] != gid]
        if len(data["groups"]) == before: return _err(404, "not found")
        # Remove from users' group lists
        for u in data["users"]:
            u["groups"] = [x for x in (u.get("groups") or []) if x != gid]
        _save_users(data)
    return {"ok": True, "deleted": gid}


@router.post("/groups/{gid}/members")
async def add_members(gid: str, body: dict):
    user_ids = body.get("user_ids", [])
    with _lock:
        data = _load_users()
        if not any(g["id"] == gid for g in data["groups"]): return _err(404, "group not found")
        for uid in user_ids:
            u = next((u for u in data["users"] if u["id"] == uid or u["email"] == uid), None)
            if u and gid not in (u.get("groups") or []):
                u.setdefault("groups", []).append(gid)
        _save_users(data)
    return {"ok": True, "added": user_ids}


@router.delete("/groups/{gid}/members/{uid}")
async def remove_member(gid: str, uid: str):
    with _lock:
        data = _load_users()
        u = next((u for u in data["users"] if u["id"] == uid or u["email"] == uid), None)
        if not u: return _err(404, "user not found")
        u["groups"] = [x for x in (u.get("groups") or []) if x != gid]
        _save_users(data)
    return {"ok": True, "removed": uid}


# ─── Usage endpoints ────────────────────────────────────────────────
@router.get("/usage")
async def usage_summary(
    days: int = 30,
    from_ts: Optional[float] = None,
    to_ts:   Optional[float] = None,
):
    """KPIs + daily activity series. Accepts either days=N (rolling) OR from_ts/to_ts (absolute window)."""
    now = time.time()
    if from_ts is None or to_ts is None:
        from_ts = now - (days * 86400)
        to_ts   = now
    events = [e for e in _load_usage()["events"] if from_ts <= e.get("at", 0) <= to_ts]
    conversations = sum(1 for e in events if e["kind"] == "conversation")
    messages      = sum(1 for e in events if e["kind"] == "message")
    active_users  = len({e.get("user_id") or e.get("user_email") for e in events if e.get("user_id") or e.get("user_email")})
    # Daily series — fills in zeros so the chart has a continuous x-axis
    by_day = {}
    for e in events:
        day = time.strftime("%Y-%m-%d", time.gmtime(e["at"]))
        by_day.setdefault(day, {"messages": 0, "conversations": 0})
        if e["kind"] == "message":      by_day[day]["messages"]      += 1
        if e["kind"] == "conversation": by_day[day]["conversations"] += 1
    # Fill missing days in the window
    import datetime as _dt
    cur = _dt.datetime.utcfromtimestamp(from_ts).date()
    end = _dt.datetime.utcfromtimestamp(to_ts).date()
    series = []
    while cur <= end:
        d = cur.isoformat()
        series.append({"date": d, "messages": (by_day.get(d) or {}).get("messages", 0), "conversations": (by_day.get(d) or {}).get("conversations", 0)})
        cur += _dt.timedelta(days=1)
    return {
        "window": {"from": from_ts, "to": to_ts},
        "kpis": {"conversations": conversations, "messages": messages, "active_users": active_users},
        "daily": series
    }


@router.get("/usage/by_user")
async def usage_by_user(
    days: int = 30,
    from_ts: Optional[float] = None,
    to_ts:   Optional[float] = None,
):
    now = time.time()
    if from_ts is None or to_ts is None:
        from_ts = now - (days * 86400)
        to_ts   = now
    events = [e for e in _load_usage()["events"] if from_ts <= e.get("at", 0) <= to_ts]
    counts = {}
    for e in events:
        key = e.get("user_email") or e.get("user_id") or "unknown"
        counts[key] = counts.get(key, 0) + (1 if e["kind"] == "message" else 0)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return {"users": [{"user": u, "messages": c} for u, c in ranked]}


@router.get("/usage/breakdown")
async def usage_breakdown(
    days: int = 30,
    from_ts: Optional[float] = None,
    to_ts:   Optional[float] = None,
):
    """Returns the three breakdown panels Edgar sees on the Kaelio usage page:
      • messages_by_source — donut (Research / SCHEDULED_RESEARCH / METRIC_WORKSHOP, etc.)
      • ai_capabilities    — ranked list (SQL Queries, Create Widget, Data Lookup, …)
      • feedback           — thumbs-up/down counts
    """
    now = time.time()
    if from_ts is None or to_ts is None:
        from_ts = now - (days * 86400)
        to_ts   = now
    events = [e for e in _load_usage()["events"] if from_ts <= e.get("at", 0) <= to_ts]

    # Source mix — pulls from metadata.source on each event (defaults to RESEARCH for backwards compat)
    src_counts = {}
    for e in events:
        if e["kind"] != "message": continue
        src = (e.get("metadata") or {}).get("source") or "RESEARCH"
        src_counts[src] = src_counts.get(src, 0) + 1
    total_msgs = sum(src_counts.values())
    messages_by_source = [
        {"name": k, "value": v, "pct": round(100*v/total_msgs, 1) if total_msgs else 0}
        for k, v in sorted(src_counts.items(), key=lambda kv: -kv[1])
    ]

    # AI capabilities — counts events where metadata.capability is set
    cap_counts = {}
    for e in events:
        cap = (e.get("metadata") or {}).get("capability")
        if not cap: continue
        cap_counts[cap] = cap_counts.get(cap, 0) + 1
    ai_capabilities = [
        {"name": k, "value": v}
        for k, v in sorted(cap_counts.items(), key=lambda kv: -kv[1])
    ]

    # Feedback — counts events with metadata.feedback in (up, down)
    up = sum(1 for e in events if (e.get("metadata") or {}).get("feedback") == "up")
    dn = sum(1 for e in events if (e.get("metadata") or {}).get("feedback") == "down")

    return {
        "messages_by_source": messages_by_source,
        "ai_capabilities": ai_capabilities,
        "feedback": {"up": up, "down": dn, "total": up + dn},
        "total_events": len(events),
    }


@router.get("/usage/export")
async def usage_export(days: int = 30):
    """CSV export of every event in the window — Kaelio's "Export" button equivalent."""
    import csv, io
    from fastapi.responses import StreamingResponse
    cutoff = time.time() - (days * 86400)
    events = [e for e in _load_usage()["events"] if e.get("at", 0) >= cutoff]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc","user_email","kind","source","capability","feedback","metadata_json"])
    for e in sorted(events, key=lambda x: x.get("at", 0)):
        meta = e.get("metadata") or {}
        w.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.get("at", 0))),
            e.get("user_email") or e.get("user_id") or "",
            e.get("kind") or "",
            meta.get("source") or "",
            meta.get("capability") or "",
            meta.get("feedback") or "",
            json.dumps(meta)
        ])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=jarvis_usage_{int(time.time())}.csv"}
    )


@router.post("/usage/record")
async def record_usage(body: UsageEvent):
    with _lock:
        data = _load_usage()
        ev = body.model_dump()
        ev["at"] = time.time()
        ev["id"] = str(uuid.uuid4())
        data["events"].append(ev)
        # Cap events to last 100k to keep file size sane
        if len(data["events"]) > 100000:
            data["events"] = data["events"][-100000:]
        _save_usage(data)
    return {"ok": True, "event_id": ev["id"]}


# ─── Publish queue (corporate-share approval) ───────────────────────
@router.get("/publish_queue")
async def list_publish_queue(x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    if me.get("role") != "admin":
        return _err(403, "admin only", "Only admins can see the publish queue")
    return {"queue": _load_users().get("publish_queue", [])}


@router.post("/publish_queue")
async def submit_publish_request(body: dict, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    """A non-admin submits a piece of content for corporate-wide publish review."""
    me = _current_user(x_jarvis_user)
    with _lock:
        data = _load_users()
        req = {
            "id": str(uuid.uuid4()),
            "kind": body.get("kind", "ask"),                # ask | dashboard | brief | etc.
            "object_id": body.get("object_id"),
            "title": body.get("title", ""),
            "summary": body.get("summary", ""),
            "submitted_by_id": me.get("id"),
            "submitted_by_email": me.get("email"),
            "submitted_at": time.time(),
            "status": "pending",
        }
        data.setdefault("publish_queue", []).append(req)
        _save_users(data)
    return req


@router.post("/publish_queue/{rid}/approve")
async def approve_publish(rid: str, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    if me.get("role") != "admin":
        return _err(403, "admin only")
    with _lock:
        data = _load_users()
        r = next((r for r in data.get("publish_queue", []) if r["id"] == rid), None)
        if not r: return _err(404, "not found")
        r["status"] = "approved"
        r["approved_by"] = me.get("email")
        r["approved_at"] = time.time()
        _save_users(data)
    return r


@router.post("/publish_queue/{rid}/reject")
async def reject_publish(rid: str, body: dict, x_jarvis_user: Optional[str] = Header(None, alias="X-Jarvis-User")):
    me = _current_user(x_jarvis_user)
    if me.get("role") != "admin":
        return _err(403, "admin only")
    with _lock:
        data = _load_users()
        r = next((r for r in data.get("publish_queue", []) if r["id"] == rid), None)
        if not r: return _err(404, "not found")
        r["status"] = "rejected"
        r["rejected_by"] = me.get("email")
        r["rejected_at"] = time.time()
        r["rejection_note"] = body.get("note", "")
        _save_users(data)
    return r
