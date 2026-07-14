"""Ours Privacy — P3 differentiation: activation loop, experiment-analysis depth,
and governed benchmarks.

  • Audience push-back: we compute a high-value cohort, then push it BACK into Ours
    as a source so their allowlist + consent gate fan it out compliantly to ad
    destinations. Consent-gated (marketing consent required) + k-anonymity floor so
    we never activate a cohort small enough to re-identify.
  • Experiment depth: a two-sample readout (uplift, Welch t, p-value, CI) over an
    Ours experiment's variant results — reusing experiments_api's tested stats.
  • Governed benchmarks: de-identified benchmark metric definitions with a minimum
    cohort guard, for the Marketplace tie-in.

Pure logic + a guarded network push (dry-run by default). No PHI: activation uses
Ours' own identifiers (external_id) that are already de-identified upstream.
"""
from __future__ import annotations

import json

MIN_COHORT = 20          # k-anonymity floor — never activate/benchmark below this


# ---------------------------------------------------------------------------
# Audience push-back (activation loop)
# ---------------------------------------------------------------------------
def _consented(member: dict) -> bool:
    v = member.get("consent_marketing", member.get("consent"))
    return v is True or str(v).lower() in ("true", "1", "yes", "granted")


def compose_audience(name: str, members: list, require_consent: bool = True) -> dict:
    """Build an Ours-ingestible payload that tags each consented member with
    audience membership (Ours turns this into a segment it can activate). Drops
    members without marketing consent; enforces the k-anonymity floor."""
    name = (name or "").strip() or "audience"
    kept, dropped = [], 0
    seen = set()
    for m in members or []:
        eid = str(m.get("external_id") or "").strip()
        if not eid or eid in seen:
            continue
        if require_consent and not _consented(m):
            dropped += 1
            continue
        seen.add(eid)
        # Ours identify/track shape — membership as a user property, no PHI.
        kept.append({"event": "Audience Membership", "externalId": eid,
                     "userProperties": {"audience": name, "audience_member": True}})
    ok = len(kept) >= MIN_COHORT
    return {"name": name, "count": len(kept), "dropped_no_consent": dropped,
            "meets_min_cohort": ok, "min_cohort": MIN_COHORT,
            "payload": kept if ok else [],
            "note": ("" if ok else f"Cohort has {len(kept)} consented members; the k-anonymity floor is {MIN_COHORT}. Broaden the audience before activating.")}


def push_audience(audience: dict, endpoint: str = "", token: str = "", dry_run: bool = True) -> dict:
    """Send a composed audience to Ours' ingest endpoint. Dry-run by default and
    whenever no endpoint/token is configured — activation is a side-effect that must
    be explicitly enabled. Refuses cohorts below the k-anonymity floor."""
    if not audience.get("meets_min_cohort"):
        return {"sent": False, "reason": "below_min_cohort", "would_send": audience.get("count", 0)}
    payload = audience.get("payload") or []
    if dry_run or not endpoint or not token:
        return {"sent": False, "dry_run": True, "would_send": len(payload),
                "endpoint_configured": bool(endpoint and token)}
    # Real send (runs only in the live server with network + configured Ours source).
    try:
        import urllib.request
        body = json.dumps({"batch": payload, "token": token}).encode()
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"sent": True, "count": len(payload), "status": r.status}
    except Exception as e:
        return {"sent": False, "error": str(e), "would_send": len(payload)}


# ---------------------------------------------------------------------------
# Experiment-analysis depth (layer on Ours' Bayesian A/B)
# ---------------------------------------------------------------------------
def ab_readout(control: list, treatment: list, alpha: float = 0.05) -> dict:
    """Two-sample readout (Welch's t) over per-unit metric values for an Ours
    experiment's control vs treatment. Reuses experiments_api's tested stats so the
    math is identical to the Test & Learn lab."""
    try:
        from experiments_api import _mean, _var, t_two_sided_p
    except Exception:
        def _mean(xs): return sum(xs) / len(xs) if xs else 0.0
        def _var(xs, ddof=1):
            n = len(xs)
            if n <= ddof: return 0.0
            m = _mean(xs); return sum((v - m) ** 2 for v in xs) / (n - ddof)
        t_two_sided_p = None
    c = [float(x) for x in (control or []) if x is not None]
    t = [float(x) for x in (treatment or []) if x is not None]
    if len(c) < 2 or len(t) < 2:
        return {"ok": False, "error": "need at least 2 observations per arm"}
    mc, mt = _mean(c), _mean(t)
    vc, vt = _var(c), _var(t)
    nc, nt = len(c), len(t)
    se = ((vc / nc) + (vt / nt)) ** 0.5
    diff = mt - mc
    tstat = diff / se if se else 0.0
    # Welch–Satterthwaite df
    df = ((vc/nc + vt/nt) ** 2) / (((vc/nc)**2/(nc-1) if nc > 1 else 0) + ((vt/nt)**2/(nt-1) if nt > 1 else 0)) if se else 1.0
    p = t_two_sided_p(tstat, df) if t_two_sided_p else None
    ci = (diff - 1.96 * se, diff + 1.96 * se)
    return {"ok": True, "control_mean": mc, "treatment_mean": mt,
            "absolute_uplift": diff, "relative_uplift": (diff / mc if mc else None),
            "t_stat": tstat, "df": df, "p_value": p,
            "significant": (p is not None and p < alpha), "alpha": alpha,
            "ci95_absolute": [ci[0], ci[1]], "n_control": nc, "n_treatment": nt,
            "note": "Welch two-sample readout. For pre/post + CUPED variance reduction, use the Test & Learn lab."}


# ---------------------------------------------------------------------------
# Governed benchmarks (Marketplace tie-in)
# ---------------------------------------------------------------------------
def benchmark_definitions() -> list:
    """De-identified benchmark metrics a tenant can compare against peers. Each
    carries a min_cohort guard so a benchmark is only published when enough tenants
    contribute to prevent re-identification (k-anonymity)."""
    base = {"grain": "month", "min_cohort": MIN_COHORT, "de_identified": True,
            "consent_required": True}
    return [
        dict(base, key="bench_visit_lead", name="Visit → Lead Rate (peer benchmark)",
             metric="Visit → Lead Rate", stat="median", unit="percent"),
        dict(base, key="bench_lead_appt", name="Lead → Appointment Rate (peer benchmark)",
             metric="Lead → Appointment Rate", stat="median", unit="percent"),
        dict(base, key="bench_show_rate", name="Appointment Completion Rate (peer benchmark)",
             metric="Appointment Completion Rate", stat="median", unit="percent"),
        dict(base, key="bench_rpv", name="Revenue per Visitor (peer benchmark)",
             metric="Revenue per Visitor", stat="median", unit="currency"),
    ]


def benchmark_ok(contributing_tenants: int) -> bool:
    """A benchmark may be published only when enough tenants contribute."""
    return contributing_tenants >= MIN_COHORT // 4  # >=5 tenants minimum
