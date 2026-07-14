"""Data Marketplace — shared core logic (no web framework).

All catalog / pricing / order / fulfillment logic lives here so BOTH the REST API
(`marketplace_api.py`, for the human UI) and the MCP server (`marketplace_mcp.py`,
for AI agents — internal and external) call one identical backend. There is no
business logic duplicated between them.

Money is never moved: `pay_order` only records an external payment reference that
your billing provider already settled, which unlocks fulfillment.
"""
from __future__ import annotations

import json
import os
import time
import uuid
import logging
import asyncio
from typing import List, Optional

log = logging.getLogger("marketplace")

_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_PATH = os.path.join(_DIR, "marketplace_products.json")
_ORDER_PATH = os.path.join(_DIR, "marketplace_orders.json")


def _load(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {key: []}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Seed catalog — de-identified Orlando Health-style data products.
# ---------------------------------------------------------------------------
def seed_products():
    return [
        {
            "id": "inpatient-encounters",
            "name": "De-identified Inpatient Encounters",
            "category": "Clinical", "domain": "healthcare",
            "summary": "Admissions, diagnoses, procedures, LOS and discharge disposition across the inpatient network.",
            "description": "Longitudinal inpatient stays with ICD-10 diagnoses/procedures, DRG, length of stay, "
                           "admit/discharge type and de-identified patient keys for cohort linkage.",
            "compliance": "HIPAA Safe Harbor de-identified; dates shifted, ages 90+ aggregated; expert-determination available.",
            "refresh": "Monthly", "row_count": 4_180_000,
            "fields": [
                {"name": "patient_token", "type": "string", "desc": "Stable de-identified linkage key"},
                {"name": "encounter_id", "type": "string", "desc": "Unique stay identifier"},
                {"name": "admit_date_shifted", "type": "date", "desc": "Date-shifted admit date"},
                {"name": "los_days", "type": "int", "desc": "Length of stay"},
                {"name": "drg", "type": "string", "desc": "MS-DRG code"},
                {"name": "primary_dx", "type": "string", "desc": "Primary ICD-10 diagnosis"},
                {"name": "discharge_disposition", "type": "string", "desc": "Home / SNF / expired / AMA"},
            ],
            "sample": [
                {"patient_token": "PT_9af3", "encounter_id": "E10231", "los_days": 4, "drg": "291", "primary_dx": "I50.9"},
                {"patient_token": "PT_1b77", "encounter_id": "E10232", "los_days": 2, "drg": "690", "primary_dx": "N39.0"},
            ],
            "pricing": {"model": "per_record", "unit_price": 0.012, "currency": "USD",
                        "floor": 5000, "notes": "Priced per licensed row, $5k engagement floor."},
            "tags": ["inpatient", "diagnoses", "DRG", "cohort", "clinical outcomes"], "status": "active",
        },
        {
            "id": "claims-billing",
            "name": "Claims & Billing (tokenized)",
            "category": "Financial", "domain": "healthcare",
            "summary": "Charge, payment and adjustment detail with payer mix — tokenized for clean-room linkage.",
            "description": "Institutional and professional claim lines: CPT/HCPCS, charge amount, allowed, paid, "
                           "payer category and service date. Tokenized patient/provider keys for privacy-safe joins.",
            "compliance": "Tokenized + Safe Harbor; direct identifiers removed; suitable for clean-room matching.",
            "refresh": "Monthly", "row_count": 22_600_000,
            "fields": [
                {"name": "claim_token", "type": "string", "desc": "De-identified claim key"},
                {"name": "cpt", "type": "string", "desc": "CPT/HCPCS procedure code"},
                {"name": "charge_amt", "type": "float", "desc": "Billed charge"},
                {"name": "allowed_amt", "type": "float", "desc": "Payer-allowed amount"},
                {"name": "payer_category", "type": "string", "desc": "Commercial / Medicare / Medicaid / Self-pay"},
                {"name": "service_date_shifted", "type": "date", "desc": "Date-shifted service date"},
            ],
            "sample": [
                {"claim_token": "CL_44a", "cpt": "99223", "charge_amt": 612.0, "allowed_amt": 214.5, "payer_category": "Medicare"},
            ],
            "pricing": {"model": "per_record", "unit_price": 0.004, "currency": "USD",
                        "floor": 7500, "notes": "High volume; per-line pricing."},
            "tags": ["claims", "reimbursement", "payer mix", "revenue cycle", "CPT"], "status": "active",
        },
        {
            "id": "ed-throughput",
            "name": "Emergency Dept Throughput",
            "category": "Operational", "domain": "healthcare",
            "summary": "Arrival-to-disposition timestamps, acuity (ESI), and boarding metrics by facility & hour.",
            "description": "Operational ED flow: door-to-triage, door-to-provider, boarding time, ESI acuity, "
                           "LWBS flag and disposition — aggregated to protect small cells.",
            "compliance": "Aggregated operational metrics; no patient-level PHI released below k-anonymity threshold.",
            "refresh": "Weekly", "row_count": 1_950_000,
            "fields": [
                {"name": "facility", "type": "string", "desc": "De-identified facility code"},
                {"name": "arrival_hour", "type": "int", "desc": "Hour of day (0-23)"},
                {"name": "esi", "type": "int", "desc": "Emergency Severity Index 1-5"},
                {"name": "door_to_provider_min", "type": "int", "desc": "Minutes to first provider"},
                {"name": "boarding_min", "type": "int", "desc": "Boarding minutes"},
                {"name": "lwbs", "type": "bool", "desc": "Left without being seen"},
            ],
            "sample": [
                {"facility": "F02", "arrival_hour": 19, "esi": 3, "door_to_provider_min": 41, "boarding_min": 128},
            ],
            "pricing": {"model": "subscription", "unit_price": 4000.0, "currency": "USD",
                        "period": "month", "notes": "Monthly operational feed."},
            "tags": ["emergency", "throughput", "operations", "capacity", "wait times"], "status": "active",
        },
        {
            "id": "pharmacy-dispensing",
            "name": "Pharmacy Dispensing (Safe Harbor)",
            "category": "Clinical", "domain": "healthcare",
            "summary": "Inpatient & outpatient medication dispensing by drug class, route and setting.",
            "description": "Dispensing events with NDC/therapeutic class, dose, route, and care setting for "
                           "real-world utilization and adherence studies.",
            "compliance": "HIPAA Safe Harbor; patient tokens only; ready for life-sciences RWE.",
            "refresh": "Monthly", "row_count": 9_300_000,
            "fields": [
                {"name": "patient_token", "type": "string", "desc": "De-identified patient key"},
                {"name": "therapeutic_class", "type": "string", "desc": "Drug therapeutic class"},
                {"name": "route", "type": "string", "desc": "PO / IV / IM ..."},
                {"name": "setting", "type": "string", "desc": "Inpatient / outpatient / ED"},
                {"name": "dispense_date_shifted", "type": "date", "desc": "Date-shifted dispense date"},
            ],
            "sample": [
                {"patient_token": "PT_9af3", "therapeutic_class": "Anticoagulant", "route": "PO", "setting": "outpatient"},
            ],
            "pricing": {"model": "per_record", "unit_price": 0.006, "currency": "USD",
                        "floor": 6000, "notes": "Popular for pharma RWE."},
            "tags": ["pharmacy", "medications", "RWE", "adherence", "life sciences"], "status": "active",
        },
        {
            "id": "imaging-orders",
            "name": "Imaging Orders & Utilization",
            "category": "Clinical", "domain": "healthcare",
            "summary": "Radiology order volumes by modality, body region, indication and turnaround.",
            "description": "CT/MR/US/XR order and completion events with indication category and report "
                           "turnaround — no images, metadata only.",
            "compliance": "Metadata only; de-identified; no DICOM pixel data.",
            "refresh": "Monthly", "row_count": 3_100_000,
            "fields": [
                {"name": "modality", "type": "string", "desc": "CT / MR / US / XR"},
                {"name": "body_region", "type": "string", "desc": "Ordered region"},
                {"name": "indication", "type": "string", "desc": "Indication category"},
                {"name": "turnaround_hours", "type": "float", "desc": "Order-to-report hours"},
            ],
            "sample": [
                {"modality": "CT", "body_region": "Abdomen/Pelvis", "indication": "Abdominal pain", "turnaround_hours": 3.2},
            ],
            "pricing": {"model": "per_query", "unit_price": 0.25, "currency": "USD",
                        "notes": "Priced per delivered query result."},
            "tags": ["radiology", "imaging", "utilization", "modality"], "status": "active",
        },
        {
            "id": "population-cohorts",
            "name": "Population Health Cohorts",
            "category": "Research", "domain": "healthcare",
            "summary": "Pre-built de-identified cohorts (diabetes, CHF, oncology) with outcomes & SDOH markers.",
            "description": "Curated chronic-disease and oncology cohorts with condition flags, comorbidity index, "
                           "utilization and area-level social-determinant markers for research and value-based care.",
            "compliance": "Expert Determination available; SDOH at area (not individual) level; IRB-friendly.",
            "refresh": "Quarterly", "row_count": 780_000,
            "fields": [
                {"name": "patient_token", "type": "string", "desc": "De-identified key"},
                {"name": "cohort", "type": "string", "desc": "Diabetes / CHF / Oncology ..."},
                {"name": "comorbidity_index", "type": "float", "desc": "Charlson-style index"},
                {"name": "annual_ed_visits", "type": "int", "desc": "ED visits per year"},
                {"name": "sdoh_area_score", "type": "float", "desc": "Area deprivation index"},
            ],
            "sample": [
                {"patient_token": "PT_1b77", "cohort": "CHF", "comorbidity_index": 3.4, "annual_ed_visits": 2},
            ],
            "pricing": {"model": "rev_share", "unit_price": 0.0, "currency": "USD",
                        "rev_share_pct": 12.0, "notes": "12% revenue share on downstream products."},
            "tags": ["cohorts", "population health", "SDOH", "value-based care", "research"], "status": "active",
        },
    ]


def products():
    store = _load(_PROD_PATH, "products")
    items = store.get("products", [])
    if not items:
        items = seed_products()
        _save(_PROD_PATH, {"products": items})
    return items


def product(pid):
    for p in products():
        if p.get("id") == pid:
            return p
    return None


def list_products(category="", q="", domain="", published_only=False):
    items = [p for p in products() if p.get("status", "active") != "retired"]
    if published_only:
        items = [p for p in items if p.get("status", "active") == "active"]
    if category:
        items = [p for p in items if p.get("category", "").lower() == category.lower()]
    if domain:
        items = [p for p in items if p.get("domain", "").lower() == domain.lower()]
    if q:
        ql = q.lower()
        items = [p for p in items if ql in json.dumps(p).lower()]
    lite = []
    for p in items:
        lite.append({k: p[k] for k in ("id", "name", "category", "domain", "summary",
                                       "compliance", "refresh", "row_count", "pricing", "tags", "status")
                     if k in p})
    return lite


def categories():
    facet = {}
    for p in products():
        c = p.get("category", "Other")
        facet[c] = facet.get(c, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(facet.items())]


def upsert_product(d: dict):
    store = _load(_PROD_PATH, "products")
    items = store.get("products") or seed_products()
    d = dict(d)
    d["id"] = d.get("id") or (d.get("name", "product").lower().replace(" ", "-")[:40] + "-" + uuid.uuid4().hex[:4])
    d.setdefault("fields", [])
    d.setdefault("status", "active")
    d.setdefault("pricing", {"model": "per_record", "unit_price": 0.01, "currency": "USD"})
    for i, p in enumerate(items):
        if p.get("id") == d["id"]:
            items[i] = d
            break
    else:
        items.append(d)
    _save(_PROD_PATH, {"products": items})
    return d


def retire_product(pid):
    store = _load(_PROD_PATH, "products")
    items = store.get("products") or seed_products()
    for p in items:
        if p.get("id") == pid:
            p["status"] = "retired"
    _save(_PROD_PATH, {"products": items})
    return True


# ---------------------------------------------------------------------------
# Pricing — pure, unit-tested.
# ---------------------------------------------------------------------------
def price_product(prod: dict, volume: int = 0, months: int = 12) -> dict:
    pr = prod.get("pricing", {})
    model = pr.get("model", "per_record")
    cur = pr.get("currency", "USD")
    line = {"product_id": prod.get("id"), "name": prod.get("name"), "model": model, "currency": cur}
    if model == "per_record":
        rows = volume or prod.get("row_count", 0)
        amt = max(rows * float(pr.get("unit_price", 0)), float(pr.get("floor", 0)))
        line.update({"rows": rows, "unit_price": pr.get("unit_price"), "amount": round(amt, 2), "recurring": False})
    elif model == "subscription":
        amt = float(pr.get("unit_price", 0)) * months
        line.update({"months": months, "monthly": pr.get("unit_price"), "amount": round(amt, 2), "recurring": True})
    elif model == "per_query":
        qn = volume or 10000
        amt = qn * float(pr.get("unit_price", 0))
        line.update({"queries": qn, "unit_price": pr.get("unit_price"), "amount": round(amt, 2), "recurring": False})
    elif model == "rev_share":
        line.update({"rev_share_pct": pr.get("rev_share_pct", 0), "amount": 0.0, "recurring": True,
                     "note": f"{pr.get('rev_share_pct', 0)}% of downstream revenue"})
    else:
        line.update({"amount": 0.0})
    return line


def price_bundle(prods: List[dict], volume: int = 0, months: int = 12, bundle_discount: float = 0.10) -> dict:
    lines = [price_product(p, volume=volume, months=months) for p in prods]
    subtotal = round(sum(l.get("amount", 0) for l in lines), 2)
    discount = round(subtotal * bundle_discount, 2) if len(prods) > 1 else 0.0
    total = round(subtotal - discount, 2)
    rev_share = [{"name": l["name"], "pct": l.get("rev_share_pct")} for l in lines if l.get("model") == "rev_share"]
    return {
        "lines": lines, "subtotal": subtotal,
        "bundle_discount_pct": bundle_discount * 100 if len(prods) > 1 else 0,
        "discount": discount, "total_upfront": total, "rev_share": rev_share,
        "projected_downstream_value": round(total * 3.0, 2),
        "currency": (prods[0].get("pricing", {}).get("currency", "USD") if prods else "USD"),
    }


def keyword_rank(query: str, items: list) -> list:
    ql = set(w for w in query.lower().replace("/", " ").split() if len(w) > 2)
    scored = []
    for p in items:
        hay = json.dumps({k: p.get(k) for k in ("name", "summary", "description", "category", "tags")}).lower()
        score = sum(1 for w in ql if w in hay)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored]


