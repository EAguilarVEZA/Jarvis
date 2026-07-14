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
    sink_to_warehouse(ev)  # best-effort BQ stream when JARVIS_CDP_WAREHOUSE=1
    try:
        journey_on_event(ev)  # fire event-triggered journeys
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


# ═══════════════════════════ Journey runtime ═══════════════════════════════
# A journey = {trigger_event, enabled, steps:[...]}. Steps:
#   {"type":"wait","seconds":N}
#   {"type":"condition","field","op","value"}         (exits journey if false)
#   {"type":"action","action":"tag|activate|webhook|workflow", ...}
# Enrollment happens on a matching ingested event; waits pause the enrollment until
# tick_journeys() resumes it. Side-effectful actions are guarded (dry-run/logged).
JRUNS_PATH = os.path.join(_DIR, "cdp_journey_runs.json")


def _eval_condition(profile: dict, cond: dict) -> bool:
    return _cmp(_field_value(profile or {}, cond.get("field")), cond.get("op"), cond.get("value"))


def _do_action(step: dict, profile: dict, enr: dict) -> dict:
    a = (step.get("action") or "noop").lower()
    if a == "tag":
        # write a trait onto our own profile store (safe, first-party)
        k, v = step.get("key"), step.get("value")
        profs = _profiles(); p = profs.get(enr["person_id"])
        if p is not None and k:
            p.setdefault("traits", {})[k] = v
            _save(PROFILES_PATH, profs)
        return {"action": "tag", "key": k, "value": v, "applied": bool(p)}
    if a in ("activate", "webhook", "workflow", "send"):
        # side-effectful → dry-run/logged; a live run requires explicit configuration
        return {"action": a, "status": "dry_run", "target": step.get("target"),
                "note": "guarded — configure + opt-in to send live"}
    return {"action": a, "status": "noop"}


def _advance_enrollment(enr: dict, journeys: dict, profiles: dict):
    j = journeys.get(enr["journey_id"])
    if not j:
        enr["status"] = "orphaned"; return
    steps = j.get("steps") or []
    while enr["step_index"] < len(steps):
        step = steps[enr["step_index"]]
        typ = (step.get("type") or "").lower()
        if typ == "wait":
            enr["next_at"] = time.time() + int(step.get("seconds", 0))
            enr["status"] = "waiting"; enr["step_index"] += 1
            enr["log"].append({"step": enr["step_index"] - 1, "wait": step.get("seconds", 0)})
            return
        if typ == "condition":
            if not _eval_condition(profiles.get(enr["person_id"]) or {}, step):
                enr["status"] = "exited"
                enr["log"].append({"step": enr["step_index"], "exit": "condition_false"})
                return
            enr["log"].append({"step": enr["step_index"], "condition": "passed"})
        elif typ == "action":
            res = _do_action(step, profiles.get(enr["person_id"]) or {}, enr)
            enr["log"].append({"step": enr["step_index"], "action": res})
        enr["step_index"] += 1
    enr["status"] = "completed"


def journey_on_event(ev: dict):
    """Enroll a person into any enabled journey whose trigger matches this event."""
    trig = ev.get("event")
    matches = {j["id"]: j for j in list_journeys() if j.get("enabled") and j.get("trigger_event") == trig}
    if not matches or not ev.get("person_id"):
        return
    runs = _load(JRUNS_PATH, list)
    profiles = _profiles()
    for jid, j in matches.items():
        if any(r for r in runs if r["journey_id"] == jid and r["person_id"] == ev["person_id"]
               and r["status"] in ("active", "waiting")):
            continue  # already enrolled
        enr = {"id": "enr_" + uuid.uuid4().hex[:10], "journey_id": jid, "person_id": ev["person_id"],
               "status": "active", "step_index": 0, "next_at": None, "log": [], "created_at": time.time()}
        _advance_enrollment(enr, {jid: j}, profiles)
        runs.append(enr)
    _save(JRUNS_PATH, runs)


def tick_journeys() -> dict:
    """Resume enrollments whose wait has elapsed. A scheduled job calls this."""
    runs = _load(JRUNS_PATH, list)
    journeys = {j["id"]: j for j in list_journeys()}
    profiles = _profiles()
    now = time.time(); resumed = 0
    for enr in runs:
        if enr.get("status") == "waiting" and enr.get("next_at") and enr["next_at"] <= now:
            enr["status"] = "active"
            _advance_enrollment(enr, journeys, profiles)
            resumed += 1
    _save(JRUNS_PATH, runs)
    return {"resumed": resumed,
            "active": sum(1 for r in runs if r["status"] in ("active", "waiting")),
            "completed": sum(1 for r in runs if r["status"] == "completed"),
            "total": len(runs)}


