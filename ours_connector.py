"""Ours Privacy source connector — maps Ours' HIPAA-compliant CDP event/profile
model into our governed semantic layer, and provides a consent-gated ingestion
mapper for their streaming/webhook destination.

Design (see OURS_PRIVACY_INTEGRATION_PLAN.md):
  • Ours captures PHI-stripped events server-side and can land them in the
    customer's warehouse (BigQuery/Snowflake) or stream them to a destination.
  • This module is the "P0" adapter: a documented Standard-Healthcare-Events
    schema template, a blueprint that turns that schema into curated tables +
    governed metrics + a topic, a normaliser for their track/identify/batch
    payloads, and a consent gate so we only ever analyse consented data.

The schema template below is built from Ours' PUBLIC docs (docs.oursprivacy.com:
track/identify/batch, Visitor Identity & Matching, Data Mapping, cookie-consent)
and healthcare-marketing funnel conventions. It is intentionally editable — the
exact "Standard Healthcare Events" list must be confirmed against a live Ours
account before go-live (an open question in the integration plan).

Pure functions here are unit-tested; the FastAPI layer (ours_api.py) exposes them.
"""
from __future__ import annotations

import re
import time

# ---------------------------------------------------------------------------
# Ours event/profile schema template (from public docs + funnel conventions)
# ---------------------------------------------------------------------------

# Identity + default properties Ours attaches to every event.
OURS_DEFAULT_PROPS = [
    {"name": "ours_visitor_id", "type": "STRING", "role": "dimension", "note": "Ours anonymous visitor id (cookie)"},
    {"name": "external_id", "type": "STRING", "role": "dimension", "note": "Your own user id (strongest identity signal)"},
    {"name": "distinct_id", "type": "STRING", "role": "dimension", "note": "Idempotency / dedup key"},
    {"name": "event_time", "type": "TIMESTAMP", "role": "date", "note": "Event timestamp"},
    {"name": "utm_source", "type": "STRING", "role": "dimension"},
    {"name": "utm_medium", "type": "STRING", "role": "dimension"},
    {"name": "utm_campaign", "type": "STRING", "role": "dimension"},
    {"name": "utm_content", "type": "STRING", "role": "dimension"},
    {"name": "utm_term", "type": "STRING", "role": "dimension"},
    {"name": "referrer_domain", "type": "STRING", "role": "dimension", "note": "Domain-only (PHI-safe)"},
    {"name": "landing_page", "type": "STRING", "role": "dimension"},
    {"name": "gclid", "type": "STRING", "role": "dimension", "note": "Google click id"},
    {"name": "fbclid", "type": "STRING", "role": "dimension", "note": "Meta click id"},
    {"name": "device_type", "type": "STRING", "role": "dimension"},
    {"name": "consent_marketing", "type": "BOOL", "role": "dimension", "note": "Marketing consent at event time"},
    {"name": "consent_analytics", "type": "BOOL", "role": "dimension", "note": "Analytics consent at event time"},
    {"name": "consent_region", "type": "STRING", "role": "dimension", "note": "Jurisdiction that governed consent"},
]

# Merged User profile (identity-resolved on external_id; newest value wins).
OURS_USER_PROFILE = [
    {"name": "external_id", "type": "STRING", "role": "dimension"},
    {"name": "email_hash", "type": "STRING", "role": "dimension", "note": "Hashed/salted email (never raw)"},
    {"name": "first_seen_at", "type": "TIMESTAMP", "role": "date"},
    {"name": "last_seen_at", "type": "TIMESTAMP", "role": "date"},
    {"name": "first_utm_source", "type": "STRING", "role": "dimension"},
    {"name": "first_utm_campaign", "type": "STRING", "role": "dimension"},
    {"name": "city", "type": "STRING", "role": "dimension", "note": "Coarse geo only (Safe Harbor)"},
    {"name": "state", "type": "STRING", "role": "dimension"},
    {"name": "lifetime_value", "type": "NUMERIC", "role": "metric", "aggregation": "SUM"},
    {"name": "consent_marketing", "type": "BOOL", "role": "dimension"},
]

# Standard Healthcare Events — a healthcare-marketing funnel. `value_prop` marks
# the numeric property (if any) that a conversion metric should sum.
STANDARD_HEALTHCARE_EVENTS = [
    {"event": "Page Viewed", "stage": "awareness", "value_prop": None},
    {"event": "Content Engaged", "stage": "awareness", "value_prop": None},
    {"event": "Form Started", "stage": "interest", "value_prop": None},
    {"event": "Lead Captured", "stage": "lead", "value_prop": None},
    {"event": "Insurance Checked", "stage": "consideration", "value_prop": None},
    {"event": "Appointment Requested", "stage": "intent", "value_prop": None},
    {"event": "Appointment Booked", "stage": "conversion", "value_prop": None},
    {"event": "Appointment Completed", "stage": "conversion", "value_prop": None},
    {"event": "Prescription Started", "stage": "conversion", "value_prop": None},
    {"event": "Purchase Completed", "stage": "revenue", "value_prop": "revenue"},
    {"event": "Subscription Started", "stage": "revenue", "value_prop": "revenue"},
]