def _anthropic_provider():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    return {"type": "anthropic", "api_key": key,
            "model": os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"), "name": "Claude"}


async def search_catalog(query: str, published_only=False) -> dict:
    items = [p for p in products() if p.get("status") != "retired"]
    if published_only:
        items = [p for p in items if p.get("status") == "active"]
    ranked = keyword_rank(query, items) or items
    narrative = ""
    prov = _anthropic_provider()
    if prov and ranked:
        try:
            import llm_router
            cat = [{"id": p["id"], "name": p["name"], "summary": p.get("summary", ""),
                    "category": p.get("category"), "pricing": p.get("pricing", {}).get("model")} for p in ranked[:6]]
            sysp = ("You are the Orlando Health Data Marketplace concierge. A buyer describes a need; recommend which "
                    "of THESE catalog products fit and how to combine them for their use case. 4-6 sentences, mention "
                    "product names. Note compliance is de-identified/HIPAA.")
            msg = f"Buyer need: {query}\n\nCatalog (JSON):\n{json.dumps(cat)}"
            r = await llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=500)
            narrative = r.get("output", "") or ""
        except Exception as e:
            log.info(f"search narrative skipped: {e}")
    lite = [{k: p.get(k) for k in ("id", "name", "category", "summary", "pricing", "tags")} for p in ranked[:8]]
    return {"query": query, "matches": lite, "narrative": narrative}