def list_enrollments(journey_id: str = None, limit: int = 100) -> list:
    runs = _load(JRUNS_PATH, list)
    if journey_id:
        runs = [r for r in runs if r["journey_id"] == journey_id]
    return runs[-limit:]


# ═══════════════════════════ Warehouse-native (BigQuery) ════════════════════
# The CDP is designed to run ON the customer's warehouse. Events stream to a BQ
# table; Customer 360 profiles and segments are then computed IN the warehouse via
# governed SQL (so PHI never leaves it). Local JSON is the dev/default backend.
EVENTS_TABLE = os.getenv("JARVIS_CDP_EVENTS_TABLE", "martin_cdp.events")
_REVENUE_SQL_EVENTS = "('purchase completed','subscription started','order completed')"


def warehouse_enabled() -> bool:
    return str(os.getenv("JARVIS_CDP_WAREHOUSE", "")).lower() in ("1", "true", "yes")


def sink_to_warehouse(ev: dict) -> bool:
    """Stream one event to BigQuery when enabled. Best-effort; the JSONL landing
    store is always written by ingest() as the source of truth for dev + audit."""
    if not warehouse_enabled():
        return False
    try:
        from semantic.executor import _client as _bq_client  # reuse the configured BQ client
        client = _bq_client()
        proj_ds, _, tbl = EVENTS_TABLE.rpartition(".")
        row = {"person_id": ev.get("person_id"), "event": ev.get("event"), "kind": ev.get("kind"),
               "ts": ev.get("time"), "external_id": ev.get("external_id"), "email_hash": ev.get("email_hash"),
               "anonymous_id": ev.get("anonymous_id"),
               "revenue": (ev.get("properties") or {}).get("revenue") or (ev.get("properties") or {}).get("value"),
               "consent_marketing": (ev.get("consent") or {}).get("marketing"),
               "consent_analytics": (ev.get("consent") or {}).get("analytics"),
               "properties": json.dumps(ev.get("properties") or {})}
        client.insert_rows_json(EVENTS_TABLE.replace(".", ".", 1), [row])
        return True
    except Exception:
        return False


def _profiles_sql(events_table: str = None) -> str:
    """Governed SQL that builds Customer 360 profiles from the events table —
    identity already resolved to person_id at ingest, then aggregated in-warehouse."""
    t = events_table or EVENTS_TABLE
    return (
        "SELECT\n"
        "  person_id,\n"
        "  COUNT(*) AS event_count,\n"
        "  MIN(ts) AS first_seen,\n"
        "  MAX(ts) AS last_seen,\n"
        f"  SUM(IF(LOWER(event) IN {_REVENUE_SQL_EVENTS}, IFNULL(revenue,0), 0)) AS revenue_total,\n"
        "  COUNTIF(LOWER(event) = 'session started') AS session_count,\n"
        "  MAX(consent_marketing) AS consent_marketing,\n"
        "  MAX(consent_analytics) AS consent_analytics,\n"
        "  ANY_VALUE(external_id) AS external_id,\n"
        "  ANY_VALUE(email_hash) AS email_hash\n"
        f"FROM `{t}`\n"
        "GROUP BY person_id"
    )


