"""
data_exchange — Martin Health Data Exchange demo engine (Volumes 8 & 9).

SYNTHETIC DEMO ONLY. Loads the 10,000-row synthetic dataset and provides:
  • inventory()          — dynamic aggregate stats ("what could we commercialize?")
  • cohort()             — feasibility count + safe aggregates (small-cell suppression)
  • tier_eligibility()   — DETERMINISTIC TierEligibilityEngine (T0-T6) — the LLM never invents eligibility
  • estimate_quote()     — demo pricing configurator (versioned rate-card seed, labeled ESTIMATE)

Everything here is generated demo data — ZERO real Orlando Health patient data. Prices are configurable
demo assumptions, not market quotes. Privacy methods, linkage, access environment and delivery are modeled
as orthogonal attributes per HIPAA guidance (Safe Harbor / Expert Determination are the only de-id methods;
tokenization alone is NOT "HIPAA de-identified"; clean rooms are an access environment, not a de-id method).
"""
from __future__ import annotations

import os
import csv
import collections

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_exchange_data")
_CSV = os.path.join(_DIR, "synthetic_demo_10000.csv")
MIN_CELL = 11                       # small-cell suppression threshold
RATE_CARD_VERSION = "demo-2026.09"
LABEL = "SYNTHETIC DEMO DATA — NOT ORLANDO HEALTH PATIENT DATA"

_ROWS: list = []


def _load() -> list:
    global _ROWS
    if _ROWS:
        return _ROWS
    if os.path.exists(_CSV):
        with open(_CSV, newline="", encoding="utf-8") as f:
            _ROWS = list(csv.DictReader(f))
    return _ROWS


# ── tier catalog (Vol 9 §3) — orthogonal privacy/linkage/access attributes ──
TIERS = {
    "T0": dict(name="Synthetic Explorer", privacy="SYNTHETIC", linkage="NONE", access="DEMO_SANDBOX",
               patient_level=False, export=True, band=(0, 5000), lead=(1, 3)),
    "T1": dict(name="Aggregate Insights", privacy="AGGREGATE_ONLY", linkage="NONE", access="SECURE_SHARE",
               patient_level=False, export=False, band=(2000, 20000), lead=(3, 10)),
    "T2": dict(name="Safe Harbor Extract", privacy="HIPAA_SAFE_HARBOR", linkage="INTERNAL_PSEUDONYMOUS_KEY",
               access="SECURE_SHARE", patient_level=True, export=True, band=(15000, 75000), lead=(10, 20)),
    "T3": dict(name="Expert Determination Certified", privacy="HIPAA_EXPERT_DETERMINATION",
               linkage="INTERNAL_PSEUDONYMOUS_KEY", access="GOVERNED_ENCLAVE", patient_level=True,
               export=True, band=(35000, 150000), lead=(20, 45)),
    "T4": dict(name="Tokenized Linkable Product", privacy="HIPAA_EXPERT_DETERMINATION+TOKENS",
               linkage="PRIVACY_PRESERVING_TOKEN", access="GOVERNED_ENCLAVE", patient_level=True,
               export=True, band=(50000, 250000), lead=(30, 60)),
    "T5": dict(name="Clean Room Collaboration", privacy="APPROVED", linkage="PRIVACY_PRESERVING_TOKEN",
               access="CLEAN_ROOM", patient_level=True, export=False, band=(75000, 500000), lead=(30, 90)),
    "T6": dict(name="Bespoke / Strategic Exchange", privacy="CUSTOM", linkage="AS_DESIGNED",
               access="CLEAN_ROOM/GOVERNED_ENCLAVE", patient_level=True, export="AS_CONTRACT",
               band=(None, None), lead=(45, 120)),
}


def _dist(rows, field, top=8):
    c = collections.Counter(r.get(field, "") for r in rows)
    return dict(c.most_common(top))


def inventory(service_line: str | None = None) -> dict:
    rows = _load()
    if not rows:
        return {"error": "demo dataset not loaded", "label": LABEL}
    if service_line:
        sl = service_line.strip().lower()
        rows = [r for r in rows if sl in (r.get("service_line", "").lower())]
    n = len(rows)
    hip = sum(1 for r in rows if r.get("hip_image_flag") == "true")
    imaging = sum(1 for r in rows if r.get("imaging_available") == "true")
    return {
        "label": LABEL,
        "scope": service_line or "all service lines",
        "encounters": n,
        "years": sorted({r.get("data_year") for r in rows}),
        "by_service_line": _dist(rows, "service_line"),
        "by_encounter_type": _dist(rows, "encounter_type"),
        "by_payer": _dist(rows, "payer_group"),
        "by_year": dict(sorted(collections.Counter(r.get("data_year", "") for r in rows).items())),
        "imaging": {"available": imaging, "by_modality": _dist(rows, "imaging_modality"),
                    "hip_image_records": hip},
        "top_conditions": _dist(rows, "primary_condition_group"),
        "top_procedures": _dist(rows, "procedure_group"),
        "tier_eligibility_tags": _dist(rows, "deid_tier_eligible"),
        "clean_room_eligible": sum(1 for r in rows if r.get("clean_room_eligible") == "true"),
    }