async def recommend_bundle(use_case: str, published_only=False) -> dict:
    items = [p for p in products() if p.get("status") != "retired"]
    if published_only:
        items = [p for p in items if p.get("status") == "active"]
    ranked = keyword_rank(use_case, items) or items
    picks = ranked[:3]
    bundle = price_bundle(picks)
    rationale = ""
    prov = _anthropic_provider()
    if prov:
        try:
            import llm_router
            cat = [{"id": p["id"], "name": p["name"], "summary": p.get("summary")} for p in picks]
            sysp = ("Explain in 3-4 sentences why combining these Orlando Health data products creates a sellable "
                    "package for the buyer's use case, and one example insight the combined data unlocks.")
            msg = f"Use case: {use_case}\nProducts: {json.dumps(cat)}"
            r = await llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=400)
            rationale = r.get("output", "")
        except Exception:
            pass
    return {"use_case": use_case,
            "recommended": [{"id": p["id"], "name": p["name"], "summary": p.get("summary")} for p in picks],
            "bundle": bundle, "rationale": rationale}


# ---------------------------------------------------------------------------
# Orders — quote -> pay -> fulfill
# ---------------------------------------------------------------------------
def orders():
    return _load(_ORDER_PATH, "orders").get("orders", [])


def save_orders(o):
    _save(_ORDER_PATH, {"orders": o})


