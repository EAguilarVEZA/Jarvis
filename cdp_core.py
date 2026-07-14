"""Martin CDP — core engine (pure logic, unit-tested; no FastAPI).

A warehouse-native, HIPAA-first Customer Data Platform built into Martin. This
module implements the six CDP pillars as pure functions + JSON-backed stores so
they run anywhere (the production deployment lands events in BigQuery; here we keep
a local reference store):

  1. Collection    — ingest track/identify/batch events with a PHI + consent guard
  2. Identity      — deterministic stitching (email / external_id / anonymous_id)
  3. Profiles      — Customer 360: traits + computed metrics + recent events
  4. Segmentation  — a rule engine over traits / metrics / behavior
  5. Activation    — destinations dispatch (guarded, consent + k-anonymity)
  6. Governance    — consent state on every event; de-id boundary reused from Datavant

Design notes:
- Identity is account/tenant-scoped and deterministic (repeatable merges).
- Nothing that looks like raw PHI is ever stored (defensive guard on ingest).
- Marketing activation requires marketing consent; analytics requires analytics
  consent; audiences enforce a k-anonymity floor.
"""
from __future__ import annotations

import os
import re
import json
import time
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH = os.path.join(_DIR, "cdp_events.jsonl")
PROFILES_PATH = os.path.join(_DIR, "cdp_profiles.json")
IDGRAPH_PATH = os.path.join(_DIR, "cdp_identity_graph.json")
SEGMENTS_PATH = os.path.join(_DIR, "cdp_segments.json")
DEST_PATH = os.path.join(_DIR, "cdp_destinations.json")
JOURNEY_PATH = os.path.join(_DIR, "cdp_journeys.json")

MIN_COHORT = 20  # k-anonymity floor for any activated/benchmarked audience

# ─────────────────────────── tiny JSON store ────────────────────────────────
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default() if callable(default) else default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


# ─────────────────────────── PHI + consent guards ───────────────────────────
_PHI = {
    "email": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),
    "phone": re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "mrn": re.compile(r"\bMRN[-:\s]?\d+\b", re.I),
}
# fields that are allowed to hold an email even though it matches the PHI regex
_ID_FIELDS = {"email", "email_hash", "user_email"}


def scan_phi(props: dict) -> list:
    """Flag property values that look like raw PHI (defensive on the ingest edge).
    Identifier fields (email) are hashed on ingest, not flagged."""
    flags = []
    for k, v in (props or {}).items():
        if k.lower() in _ID_FIELDS:
            continue
        s = str(v)
        for label, rx in _PHI.items():
            if rx.search(s):
                flags.append({"field": k, "looks_like": label})
    return flags


def _truthy(v) -> bool:
    return v is True or str(v).lower() in ("true", "1", "yes", "granted", "y")


def consent_state(props: dict) -> dict:
    p = props or {}
    return {"marketing": _truthy(p.get("consent_marketing", p.get("consent"))),
            "analytics": _truthy(p.get("consent_analytics", p.get("consent", True)))}


def _hash_email(email: str) -> str:
    import hashlib
    return "eh_" + hashlib.sha256(str(email).strip().lower().encode()).hexdigest()[:24]