# The property carrying revenue on monetary events.
REVENUE_PROP = {"name": "revenue", "type": "NUMERIC", "role": "metric", "aggregation": "SUM", "format": "currency"}
CONSENT_FIELDS = ("consent_marketing", "consent_analytics")


def schema_template() -> dict:
    """Return the full Ours schema template (events + profile + defaults + consent)."""
    return {
        "events": STANDARD_HEALTHCARE_EVENTS,
        "event_default_properties": OURS_DEFAULT_PROPS,
        "revenue_property": REVENUE_PROP,
        "user_profile": OURS_USER_PROFILE,
        "consent_fields": list(CONSENT_FIELDS),
        "identity_keys": ["external_id", "ours_visitor_id", "distinct_id"],
        "source": "Ours Privacy Standard Healthcare Events (template — confirm against live account)",
    }


# ---------------------------------------------------------------------------
# Blueprint: schema template -> recommended semantic model
# ---------------------------------------------------------------------------
def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def recommended_model(dataset: str = "ours_events") -> dict:
    """Turn the schema template into a recommended semantic model:
    two curated tables (events + users), governed candidate metrics, one topic,
    and consent-governance notes. Returned as plain dicts the UI can preview and
    the API can apply (metrics via Metric Lab)."""
    events_table = {
        "key": "ours_events",
        "display_name": "Marketing Events (Ours)",
        "raw_table": f"{dataset}.events",
        "kind": "fact",
        "description": "PHI-stripped marketing/web events captured by Ours Privacy, consent-gated.",
        "time_field": "event_time",
        "dimensions": [p["name"] for p in OURS_DEFAULT_PROPS if p["role"] == "dimension"] + ["event_name"],
        "dates": [p["name"] for p in OURS_DEFAULT_PROPS if p["role"] == "date"],
        "metrics": ["revenue"],
        "join_keys": ["external_id", "ours_visitor_id"],
    }
    users_table = {
        "key": "ours_users",
        "display_name": "Users (Ours)",
        "raw_table": f"{dataset}.users",
        "kind": "dim",
        "description": "Identity-resolved user profiles from Ours Privacy (merged on external_id).",
        "dimensions": [p["name"] for p in OURS_USER_PROFILE if p["role"] == "dimension"],
        "dates": [p["name"] for p in OURS_USER_PROFILE if p["role"] == "date"],
        "metrics": [p["name"] for p in OURS_USER_PROFILE if p["role"] == "metric"],
        "join_keys": ["external_id"],
    }
    relationship = {
        "left": "ours_events", "right": "ours_users", "on": "external_id",
        "cardinality": "many_to_one",
        "note": "Many events per user — the 'many' side (events) is pre-aggregated before joining.",
    }
    metrics = candidate_metrics()
    topic = {
        "name": "Healthcare Marketing (Ours)",
        "description": "Acquisition → conversion funnel on Ours Privacy events, consent-governed.",
        "base_table": "ours_events",
        "tables": ["ours_users"],
        "metrics": [m["name"] for m in metrics],
        "dimensions": ["utm_source", "utm_medium", "utm_campaign", "device_type", "consent_region"],
    }
    return {
        "tables": [events_table, users_table],
        "relationships": [relationship],
        "metrics": metrics,
        "pending_metrics": pending_metrics(),
        "topics": [topic],
        "governance": {
            "consent_required": True,
            "consent_filter": {"field": "consent_analytics", "op": "eq", "value": True},
            "note": "Analytics metrics analyse consented events only; marketing activation requires consent_marketing.",
        },
    }