def order(oid):
    for o in orders():
        if o["id"] == oid:
            return o
    return None


def create_order(product_ids, buyer_org, buyer_email="", use_case="", volume=0, months=12) -> dict:
    prods = [p for p in (product(i) for i in product_ids) if p]
    if not prods:
        return {"error": "no valid products"}
    bundle = price_bundle(prods, volume=volume, months=months)
    o = {
        "id": "ord_" + uuid.uuid4().hex[:10], "created": time.time(), "status": "quote",
        "buyer_org": buyer_org, "buyer_email": buyer_email, "use_case": use_case,
        "product_ids": [p["id"] for p in prods], "product_names": [p["name"] for p in prods],
        "volume": volume, "months": months, "quote": bundle, "payment_ref": None, "delivery": None,
    }
    allo = orders()
    allo.insert(0, o)
    save_orders(allo)
    return o


def pay_order(oid, payment_ref) -> dict:
    allo = orders()
    for o in allo:
        if o["id"] == oid:
            if not (payment_ref or "").strip():
                return {"error": "payment_ref required (settle via your billing provider first)"}
            o["status"] = "paid"
            o["payment_ref"] = payment_ref.strip()
            o["paid_at"] = time.time()
            save_orders(allo)
            return o
    return {"error": "not found"}


async def code_delivery(o: dict) -> dict:
    """Agents 'code the final output': produce a licensed-delivery spec."""
    prods = [p for p in (product(i) for i in o.get("product_ids", [])) if p]
    schema = {p["id"]: {"name": p["name"], "fields": p.get("fields", []),
                        "compliance": p.get("compliance", "")} for p in prods}
    spec_text = ""
    prov = _anthropic_provider()
    if prov:
        try:
            import llm_router
            sysp = ("You are the Orlando Health data-engineering agent fulfilling a paid data-license order. Produce "
                    "the DELIVERY SPEC: (1) SQL that assembles/joins the ordered products on the de-identified "
                    "patient/claim token, (2) transforms & filters for the buyer's use case, (3) de-identification / "
                    "Safe-Harbor steps applied, (4) a concise data dictionary. Clean markdown with fenced SQL. Never "
                    "expose real PHI.")
            msg = (f"Buyer: {o.get('buyer_org')}\nUse case: {o.get('use_case')}\n"
                   f"Ordered products & schema:\n{json.dumps(schema, indent=2)[:6000]}")
            r = await llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=1600)
            spec_text = r.get("output", "") or ""
        except Exception as e:
            log.info(f"delivery codegen skipped: {e}")
    if not spec_text:
        joins = "\n".join(f"  LEFT JOIN {p['id'].replace('-', '_')} USING (patient_token)" for p in prods[1:])
        base = prods[0]["id"].replace("-", "_") if prods else "product"
        spec_text = (f"# Licensed delivery spec — {o.get('buyer_org', 'buyer')}\n\n## Assembly SQL\n```sql\nSELECT *\n"
                     f"FROM {base}\n{joins}\nWHERE 1=1  -- cohort filters for: {o.get('use_case', 'general')}\n;\n```\n\n"
                     "## De-identification\nHIPAA Safe Harbor: direct identifiers removed, dates shifted, ages 90+ "
                     "aggregated, small cells suppressed (k<11). Delivered via secure clean room.\n\n## Data dictionary\n"
                     + "\n".join(f"- **{f['name']}** ({f['type']}): {f.get('desc', '')}"
                                 for p in prods for f in p.get("fields", [])))
    return {"spec": spec_text, "products": [p["id"] for p in prods],
            "generated_at": time.time(), "format": "clean_room_delivery"}