_WH_OPS = {"eq": "=", "neq": "!=", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
_WH_COLS = {"event_count", "revenue_total", "session_count", "first_seen", "last_seen",
            "consent_marketing", "consent_analytics"}


def _wh_col(field: str):
    kind, _, key = (field or "").partition(":")
    if kind == "metric" and key in _WH_COLS:
        return key
    if kind == "consent" and key in ("marketing", "analytics"):
        return "consent_" + key
    return None  # trait:/event: need extra modeling; skipped in the warehouse view


def _wh_lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.match(r"^-?\d+(\.\d+)?$", s):
        return s
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _safe_ident(name: str) -> str:
    return "ev_" + (re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "x")


def _json_path(key: str) -> str:
    return "$." + re.sub(r"[^A-Za-z0-9_]", "", str(key))


def _profiles_cte(events_table: str, event_names: list) -> str:
    """Full warehouse profile view: base metrics + latest identify traits +
    a per-event count column for each `event:` referenced by a segment."""
    t = events_table or EVENTS_TABLE
    evcols = "".join(f",\n    COUNTIF(e.event = {_wh_lit(n)}) AS {_safe_ident(n)}" for n in event_names)
    return (
        "WITH ev AS (SELECT * FROM `" + t + "`),\n"
        "latest_traits AS (\n"
        "  SELECT person_id, properties AS traits FROM (\n"
        "    SELECT person_id, properties, ROW_NUMBER() OVER (PARTITION BY person_id ORDER BY ts DESC) AS rn\n"
        "    FROM ev WHERE kind = 'identify'\n"
        "  ) WHERE rn = 1\n"
        "),\n"
        "profiles AS (\n"
        "  SELECT\n"
        "    e.person_id,\n"
        "    COUNT(*) AS event_count,\n"
        "    MIN(e.ts) AS first_seen,\n"
        "    MAX(e.ts) AS last_seen,\n"
        f"    SUM(IF(LOWER(e.event) IN {_REVENUE_SQL_EVENTS}, IFNULL(e.revenue,0), 0)) AS revenue_total,\n"
        "    COUNTIF(LOWER(e.event) = 'session started') AS session_count,\n"
        "    MAX(e.consent_marketing) AS consent_marketing,\n"
        "    MAX(e.consent_analytics) AS consent_analytics,\n"
        "    ANY_VALUE(e.external_id) AS external_id,\n"
        "    ANY_VALUE(lt.traits) AS traits" + evcols + "\n"
        "  FROM ev e LEFT JOIN latest_traits lt USING (person_id)\n"
        "  GROUP BY e.person_id\n"
        ")"
    )


def segment_sql(rules: list, match: str = "all", events_table: str = None) -> dict:
    """Compile a segment into governed BigQuery SQL that selects member person_ids
    from the in-warehouse profile view. Supports metric:, consent:, event: (per-event
    counts) and trait: (latest identify, via JSON_VALUE). Returns {sql, unsupported_rules}."""
    preds, unsupported, event_names = [], [], []
    seen = set()
    for r in (rules or []):
        k, _, key = (r.get("field") or "").partition(":")
        if k == "event" and key not in seen:
            seen.add(key); event_names.append(key)
    for r in (rules or []):
        field = r.get("field") or ""
        op = (r.get("op") or "eq").lower()
        val = r.get("value")
        kind, _, key = field.partition(":")
        if kind == "trait":
            jv = f"JSON_VALUE(traits, {_wh_lit(_json_path(key))})"
            if op == "exists":
                preds.append(f"{jv} IS NOT NULL")
            elif op == "not_exists":
                preds.append(f"{jv} IS NULL")
            elif op == "contains":
                preds.append(f"{jv} LIKE {_wh_lit('%' + str(val) + '%')}")
            elif op in ("gt", "lt", "gte", "lte"):
                preds.append(f"SAFE_CAST({jv} AS FLOAT64) {_WH_OPS[op]} {_wh_lit(val)}")
            elif op in ("eq", "neq"):
                preds.append(f"{jv} {_WH_OPS[op]} {_wh_lit(str(val))}")
            else:
                unsupported.append(field)
            continue
        if kind == "metric" and key in _WH_COLS:
            col = key
        elif kind == "consent" and key in ("marketing", "analytics"):
            col = "consent_" + key
        elif kind == "event":
            col = _safe_ident(key)
        else:
            unsupported.append(field)
            continue
        if op == "exists":
            preds.append(f"{col} IS NOT NULL")
        elif op == "not_exists":
            preds.append(f"{col} IS NULL")
        elif op == "contains":
            preds.append(f"CAST({col} AS STRING) LIKE {_wh_lit('%' + str(val) + '%')}")
        elif op in ("did", "did_not"):
            preds.append(f"{col} {'>' if op == 'did' else '='} 0")
        elif op in _WH_OPS:
            preds.append(f"{col} {_WH_OPS[op]} {_wh_lit(val)}")
        else:
            unsupported.append(field)
    joiner = " AND " if match == "all" else " OR "
    where = joiner.join(preds) if preds else "TRUE"
    sql = _profiles_cte(events_table, event_names) + "\nSELECT person_id FROM profiles\nWHERE " + where
    return {"sql": sql, "unsupported_rules": unsupported}


def warehouse_status() -> dict:
    return {"enabled": warehouse_enabled(), "events_table": EVENTS_TABLE,
            "note": "Set JARVIS_CDP_WAREHOUSE=1 and configure BigQuery to stream events + compute "
                    "profiles/segments in-warehouse (PHI never leaves it). trait:/event: rules need an "
                    "identify/events sub-model in the warehouse view."}