def cohort(criteria: dict) -> dict:
    """Filter the synthetic dataset. criteria keys (all optional): service_line, procedure_group,
    primary_condition_group, imaging_modality, payer_group, encounter_type, age_band, min_year,
    max_year, hip_image (bool), imaging_required (bool), followup_30d (bool), readmission_30d (bool)."""
    rows = _load()
    c = criteria or {}

    def keep(r):
        def has(field, val):
            return val is None or val == "" or (str(val).lower() in r.get(field, "").lower())
        if not has("service_line", c.get("service_line")): return False
        if not has("procedure_group", c.get("procedure_group")): return False
        if not has("primary_condition_group", c.get("primary_condition_group")): return False
        if not has("payer_group", c.get("payer_group")): return False
        if not has("encounter_type", c.get("encounter_type")): return False
        if not has("age_band", c.get("age_band")): return False
        if c.get("imaging_modality") and c["imaging_modality"].lower() not in r.get("imaging_modality", "").lower(): return False
        if c.get("hip_image") and r.get("hip_image_flag") != "true": return False
        if c.get("imaging_required") and r.get("imaging_available") != "true": return False
        if c.get("followup_30d") and r.get("followup_30d") != "true": return False
        if c.get("readmission_30d") and r.get("readmission_30d") != "true": return False
        try:
            y = int(r.get("data_year") or 0)
            if c.get("min_year") and y < int(c["min_year"]): return False
            if c.get("max_year") and y > int(c["max_year"]): return False
        except Exception:
            pass
        return True

    match = [r for r in rows if keep(r)]
    n = len(match)
    if n < MIN_CELL:
        return {"label": LABEL, "criteria": c, "count": n, "suppressed": True,
                "note": f"Cohort below minimum cell size ({MIN_CELL}); patient-level detail suppressed."}
    def _avg(field):
        vals = [float(r[field]) for r in match if r.get(field) not in (None, "", "N/A")]
        return round(sum(vals) / len(vals), 1) if vals else None
    return {
        "label": LABEL, "criteria": c, "count": n, "suppressed": False,
        "payer_mix": _dist(match, "payer_group"), "by_year": _dist(match, "data_year"),
        "imaging_available": sum(1 for r in match if r.get("imaging_available") == "true"),
        "hip_image_records": sum(1 for r in match if r.get("hip_image_flag") == "true"),
        "avg_experience_0_10": _avg("experience_score_0_10"),
        "readmission_30d": sum(1 for r in match if r.get("readmission_30d") == "true"),
        "tier_tags": _dist(match, "deid_tier_eligible"),
    }


