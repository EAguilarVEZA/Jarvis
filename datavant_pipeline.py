"""Datavant data pipeline — ingest a dataset, tokenize, de-identify, certify, and
publish it to the Data Marketplace ready to sell. Plus contracted-review "data
rooms" so a customer under contract can review a dataset before licensing it.

Asset lifecycle (stages)
------------------------
  connected -> tokenized -> deidentified -> certified -> published

Each stage is explicit and auditable. Publishing turns a certified asset into a
marketplace product (carrying its Datavant certification), so the sell-ready dataset
shows up in the catalog with a certified badge.

Contracted review
------------------
A buyer requests review of a product/asset; an admin grants it against a contract
(NDA/agreement id + term). Granting issues a scoped, expiring REVIEW TOKEN that
exposes only the schema, a de-identified sample, and summary stats — never the full
dataset. Every review access is logged.
"""
from __future__ import annotations

import os
import json
import time
import uuid
import logging

import datavant_client as dvt
import marketplace_core as market

log = logging.getLogger("datavant_pipeline")

_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSET_PATH = os.path.join(_DIR, "datavant_assets.json")
_CONTRACT_PATH = os.path.join(_DIR, "datavant_contracts.json")

STAGES = ["connected", "tokenized", "deidentified", "certified", "published"]


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
# Sample datasets to demo the pipeline (synthetic PII — safe, not real people).
# ---------------------------------------------------------------------------
def _seed_assets():
    return [
        {
            "id": "asset-oncology-registry",
            "name": "Oncology Registry Extract",
            "source": "Epic Clarity → CSV",
            "category": "Research", "domain": "healthcare",
            "summary": "Tumor registry with staging, treatment and outcomes for a cancer research cohort.",
            "stage": "connected",
            "pii_fields": ["first_name", "last_name", "dob", "ssn"],
            "sample": [
                {"first_name": "Jane", "last_name": "Doe", "dob": "1961-03-14", "ssn": "111-22-3333",
                 "zip": "32801", "age": 64, "tumor_site": "Breast", "stage_group": "IIA",
                 "regimen": "AC-T", "months_survival": 58},
                {"first_name": "John", "last_name": "Roe", "dob": "1949-11-02", "ssn": "444-55-6666",
                 "zip": "32803", "age": 76, "tumor_site": "Lung", "stage_group": "IIIB",
                 "regimen": "Carbo-Pem", "months_survival": 22},
            ],
            "pricing": {"model": "rev_share", "currency": "USD", "rev_share_pct": 15.0,
                        "notes": "Research cohort — revenue share."},
            "tokenization": None, "deid": None, "certification": None, "product_id": None,
        },
        {
            "id": "asset-cardio-claims",
            "name": "Cardiology Claims Feed",
            "source": "Clearinghouse 837 → warehouse",
            "category": "Financial", "domain": "healthcare",
            "summary": "Cardiology professional & facility claim lines with charges, payer and CPT.",
            "stage": "connected",
            "pii_fields": ["member_name", "dob", "member_id"],
            "sample": [
                {"member_name": "Alice Smith", "dob": "1972-06-30", "member_id": "M83920",
                 "zip": "34741", "cpt": "93458", "charge_amt": 4200.0, "payer_category": "Commercial"},
                {"member_name": "Bob Jones", "dob": "1958-01-19", "member_id": "M11827",
                 "zip": "34744", "cpt": "92928", "charge_amt": 15800.0, "payer_category": "Medicare"},
            ],
            "pricing": {"model": "per_record", "unit_price": 0.005, "currency": "USD", "floor": 6000},
            "tokenization": None, "deid": None, "certification": None, "product_id": None,
        },
    ]


def assets():
    store = _load(_ASSET_PATH, "assets")
    items = store.get("assets", [])
    if not items:
        items = _seed_assets()
        _save(_ASSET_PATH, {"assets": items})
    return items


def save_assets(a):
    _save(_ASSET_PATH, {"assets": a})


def asset(aid):
    for a in assets():
        if a["id"] == aid:
            return a
    return None


def _schema_from_sample(sample):
    if not sample:
        return []
    row = sample[0]
    def _t(v):
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        return "string"
    return [{"name": k, "type": _t(v)} for k, v in row.items()]