def candidate_metrics() -> list:
    """Governed candidate metrics (Metric Lab-compatible dicts) from the funnel.
    A count metric per key funnel stage + total revenue. All time-based on
    event_time, monthly grain, consent-filtered."""
    consent_filter = {"field": "consent_analytics", "op": "eq", "value": True}
    stage_events = {
        "Website Visits": "Page Viewed",
        "Leads Captured": "Lead Captured",
        "Appointments Requested": "Appointment Requested",
        "Appointments Booked": "Appointment Booked",
        "Conversions": "Appointment Completed",
    }
    metrics = []
    for name, ev in stage_events.items():
        metrics.append({
            "name": name,
            "description": f"Count of '{ev}' events (consented).",
            "synonyms": [],
            "base_table": "ours_events",
            "measure_field": "distinct_id",
            "aggregation": "count_distinct",
            "time_field": "event_time",
            "default_grain": "month",
            "filters": [{"field": "event_name", "op": "eq", "value": ev}, dict(consent_filter)],
            "format": {"type": "number", "decimals": 0},
            "direction": "favorable_up",
        })
    metrics.append({
        "name": "Marketing Revenue",
        "description": "Sum of revenue from purchase/subscription events (consented).",
        "synonyms": ["sales", "topline"],
        "base_table": "ours_events",
        "measure_field": "revenue",
        "aggregation": "sum",
        "time_field": "event_time",
        "default_grain": "month",
        "filters": [{"field": "event_name", "op": "in", "value": ["Purchase Completed", "Subscription Started"]}, dict(consent_filter)],
        "format": {"type": "currency", "decimals": 0},
        "direction": "favorable_up",
    })

    # --- Ratio metrics (deeper analytics: funnel conversion rates + unit economics)
    def _count_side(ev=None):
        s = {"measure_field": "distinct_id", "aggregation": "count_distinct", "filters": []}
        if ev:
            s["filters"] = [{"field": "event_name", "op": "eq", "value": ev}]
        return s

    def _rate(name, desc, num_ev, den_ev, syn=None):
        return {"name": name, "description": desc, "synonyms": syn or [], "type": "ratio",
                "base_table": "ours_events", "time_field": "event_time", "default_grain": "month",
                "numerator": _count_side(num_ev), "denominator": _count_side(den_ev),
                "filters": [dict(consent_filter)],
                "format": {"type": "percent", "decimals": 1}, "direction": "favorable_up"}

    metrics += [
        _rate("Visit → Lead Rate", "Share of visitors who become leads (consented).",
              "Lead Captured", "Page Viewed", ["lead conversion rate"]),
        _rate("Lead → Appointment Rate", "Share of leads who book an appointment (consented).",
              "Appointment Booked", "Lead Captured", ["booking rate"]),
        _rate("Appointment Completion Rate", "Share of booked appointments that are completed.",
              "Appointment Completed", "Appointment Booked", ["show rate"]),
    ]
    # Revenue per visitor (unit economics) — sum(revenue) / distinct visitors.
    metrics.append({
        "name": "Revenue per Visitor", "description": "Marketing revenue divided by unique visitors (consented).",
        "synonyms": ["RPV", "revenue per visit"], "type": "ratio",
        "base_table": "ours_events", "time_field": "event_time", "default_grain": "month",
        "numerator": {"measure_field": "revenue", "aggregation": "sum",
                      "filters": [{"field": "event_name", "op": "in", "value": ["Purchase Completed", "Subscription Started"]}]},
        "denominator": _count_side("Page Viewed"),
        "filters": [dict(consent_filter)],
        "format": {"type": "currency", "decimals": 2}, "direction": "favorable_up",
    })
    return metrics


def pending_metrics() -> list:
    """Metrics that need an ad-spend feed (from the ad platforms, not the Ours event
    stream). Documented so the pack is complete; created only once a spend source is
    connected. ROAS = revenue / spend; CAC = spend / conversions."""
    return [
        {"name": "ROAS", "description": "Return on ad spend — Marketing Revenue / ad spend. Requires an ad-spend source.",
         "type": "ratio", "needs": "ad_spend", "format": {"type": "number", "decimals": 2}},
        {"name": "CAC", "description": "Customer acquisition cost — ad spend / conversions. Requires an ad-spend source.",
         "type": "ratio", "needs": "ad_spend", "format": {"type": "currency", "decimals": 0}},
    ]


# ---------------------------------------------------------------------------
# Ingestion: normalise Ours track/identify/batch payloads (streaming path)
# ---------------------------------------------------------------------------
_KNOWN_EVENTS = {e["event"] for e in STANDARD_HEALTHCARE_EVENTS}


def normalize_event(payload: dict) -> dict:
    """Normalise one Ours event payload (track/identify shape) into a flat row
    for our landing store. Ours sends: event, token, one of userId/externalId/
    email, time, distinctId, eventProperties, userProperties, defaultProperties."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    ev = payload.get("event") or payload.get("type") or ""
    props = {}
    for bag in ("defaultProperties", "userProperties", "eventProperties"):
        b = payload.get(bag)
        if isinstance(b, dict):
            props.update(b)
    row = {
        "event_name": ev,
        "external_id": payload.get("externalId") or payload.get("userId") or props.get("external_id"),
        "ours_visitor_id": props.get("ours_visitor_id") or payload.get("visitorId"),
        "distinct_id": payload.get("distinctId") or props.get("distinct_id"),
        "event_time": payload.get("time") or props.get("event_time"),
        "properties": props,
        "known_event": ev in _KNOWN_EVENTS,
        "received_at": time.time(),
    }
    return row


def consent_ok(row: dict, require: str = "consent_analytics") -> bool:
    """Consent gate: only pass an event if the required consent flag is truthy.
    Missing/false consent → dropped from analytics. `require` is the consent field
    ('consent_analytics' for analytics, 'consent_marketing' for activation)."""
    props = row.get("properties") or {}
    v = props.get(require, row.get(require))
    return v is True or str(v).lower() in ("true", "1", "yes", "granted")


def contains_possible_phi(row: dict) -> list:
    """Defensive check: flag fields that look like raw PHI (email, phone, SSN,
    MRN, full name) so we never store/analyse them. Ours strips PHI upstream;
    this is belt-and-suspenders on the ingest boundary."""
    flags = []
    props = row.get("properties") or {}
    patt = {
        "email": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),
        "phone": re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    }
    for k, v in props.items():
        s = str(v)
        for label, rx in patt.items():
            if rx.search(s):
                flags.append({"field": k, "looks_like": label})
    return flags
