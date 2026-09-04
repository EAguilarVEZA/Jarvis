"""
exchange_tool — conversational tools for the Martin Health Data Exchange demo (Vol 8/9).

Lets the voice/text agent run the executive demo conversations: inventory discovery, the hip-imaging
opportunity, buyer cohort → feasibility → eligible tiers → demo quote, all over the SYNTHETIC dataset.
The deterministic engine (data_exchange.py) decides feasibility/eligibility/price; the LLM narrates.
"""
from __future__ import annotations

import data_exchange as DX

INVENTORY_SCHEMA = {
    "name": "data_exchange_inventory",
    "description": (
        "Explore the Martin Health Data Exchange SYNTHETIC demo inventory — what data COULD be "
        "commercialized: encounter counts, service lines, imaging (including hip images), payers, "
        "year coverage, and de-id tier eligibility. Use for 'what data could we commercialize', "
        "'what's in our inventory', 'show me the orthopedics/hip imaging opportunity'. Optionally focus "
        "on one service line. ALWAYS tell the user these are synthetic demo numbers, not real OH data."),
    "input_schema": {"type": "object", "properties": {
        "service_line": {"type": "string", "description": "Optional focus, e.g. 'Orthopedics'"}},
        "required": []},
}

OFFER_SCHEMA = {
    "name": "data_exchange_offer",
    "description": (
        "For a buyer's cohort request, return SYNTHETIC feasibility (matching encounters + safe "
        "aggregates), the eligible data-product tiers T0–T6 with reasons, the recommended tier, and a "
        "demo price estimate. First gather from the user BOTH the cohort (service line, procedure or "
        "condition, imaging need, payer, years) AND access needs (patient_level_required, "
        "linkage_required to external data like claims, export_required, clean_room_available, "
        "intended_use), then call this. Prices are demo estimates, not market quotes."),
    "input_schema": {"type": "object", "properties": {
        "service_line": {"type": "string"}, "procedure_group": {"type": "string"},
        "primary_condition_group": {"type": "string"}, "payer_group": {"type": "string"},
        "min_year": {"type": "integer"}, "max_year": {"type": "integer"},
        "imaging_required": {"type": "boolean"}, "hip_image": {"type": "boolean"},
        "followup_30d": {"type": "boolean"},
        "intended_use": {"type": "string"},
        "patient_level_required": {"type": "boolean"},
        "linkage_required": {"type": "boolean"},
        "export_required": {"type": "boolean"},
        "clean_room_available": {"type": "boolean"},
        "model_training": {"type": "boolean"}, "rare_condition": {"type": "boolean"}},
        "required": []},
}


async def data_exchange_inventory(args: dict) -> dict:
    sl = (args or {}).get("service_line")
    inv = DX.inventory(sl)
    return {"output": inv, "provenance": [{"source": "synthetic_demo_dataset"}],
            "is_error": "error" in inv,
            # open the visual, filterable dashboard on screen (charts + KPIs + tables)
            "ui_action": {"action": "exchange", "scope": sl or ""}}


async def data_exchange_offer(args: dict) -> dict:
    a = args or {}
    crit = {k: a.get(k) for k in ["service_line", "procedure_group", "primary_condition_group",
                                   "payer_group", "min_year", "max_year", "imaging_required", "hip_image",
                                   "followup_30d", "encounter_type", "age_band", "imaging_modality", "readmission_30d"]}
    co = DX.cohort(crit)
    spec = {"intended_use": a.get("intended_use"),
            "patient_level_required": a.get("patient_level_required"),
            "linkage_required": a.get("linkage_required"),
            "export_required": a.get("export_required", True),
            "clean_room_available": a.get("clean_room_available"),
            "model_training": a.get("model_training"), "rare_condition": a.get("rare_condition"),
            "cohort_size": co.get("count", 0),
            "feasibility": not a.get("patient_level_required")}
    elig = DX.tier_eligibility(spec)
    rec = elig.get("recommended_tier")
    yrs = 0
    try:
        if a.get("min_year") and a.get("max_year"):
            yrs = int(a["max_year"]) - int(a["min_year"])
    except Exception:
        pass
    quote = DX.estimate_quote(rec, cohort_size=co.get("count", 0),
                              imaging=bool(a.get("imaging_required") or a.get("hip_image")),
                              linkage=bool(a.get("linkage_required")), longitudinal_years=yrs,
                              clean_room=bool(a.get("clean_room_available")))
    return {"output": {"feasibility": co, "tiers": elig, "recommended_quote": quote, "label": DX.LABEL},
            "provenance": [{"source": "synthetic_demo_dataset"}], "is_error": False}