def register_asset(name, source="", category="Other", summary="", sample=None,
                   pii_fields=None, pricing=None, domain="healthcare") -> dict:
    a = {
        "id": "asset-" + uuid.uuid4().hex[:8], "name": name, "source": source,
        "category": category, "domain": domain, "summary": summary,
        "stage": "connected", "pii_fields": pii_fields or [],
        "sample": sample or [], "pricing": pricing or {"model": "per_record", "unit_price": 0.01, "currency": "USD"},
        "tokenization": None, "deid": None, "certification": None, "product_id": None,
    }
    allo = assets()
    allo.insert(0, a)
    save_assets(allo)
    return a


def _update(aid, **fields):
    allo = assets()
    for a in allo:
        if a["id"] == aid:
            a.update(fields)
            save_assets(allo)
            return a
    return {"error": "not found"}


# ── Pipeline steps ─────────────────────────────────────────────────────────
def run_tokenize(aid, recipient="") -> dict:
    a = asset(aid)
    if not a:
        return {"error": "asset not found"}
    res = dvt.tokenize(a.get("sample", []), a.get("pii_fields", []), recipient=recipient)
    tok = {"token_types": res["token_types"], "recipient": res.get("recipient"),
           "count": res["count"], "mode": res["mode"], "at": time.time(),
           "preview": res["tokens"][:2]}
    return _update(aid, tokenization=tok, stage="tokenized")


def run_deidentify(aid, method="safe_harbor") -> dict:
    a = asset(aid)
    if not a:
        return {"error": "asset not found"}
    if a.get("stage") == "connected":
        return {"error": "tokenize before de-identifying"}
    res = dvt.deidentify(a.get("sample", []), method=method)
    deid = {"method": res["method"], "transforms": res["transforms"],
            "identifiers_removed": res["identifiers_removed"], "at": time.time()}
    # store the de-identified sample so the certified/published asset shows clean data
    return _update(aid, deid=deid, deid_sample=res["records"], stage="deidentified")


def run_certify(aid, expert="") -> dict:
    a = asset(aid)
    if not a:
        return {"error": "asset not found"}
    if not a.get("deid"):
        return {"error": "de-identify before certifying"}
    cert = dvt.certify(a["name"], a["deid"]["method"], a["deid"]["transforms"], expert=expert)
    return _update(aid, certification=cert, stage="certified")


def publish_to_marketplace(aid) -> dict:
    a = asset(aid)
    if not a:
        return {"error": "asset not found"}
    if not a.get("certification"):
        return {"error": "certify before publishing"}
    cert = a["certification"]
    clean_sample = (a.get("deid_sample") or a.get("sample") or [])[:3]
    product = {
        "id": a["id"].replace("asset-", "prod-"),
        "name": a["name"], "category": a.get("category", "Other"), "domain": a.get("domain", "healthcare"),
        "summary": a.get("summary", ""),
        "description": a.get("summary", ""),
        "compliance": f"{cert['standard']} — Datavant certified ({cert['certification_id']}). "
                      f"Tokenized (master/site) for privacy-safe linkage.",
        "refresh": "Monthly",
        "row_count": len(a.get("sample", [])) * 100000,   # sample stands in for the full extract
        "fields": _schema_from_sample(clean_sample),
        "sample": clean_sample,
        "pricing": a.get("pricing") or {"model": "per_record", "unit_price": 0.01, "currency": "USD"},
        "tags": [a.get("category", "").lower(), "datavant-certified", "tokenized", "de-identified"],
        "status": "active",
        "datavant": {"certified": True, "certification_id": cert["certification_id"],
                     "method": cert["method"], "token_types": (a.get("tokenization") or {}).get("token_types", [])},
    }
    saved = market.upsert_product(product)
    _update(aid, product_id=saved["id"], stage="published")
    return {"ok": True, "product": saved, "asset_id": aid}


# ---------------------------------------------------------------------------
# Contracted review ("data rooms")
# ---------------------------------------------------------------------------
def contracts():
    return _load(_CONTRACT_PATH, "contracts").get("contracts", [])


def save_contracts(c):
    _save(_CONTRACT_PATH, {"contracts": c})


def contract(cid):
    for c in contracts():
        if c["id"] == cid:
            return c
    return None


def contract_by_token(token):
    for c in contracts():
        if c.get("review_token") == token:
            return c
    return None