def tier_eligibility(spec: dict) -> dict:
    """DETERMINISTIC engine. spec keys: intended_use, patient_level_required(bool), linkage_required(bool),
    external_dataset_type, export_required(bool), clean_room_available(bool), model_training(bool),
    rare_condition(bool), cohort_size(int). Returns eligible/ineligible tiers + a recommendation."""
    s = spec or {}
    patient = bool(s.get("patient_level_required"))
    linkage = bool(s.get("linkage_required"))
    export = s.get("export_required", True)
    clean_room = bool(s.get("clean_room_available"))
    eligible, ineligible = [], []

    # T0/T1 — no patient-level
    if patient:
        ineligible += [{"tier": "T0", "reason": "No patient-level records (synthetic only)."},
                       {"tier": "T1", "reason": "Aggregate only — no patient-level modeling/linkage."}]
    else:
        eligible += ["T0", "T1"]
    # T2 Safe Harbor — patient-level, but destroys linkage
    if patient and not linkage:
        eligible.append("T2")
    elif patient and linkage:
        ineligible.append({"tier": "T2", "reason": "Safe Harbor removes identifiers needed for linkage — "
                           "does not guarantee linkability. Use T4 tokenized."})
    # T3 Expert Determination — patient-level higher utility
    if patient:
        eligible.append("T3")
    # T4 Tokenized linkable — only path that supports external linkage
    if patient and linkage:
        eligible.append("T4")
    elif patient and not linkage:
        ineligible.append({"tier": "T4", "reason": "Tokenized linkage not needed for this use."})
    # T5 clean room — data stays put / no row export or cross-party
    if patient and (clean_room or export is False):
        eligible.append("T5")
    # T6 always available as bespoke
    eligible.append("T6")

    # recommendation
    if not patient:
        rec = "T1" if "market" in str(s.get("intended_use", "")).lower() or s.get("feasibility") else "T0"
    elif linkage:
        rec = "T4"
    elif export is False or clean_room:
        rec = "T5"
    else:
        rec = "T3" if s.get("model_training") or s.get("rare_condition") else "T2"

    out = {"label": LABEL, "eligible_tiers": [], "ineligible_tiers": ineligible, "recommended_tier": rec}
    for t in eligible:
        m = TIERS[t]
        out["eligible_tiers"].append({
            "tier": t, "name": m["name"], "privacy_method": m["privacy"], "linkage_method": m["linkage"],
            "access_environment": m["access"], "patient_level": m["patient_level"], "export_allowed": m["export"],
            "estimated_price_band_usd": {"low": m["band"][0], "high": m["band"][1]},
            "estimated_lead_days": {"low": m["lead"][0], "high": m["lead"][1]},
        })
    out["required_approvals"] = _approvals(rec)
    out["disclaimer"] = "Demo eligibility + price bands are configurable seed assumptions, not legal or market determinations."
    return out


def _approvals(tier: str) -> list:
    base = ["metadata_discovery:auto"]
    if tier in ("T2", "T3", "T4", "T5", "T6"):
        base += ["privacy_job:policy_gated", "legal_signature:authorized_human", "release_review:qualified_reviewer"]
    if tier in ("T3", "T4"):
        base.append("expert_determination:qualified_expert (NOT_CONFIGURED)")
    if tier in ("T4", "T5"):
        base.append("privacy_linkage_provider:tokenization (NOT_CONFIGURED)")
    if tier == "T5":
        base.append("clean_room_workspace:setup (NOT_CONFIGURED)")
    return base


def estimate_quote(tier: str, cohort_size: int = 0, imaging: bool = False,
                   linkage: bool = False, longitudinal_years: int = 0, clean_room: bool = False) -> dict:
    """Demo pricing configurator (Vol 9 §5). Returns a component breakdown + a range. LABELED ESTIMATE."""
    m = TIERS.get(tier)
    if not m:
        return {"error": f"unknown tier {tier}"}
    lo, hi = m["band"]
    if lo is None:
        return {"label": LABEL, "tier": tier, "estimate": "Custom quote (bespoke / strategic exchange).",
                "rate_card_version": RATE_CARD_VERSION, "disclaimer": "Demo estimate only."}
    comps = [{"component": "base_tier", "range": [lo, hi]}]
    # cohort/volume factor
    vf = 1.0 + min(0.4, (max(cohort_size, 0) / 5000.0) * 0.4)
    comps.append({"component": "cohort_volume_factor", "factor": round(vf, 2)})
    add_lo = add_hi = 0
    if imaging:
        add_lo += 10000; add_hi += 100000; comps.append({"component": "imaging_asset_package", "range": [10000, 100000]})
    if longitudinal_years and longitudinal_years > 5:
        vf2 = 1.1 + min(0.3, (longitudinal_years - 5) * 0.05); comps.append({"component": "longitudinal_depth", "factor": round(vf2, 2)}); vf *= vf2
    if linkage:
        add_lo += 20000; add_hi += 120000; comps.append({"component": "tokenization_linkage_passthrough (NOT_CONFIGURED)", "range": [20000, 120000]})
    if clean_room or tier == "T5":
        add_lo += 25000; add_hi += 150000; comps.append({"component": "clean_room_setup_compute (NOT_CONFIGURED)", "range": [25000, 150000]})
    est_lo = int(lo * vf + add_lo)
    est_hi = int(hi * vf + add_hi)
    return {"label": LABEL, "tier": tier, "tier_name": m["name"], "rate_card_version": RATE_CARD_VERSION,
            "components": comps, "estimated_total_usd": {"low": est_lo, "high": est_hi},
            "estimated_lead_days": {"low": m["lead"][0], "high": m["lead"][1]},
            "disclaimer": "Demo estimate — configurable seed assumptions, not a market quote or legal fee."}