async def fulfill_order(oid) -> dict:
    allo = orders()
    for o in allo:
        if o["id"] == oid:
            if o.get("status") not in ("paid", "fulfilled"):
                return {"error": f"order is '{o.get('status')}', must be paid before fulfillment"}
            o["delivery"] = await code_delivery(o)
            o["status"] = "fulfilled"
            o["fulfilled_at"] = time.time()
            lic = _issue_license(o)          # Phase 2: license + secure clean-room delivery
            o["license_id"] = lic["id"]
            o["delivery_token"] = lic["delivery_token"]
            save_orders(allo)
            return o
    return {"error": "not found"}


# ---------------------------------------------------------------------------
# Phase 2 — Licensing, secure clean-room delivery, revenue attribution
# ---------------------------------------------------------------------------
_LICENSE_PATH = os.path.join(_DIR, "marketplace_licenses.json")
_DAY = 86400


def licenses():
    return _load(_LICENSE_PATH, "licenses").get("licenses", [])


def save_licenses(ls):
    _save(_LICENSE_PATH, {"licenses": ls})


def license_by_id(lid):
    for l in licenses():
        if l["id"] == lid:
            return l
    return None


def license_by_token(token):
    for l in licenses():
        if l.get("delivery_token") == token:
            return l
    return None