def _target(target_id):
    """Resolve a review target to (kind, name, sample, schema). Works for a published
    marketplace product OR a pipeline asset."""
    p = market.product(target_id)
    if p:
        return "product", p.get("name"), p.get("sample", []), p.get("fields", []), p.get("summary", "")
    a = asset(target_id)
    if a:
        s = a.get("deid_sample") or a.get("sample") or []
        return "asset", a.get("name"), s, _schema_from_sample(s), a.get("summary", "")
    return None, None, [], [], ""


def request_review(target_id, buyer_org, buyer_email="", intended_use="") -> dict:
    kind, name, _, _, _ = _target(target_id)
    if not kind:
        return {"error": "target not found"}
    c = {
        "id": "ctr_" + uuid.uuid4().hex[:10],
        "target_id": target_id, "target_kind": kind, "target_name": name,
        "buyer_org": buyer_org, "buyer_email": buyer_email, "intended_use": intended_use,
        "status": "requested", "created": time.time(),
        "agreement_id": None, "review_token": None, "expires_at": None,
        "access_count": 0, "audit": [{"ts": time.time(), "event": "requested", "detail": buyer_org}],
    }
    allo = contracts()
    allo.insert(0, c)
    save_contracts(allo)
    return c


def grant_review(cid, agreement_id="", term_days=30) -> dict:
    """Grant a review request under a contract. Issues a scoped, expiring review
    token. Requires an agreement/NDA id (the contract the buyer signed)."""
    allo = contracts()
    for c in allo:
        if c["id"] == cid:
            c["status"] = "active"
            c["agreement_id"] = agreement_id or ("NDA-" + uuid.uuid4().hex[:6].upper())
            c["review_token"] = uuid.uuid4().hex
            c["granted_at"] = time.time()
            c["expires_at"] = time.time() + term_days * 86400
            c["audit"].append({"ts": time.time(), "event": "granted",
                               "detail": f"agreement {c['agreement_id']} · {term_days}d"})
            save_contracts(allo)
            return c
    return {"error": "not found"}


def _summary_stats(sample, schema):
    """Compute lightweight column stats for the review room (counts / numeric ranges)."""
    stats = []
    for col in schema:
        name, typ = col["name"], col.get("type")
        vals = [r.get(name) for r in sample if name in r]
        st = {"name": name, "type": typ, "non_null": sum(1 for v in vals if v not in (None, ""))}
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            st["min"], st["max"] = min(nums), max(nums)
            st["avg"] = round(sum(nums) / len(nums), 2)
        else:
            st["distinct"] = len(set(str(v) for v in vals))
        stats.append(st)
    return stats


def review_room(token, actor="buyer") -> dict:
    """Buyer-facing: open a review room with a granted token. Returns schema, a
    de-identified sample, and summary stats only — never the full dataset."""
    allo = contracts()
    c = next((x for x in allo if x.get("review_token") == token), None)
    if not c:
        return {"error": "invalid review token"}
    if c.get("status") != "active":
        return {"error": f"review is {c.get('status')}"}
    if time.time() > (c.get("expires_at") or 0):
        c["status"] = "expired"
        save_contracts(allo)
        return {"error": "review access expired"}
    kind, name, sample, schema, summary = _target(c["target_id"])
    sample = (sample or [])[:5]
    c["access_count"] = c.get("access_count", 0) + 1
    c["audit"].append({"ts": time.time(), "event": "reviewed", "detail": f"opened by {actor}"})
    save_contracts(allo)
    return {"contract_id": c["id"], "agreement_id": c["agreement_id"], "target_name": name,
            "summary": summary, "schema": schema, "sample": sample,
            "stats": _summary_stats(sample, schema), "expires_at": c["expires_at"],
            "access_count": c["access_count"],
            "watermark": f"CONFIDENTIAL · {c['buyer_org']} · under {c['agreement_id']}"}


def revoke_review(cid) -> dict:
    allo = contracts()
    for c in allo:
        if c["id"] == cid:
            c["status"] = "revoked"
            c["audit"].append({"ts": time.time(), "event": "revoked", "detail": "access revoked"})
            save_contracts(allo)
            return c
    return {"error": "not found"}


def pipeline_overview() -> dict:
    a = assets()
    by_stage = {s: sum(1 for x in a if x.get("stage") == s) for s in STAGES}
    return {"mode": dvt.status()["mode"], "assets": len(a), "by_stage": by_stage,
            "published": by_stage.get("published", 0),
            "open_reviews": sum(1 for c in contracts() if c.get("status") == "active")}
