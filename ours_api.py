"""Ours Privacy connector API (prefix /api/ours).

Endpoints:
  GET  /api/ours/schema        — Standard Healthcare Events schema template
  GET  /api/ours/blueprint     — recommended semantic model (tables/metrics/topic)
  POST /api/ours/apply_metrics — create the governed candidate metrics in Metric Lab
  POST /api/ours/ingest        — consent-gated ingestion of Ours event payloads
  GET  /api/ours/status        — connector status (landing counts, metrics applied)

Compliance posture: this endpoint accepts ONLY de-identified data (Ours strips PHI
upstream). The ingest path re-checks for anything that looks like raw PHI and drops
it, applies the consent gate, and lands normalised events locally. See
OURS_PRIVACY_INTEGRATION_PLAN.md.
"""
from __future__ import annotations

import os
import json
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

import ours_connector as _ours

router = APIRouter(prefix="/api/ours", tags=["ours"])

_DIR = os.path.dirname(os.path.abspath(__file__))
_LANDING = os.path.join(_DIR, "ours_landing.jsonl")
_STATE = os.path.join(_DIR, "ours_connector_state.json")


def _load_state() -> dict:
    try:
        with open(_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict):
    tmp = _STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, _STATE)


@router.get("/schema")
async def ours_schema():
    """Ours Standard Healthcare Events schema template (events + profile + consent)."""
    return _ours.schema_template()


@router.get("/blueprint")
async def ours_blueprint(dataset: str = "ours_events"):
    """Recommended semantic model derived from the Ours schema template."""
    return _ours.recommended_model(dataset)


@router.post("/apply_metrics")
async def ours_apply_metrics():
    """Create the governed candidate metrics in the Metric Lab so they're queryable
    and available to the grounded agent. Idempotent-ish: skips a metric whose name
    already exists."""
    try:
        import metric_lab as _mlab
    except Exception as e:
        return {"ok": False, "error": f"metric_lab unavailable: {e}"}
    existing = {(m.get("name") or "").strip().lower() for m in _mlab.list_metrics()}
    created, skipped = [], []
    for m in _ours.candidate_metrics():
        if (m.get("name") or "").strip().lower() in existing:
            skipped.append(m["name"]); continue
        saved = _mlab.upsert_metric(dict(m))
        created.append({"id": saved.get("id"), "name": saved.get("name")})
    st = _load_state(); st["metrics_applied_at"] = time.time()
    st["metrics_created"] = [c["name"] for c in created]
    _save_state(st)
    return {"ok": True, "created": created, "skipped": skipped,
            "note": "Metrics reference ours_events; connect the dataset to run them live."}


class IngestBody(BaseModel):
    event: Optional[str] = None
    type: Optional[str] = None
    token: Optional[str] = None
    userId: Optional[str] = None
    externalId: Optional[str] = None
    visitorId: Optional[str] = None
    distinctId: Optional[str] = None
    time: Optional[object] = None
    eventProperties: Optional[dict] = None
    userProperties: Optional[dict] = None
    defaultProperties: Optional[dict] = None
    batch: Optional[list] = None       # for /batch-style payloads


@router.post("/ingest")
async def ours_ingest(body: IngestBody):
    """Consent-gated ingestion of Ours event payload(s). Accepts one event or a
    `batch`. Drops events lacking analytics consent or containing apparent raw PHI,
    normalises the rest, and lands them locally. Returns per-event disposition."""
    raw = body.batch if body.batch else [body.dict(exclude_none=True)]
    accepted, dropped = [], []
    lines = []
    for p in raw:
        try:
            row = _ours.normalize_event(p)
        except Exception as e:
            dropped.append({"reason": f"malformed: {e}"}); continue
        phi = _ours.contains_possible_phi(row)
        if phi:
            dropped.append({"event": row.get("event_name"), "reason": "possible raw PHI", "fields": phi}); continue
        if not _ours.consent_ok(row, "consent_analytics"):
            dropped.append({"event": row.get("event_name"), "reason": "no analytics consent"}); continue
        accepted.append({"event": row.get("event_name"), "known": row.get("known_event")})
        lines.append(json.dumps(row))
    if lines:
        try:
            with open(_LANDING, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            return {"ok": False, "error": f"landing write failed: {e}", "accepted": 0}
    st = _load_state(); st["landed"] = st.get("landed", 0) + len(accepted)
    st["last_ingest_at"] = time.time(); _save_state(st)
    return {"ok": True, "accepted": len(accepted), "dropped": len(dropped),
            "accepted_detail": accepted, "dropped_detail": dropped}


# ── P3: audience activation loop ──────────────────────────────────────────────
class AudienceBody(BaseModel):
    name: str
    members: list                      # [{external_id, consent_marketing}]
    require_consent: Optional[bool] = True


@router.post("/audience/compose")
async def audience_compose(body: AudienceBody):
    """Compose a consent-gated, k-anonymity-checked audience payload (no send)."""
    import ours_activation as _act
    return _act.compose_audience(body.name, body.members, require_consent=body.require_consent)


class AudiencePushBody(BaseModel):
    name: str
    members: list
    require_consent: Optional[bool] = True
    endpoint: Optional[str] = ""       # Ours ingest URL (from tenant config)
    token: Optional[str] = ""          # Ours source token
    dry_run: Optional[bool] = True     # default dry-run — activation is opt-in


@router.post("/audience/push")
async def audience_push(body: AudiencePushBody):
    """Push a computed audience back into Ours for compliant activation. Dry-run by
    default and whenever no endpoint/token is configured; refuses sub-cohort sizes."""
    import ours_activation as _act
    aud = _act.compose_audience(body.name, body.members, require_consent=body.require_consent)
    res = _act.push_audience(aud, endpoint=body.endpoint or "", token=body.token or "",
                             dry_run=bool(body.dry_run))
    return {"audience": {k: aud[k] for k in ("name", "count", "dropped_no_consent", "meets_min_cohort", "min_cohort")},
            "push": res}


# ── P3: experiment-analysis depth ─────────────────────────────────────────────
class ExperimentBody(BaseModel):
    control: list                      # per-unit metric values in control arm
    treatment: list                    # per-unit metric values in treatment arm
    alpha: Optional[float] = 0.05


@router.post("/experiment_analyze")
async def experiment_analyze(body: ExperimentBody):
    """Deeper readout over an Ours experiment's variant results (Welch t + CI)."""
    import ours_activation as _act
    return _act.ab_readout(body.control, body.treatment, alpha=body.alpha or 0.05)


# ── P3: governed benchmarks (Marketplace tie-in) ──────────────────────────────
@router.get("/benchmarks")
async def ours_benchmarks():
    """De-identified peer-benchmark definitions (k-anonymity guarded)."""
    import ours_activation as _act
    return {"benchmarks": _act.benchmark_definitions(),
            "note": "Published per benchmark only when enough tenants contribute (k-anonymity)."}


@router.get("/status")
async def ours_status():
    """Connector status: landed event count, metrics applied, last ingest time."""
    st = _load_state()
    landed = st.get("landed", 0)
    return {"connected": bool(st.get("metrics_applied_at") or landed),
            "landed_events": landed,
            "metrics_applied_at": st.get("metrics_applied_at"),
            "metrics_created": st.get("metrics_created", []),
            "last_ingest_at": st.get("last_ingest_at"),
            "schema_source": _ours.schema_template()["source"]}