def _issue_license(o: dict) -> dict:
    """Issue a license + a secure, revocable clean-room delivery token for a
    fulfilled order. The token is how a buyer (or their agent) accesses the
    licensed output; every access is logged."""
    ls = licenses()
    existing = next((l for l in ls if l.get("order_id") == o["id"]), None)
    if existing:
        return existing
    # subscription orders are termed by months; one-time licenses default to 12mo.
    recurring = any(l.get("recurring") for l in o.get("quote", {}).get("lines", []))
    term_months = o.get("months", 12) if recurring else 12
    now = time.time()
    lic = {
        "id": "lic_" + uuid.uuid4().hex[:10],
        "order_id": o["id"],
        "buyer_org": o.get("buyer_org", ""),
        "product_ids": o.get("product_ids", []),
        "scope": o.get("use_case", ""),
        "term_months": term_months,
        "issued_at": now,
        "expires_at": now + term_months * 30 * _DAY,
        "status": "active",
        "delivery_token": uuid.uuid4().hex,   # secure access token for the delivery
        "access_count": 0,
        "audit": [{"ts": now, "event": "issued", "detail": f"license for order {o['id']}"}],
    }
    ls.append(lic)
    save_licenses(ls)
    return lic


def access_delivery(token: str, actor: str = "buyer") -> dict:
    """Buyer-facing: exchange a delivery token for the licensed output. Enforces
    expiry + revocation and writes an audit entry on every access."""
    ls = licenses()
    lic = next((l for l in ls if l.get("delivery_token") == token), None)
    if not lic:
        return {"error": "invalid delivery token"}
    if lic.get("status") != "active":
        return {"error": f"license is {lic.get('status')}"}
    if time.time() > lic.get("expires_at", 0):
        lic["status"] = "expired"
        save_licenses(ls)
        return {"error": "license expired"}
    o = order(lic["order_id"])
    delivery = (o or {}).get("delivery") or {}
    lic["access_count"] = lic.get("access_count", 0) + 1
    lic["audit"].append({"ts": time.time(), "event": "access", "detail": f"delivery retrieved by {actor}"})
    save_licenses(ls)
    return {"license_id": lic["id"], "buyer_org": lic["buyer_org"], "scope": lic["scope"],
            "expires_at": lic["expires_at"], "access_count": lic["access_count"],
            "delivery": delivery}


def revoke_license(lid: str) -> dict:
    ls = licenses()
    for l in ls:
        if l["id"] == lid:
            l["status"] = "revoked"
            l["audit"].append({"ts": time.time(), "event": "revoked", "detail": "license revoked"})
            save_licenses(ls)
            return l
    return {"error": "not found"}