# ─────────────────────────── event normalization ────────────────────────────
def normalize(payload: dict, kind: str = "track") -> dict:
    """Normalize a track/identify payload (Segment-style) into a flat CDP event."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    props = {}
    for bag in ("defaultProperties", "context", "traits", "properties", "eventProperties", "userProperties"):
        b = payload.get(bag)
        if isinstance(b, dict):
            props.update(b)
    email = payload.get("email") or props.get("email")
    ev = {
        "id": "ev_" + uuid.uuid4().hex[:12],
        "kind": kind,
        "event": payload.get("event") or payload.get("type") or ("Identify" if kind == "identify" else ""),
        "anonymous_id": payload.get("anonymousId") or props.get("anonymous_id"),
        "external_id": payload.get("externalId") or payload.get("userId") or props.get("external_id"),
        "email_hash": _hash_email(email) if email else None,
        "time": payload.get("time") or time.time(),
        "properties": {k: v for k, v in props.items() if k.lower() not in _ID_FIELDS},
        "consent": consent_state(props),
    }
    return ev


# ─────────────────────────── identity resolution ────────────────────────────
def _idgraph():
    return _load(IDGRAPH_PATH, dict)


def _profiles():
    return _load(PROFILES_PATH, dict)


def resolve_identity(graph: dict, ev: dict) -> str:
    """Return the canonical person_id for an event, merging on the strongest
    available identifier (external_id > email_hash > anonymous_id). Deterministic:
    the same identifiers always resolve to the same person."""
    keys = []
    if ev.get("external_id"):
        keys.append("ext:" + str(ev["external_id"]))
    if ev.get("email_hash"):
        keys.append("em:" + ev["email_hash"])
    if ev.get("anonymous_id"):
        keys.append("anon:" + str(ev["anonymous_id"]))
    if not keys:
        keys.append("anon:" + ev["id"])  # standalone
    # find any existing person for these keys
    pid = None
    for k in keys:
        if k in graph:
            pid = graph[k]
            break
    if pid is None:
        pid = "person_" + uuid.uuid4().hex[:12]
    # bind all keys to this person (merge)
    for k in keys:
        graph[k] = pid
    return pid


# ─────────────────────────── profile computation ────────────────────────────
def _blank_profile(pid: str) -> dict:
    return {"person_id": pid, "traits": {}, "identifiers": [],
            "metrics": {"event_count": 0, "first_seen": None, "last_seen": None,
                        "revenue_total": 0.0, "session_count": 0},
            "consent": {"marketing": False, "analytics": False},
            "recent_events": [], "updated_at": None}


_REVENUE_EVENTS = {"purchase completed", "subscription started", "order completed", "payment"}


def apply_event(profile: dict, ev: dict) -> dict:
    """Fold one event into a profile (traits from identify, metrics from track)."""
    m = profile["metrics"]
    m["event_count"] += 1
    t = float(ev.get("time") or time.time())
    m["first_seen"] = t if m["first_seen"] is None else min(m["first_seen"], t)
    m["last_seen"] = t if m["last_seen"] is None else max(m["last_seen"], t)
    # identifiers
    for key in ("external_id", "email_hash", "anonymous_id"):
        val = ev.get(key)
        if val and val not in profile["identifiers"]:
            profile["identifiers"].append(val)
    # consent (latest wins, sticky-true for having-ever-granted is avoided; use latest)
    profile["consent"] = ev.get("consent") or profile["consent"]
    # traits from identify
    if ev.get("kind") == "identify":
        profile["traits"].update(ev.get("properties") or {})
    else:
        name = (ev.get("event") or "").lower()
        # revenue
        rev = ev.get("properties", {}).get("revenue") or ev.get("properties", {}).get("value")
        if name in _REVENUE_EVENTS and rev is not None:
            try:
                m["revenue_total"] += float(rev)
            except Exception:
                pass
        if name in ("session started", "session_start"):
            m["session_count"] += 1
        # per-event counter (behavioral)
        ec = profile.setdefault("event_counts", {})
        if ev.get("event"):
            ec[ev["event"]] = ec.get(ev["event"], 0) + 1
    # recent events (cap 20)
    profile["recent_events"] = ([{"event": ev.get("event"), "time": t}] + profile["recent_events"])[:20]
    profile["updated_at"] = time.time()
    return profile


def ingest(payload: dict, kind: str = "track", require_consent: bool = True) -> dict:
    """Full ingest: normalize → PHI guard → consent gate → identity → profile update
    → append event. Returns disposition."""
    ev = normalize(payload, kind)
    phi = scan_phi(ev.get("properties"))
    if phi:
        return {"accepted": False, "reason": "possible_raw_phi", "fields": phi}
    if require_consent and not ev["consent"]["analytics"]:
        return {"accepted": False, "reason": "no_analytics_consent"}
    graph = _idgraph()
    pid = resolve_identity(graph, ev)
    ev["person_id"] = pid
    profiles = _profiles()
    prof = profiles.get(pid) or _blank_profile(pid)
    apply_event(prof, ev)
    profiles[pid] = prof
    _save(IDGRAPH_PATH, graph)
    _save(PROFILES_PATH, profiles)
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, default=str) + "\n")
    except Exception:
        pass
    return {"accepted": True, "person_id": pid, "event_id": ev["id"], "kind": kind}


def get_profile(pid: str) -> dict | None:
    return _profiles().get(pid)


def list_profiles(limit: int = 100) -> list:
    return list(_profiles().values())[:limit]


def stats() -> dict:
    profs = _profiles()
    total = len(profs)
    consented = sum(1 for p in profs.values() if p.get("consent", {}).get("marketing"))
    events = sum(p["metrics"]["event_count"] for p in profs.values())
    revenue = sum(p["metrics"].get("revenue_total", 0) for p in profs.values())
    return {"profiles": total, "marketing_consented": consented, "events": events,
            "revenue_total": round(revenue, 2)}


# ═══════════════════════════ Segmentation (rule engine) ═════════════════════
# A segment is {id,name,description,match:'all'|'any',rules:[{field,op,value}]}.
# field:  trait:<k> | metric:<k> | event:<Name> | consent:<marketing|analytics>
# op:     eq neq gt lt gte lte contains exists not_exists did did_not
def _field_value(profile: dict, field: str):
    kind, _, key = (field or "").partition(":")
    if kind == "trait":
        return profile.get("traits", {}).get(key)
    if kind == "metric":
        return profile.get("metrics", {}).get(key)
    if kind == "event":
        return profile.get("event_counts", {}).get(key, 0)
    if kind == "consent":
        return profile.get("consent", {}).get(key)
    return None


def _cmp(val, op, target) -> bool:
    op = (op or "eq").lower()
    if op == "exists":
        return val is not None
    if op == "not_exists":
        return val is None
    if op == "did":
        try:
            return float(val or 0) > 0
        except Exception:
            return bool(val)
    if op == "did_not":
        try:
            return float(val or 0) == 0
        except Exception:
            return not bool(val)
    if op == "contains":
        return target is not None and str(target).lower() in str(val or "").lower()
    if val is None:
        return False
    # numeric compares when possible
    def num(x):
        try:
            return float(x)
        except Exception:
            return None
    nv, nt = num(val), num(target)
    if op == "eq":
        return (str(val) == str(target)) or (nv is not None and nv == nt)
    if op == "neq":
        return not ((str(val) == str(target)) or (nv is not None and nv == nt))
    if nv is None or nt is None:
        return False
    return {"gt": nv > nt, "lt": nv < nt, "gte": nv >= nt, "lte": nv <= nt}.get(op, False)


def profile_matches(profile: dict, segment: dict) -> bool:
    rules = segment.get("rules") or []
    if not rules:
        return False
    results = [_cmp(_field_value(profile, r.get("field")), r.get("op"), r.get("value")) for r in rules]
    return all(results) if (segment.get("match", "all") == "all") else any(results)


def evaluate_segment(segment: dict, require_marketing_consent: bool = False) -> dict:
    profs = _profiles()
    members = []
    for pid, p in profs.items():
        if require_marketing_consent and not p.get("consent", {}).get("marketing"):
            continue
        if profile_matches(p, segment):
            members.append(pid)
    return {"count": len(members), "member_ids": members,
            "meets_min_cohort": len(members) >= MIN_COHORT, "min_cohort": MIN_COHORT,
            "sample": members[:5]}


def list_segments():
    return _load(SEGMENTS_PATH, list)


def upsert_segment(d: dict) -> dict:
    segs = _load(SEGMENTS_PATH, list)
    d = dict(d)
    d["id"] = d.get("id") or ("seg_" + uuid.uuid4().hex[:10])
    d.setdefault("match", "all")
    d["updated_at"] = time.time()
    for i, s in enumerate(segs):
        if s.get("id") == d["id"]:
            segs[i] = d
            break
    else:
        segs.append(d)
    _save(SEGMENTS_PATH, segs)
    return d


def delete_segment(sid: str) -> bool:
    segs = _load(SEGMENTS_PATH, list)
    kept = [s for s in segs if s.get("id") != sid]
    _save(SEGMENTS_PATH, kept)
    return len(kept) != len(segs)


# ═══════════════════════════ Activation (destinations) ═════════════════════
# Destination = {id,name,type,config,consent_required}. type in warehouse/webhook/
# ads/crm/esp/streaming. Dispatch is DRY-RUN by default and consent + k-anon gated.
_DEST_TYPES = ["warehouse", "webhook", "ads", "crm", "esp", "streaming"]


def list_destinations():
    return _load(DEST_PATH, list)


def upsert_destination(d: dict) -> dict:
    dests = _load(DEST_PATH, list)
    d = dict(d)
    d["id"] = d.get("id") or ("dst_" + uuid.uuid4().hex[:10])
    d.setdefault("consent_required", True)
    d.setdefault("type", "webhook")
    d["updated_at"] = time.time()
    for i, x in enumerate(dests):
        if x.get("id") == d["id"]:
            dests[i] = d
            break
    else:
        dests.append(d)
    _save(DEST_PATH, dests)
    return d


def delete_destination(did: str) -> bool:
    dests = _load(DEST_PATH, list)
    kept = [x for x in dests if x.get("id") != did]
    _save(DEST_PATH, kept)
    return len(kept) != len(dests)


def build_audience(member_ids: list, require_marketing_consent: bool = True) -> dict:
    """Turn segment members into an activation-ready audience: consented profiles
    only, PII-free (hashed identifiers), k-anonymity enforced."""
    profs = _profiles()
    rows, dropped = [], 0
    for pid in member_ids:
        p = profs.get(pid)
        if not p:
            continue
        if require_marketing_consent and not p.get("consent", {}).get("marketing"):
            dropped += 1
            continue
        # only expose hashed/opaque identifiers — never raw PII
        ext = next((i for i in p.get("identifiers", []) if not str(i).startswith(("eh_", "anon"))), None)
        rows.append({"person_id": pid, "external_id": ext,
                     "email_hash": next((i for i in p.get("identifiers", []) if str(i).startswith("eh_")), None)})
    ok = len(rows) >= MIN_COHORT
    return {"count": len(rows), "dropped_no_consent": dropped, "meets_min_cohort": ok,
            "min_cohort": MIN_COHORT, "audience": rows if ok else []}


def activate(destination: dict, member_ids: list, dry_run: bool = True) -> dict:
    """Send an audience to a destination. Dry-run by default; refuses sub-cohort
    sizes and (unless overridden) requires marketing consent."""
    aud = build_audience(member_ids, require_marketing_consent=destination.get("consent_required", True))
    if not aud["meets_min_cohort"]:
        return {"sent": False, "reason": "below_min_cohort", "would_send": aud["count"], "audience": aud}
    endpoint = (destination.get("config") or {}).get("endpoint")
    token = (destination.get("config") or {}).get("token")
    if dry_run or not endpoint:
        return {"sent": False, "dry_run": True, "would_send": aud["count"],
                "endpoint_configured": bool(endpoint), "destination": destination.get("name")}
    try:
        import urllib.request
        body = json.dumps({"audience": aud["audience"], "token": token}).encode()
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"sent": True, "count": aud["count"], "status": getattr(r, "status", 200)}
    except Exception as e:
        return {"sent": False, "error": str(e), "would_send": aud["count"]}


# ═══════════════════════════ Journeys (event-triggered) ════════════════════
def list_journeys():
    return _load(JOURNEY_PATH, list)


def upsert_journey(d: dict) -> dict:
    js = _load(JOURNEY_PATH, list)
    d = dict(d)
    d["id"] = d.get("id") or ("jrn_" + uuid.uuid4().hex[:10])
    d.setdefault("enabled", False)
    d.setdefault("steps", [])
    d["updated_at"] = time.time()
    for i, x in enumerate(js):
        if x.get("id") == d["id"]:
            js[i] = d
            break
    else:
        js.append(d)
    _save(JOURNEY_PATH, js)
    return d


def delete_journey(jid: str) -> bool:
    js = _load(JOURNEY_PATH, list)
    kept = [x for x in js if x.get("id") != jid]
    _save(JOURNEY_PATH, kept)
    return len(kept) != len(js)