def record_downstream_revenue(oid: str, amount: float) -> dict:
    """For revenue-share products: the buyer reports downstream revenue derived from
    the licensed data; we compute the share Orlando Health has earned."""
    allo = orders()
    for o in allo:
        if o["id"] == oid:
            rs_lines = [l for l in o.get("quote", {}).get("lines", []) if l.get("model") == "rev_share"]
            if not rs_lines:
                return {"error": "this order has no revenue-share products"}
            pct = max((l.get("rev_share_pct", 0) for l in rs_lines), default=0)
            rec = o.setdefault("downstream", {"reported_total": 0.0, "share_pct": pct, "entries": []})
            rec["reported_total"] = round(rec["reported_total"] + float(amount), 2)
            rec["share_pct"] = pct
            rec["owed"] = round(rec["reported_total"] * pct / 100.0, 2)
            rec["entries"].append({"ts": time.time(), "amount": round(float(amount), 2)})
            save_orders(allo)
            return {"ok": True, "order_id": oid, "downstream": rec}
    return {"error": "not found"}


def insights() -> dict:
    """Revenue attribution across the marketplace: by product, buyer, category,
    the order funnel, a monthly booking trend, and revenue-share owed."""
    allo = orders()
    prods = {p["id"]: p for p in products()}
    booked = [o for o in allo if o.get("status") in ("paid", "fulfilled")]

    by_product, by_buyer, by_category = {}, {}, {}
    for o in booked:
        total = o.get("quote", {}).get("total_upfront", 0) or 0
        ids = o.get("product_ids", []) or []
        split = (total / len(ids)) if ids else 0     # attribute a bundle evenly across its products
        for pid in ids:
            by_product[pid] = round(by_product.get(pid, 0) + split, 2)
            cat = prods.get(pid, {}).get("category", "Other")
            by_category[cat] = round(by_category.get(cat, 0) + split, 2)
        by_buyer[o.get("buyer_org", "—")] = round(by_buyer.get(o.get("buyer_org", "—"), 0) + total, 2)

    funnel = {"quote": sum(1 for o in allo if o.get("status") == "quote"),
              "paid": sum(1 for o in allo if o.get("status") == "paid"),
              "fulfilled": sum(1 for o in allo if o.get("status") == "fulfilled")}

    trend = {}
    for o in booked:
        ts = o.get("paid_at") or o.get("created") or time.time()
        key = time.strftime("%Y-%m", time.localtime(ts))
        trend[key] = round(trend.get(key, 0) + (o.get("quote", {}).get("total_upfront", 0) or 0), 2)

    rev_share_owed = round(sum((o.get("downstream", {}) or {}).get("owed", 0) for o in allo), 2)
    downstream_reported = round(sum((o.get("downstream", {}) or {}).get("reported_total", 0) for o in allo), 2)

    def _named(d):
        return sorted(({"id": k, "name": prods.get(k, {}).get("name", k), "revenue": v}
                       for k, v in d.items()), key=lambda x: -x["revenue"])

    return {
        "revenue_booked": round(sum(o.get("quote", {}).get("total_upfront", 0) or 0 for o in booked), 2),
        "pipeline_quoted": round(sum(o.get("quote", {}).get("total_upfront", 0) or 0
                                     for o in allo if o.get("status") == "quote"), 2),
        "orders_total": len(allo),
        "active_licenses": sum(1 for l in licenses() if l.get("status") == "active"),
        "by_product": _named(by_product),
        "by_buyer": sorted(({"buyer": k, "revenue": v} for k, v in by_buyer.items()), key=lambda x: -x["revenue"]),
        "by_category": sorted(({"category": k, "revenue": v} for k, v in by_category.items()), key=lambda x: -x["revenue"]),
        "funnel": funnel,
        "monthly_trend": [{"month": k, "revenue": trend[k]} for k in sorted(trend)],
        "rev_share_owed": rev_share_owed,
        "downstream_reported": downstream_reported,
    }


def stats():
    allo = orders()
    paid = [o for o in allo if o.get("status") in ("paid", "fulfilled")]
    revenue = round(sum(o.get("quote", {}).get("total_upfront", 0) for o in paid), 2)
    pipeline = round(sum(o.get("quote", {}).get("total_upfront", 0) for o in allo if o.get("status") == "quote"), 2)
    return {"products": len([p for p in products() if p.get("status") != "retired"]),
            "orders": len(allo), "paid": len(paid), "revenue_booked": revenue, "pipeline_quoted": pipeline,
            "projected_downstream": round(sum(o.get("quote", {}).get("projected_downstream_value", 0) for o in paid), 2)}
