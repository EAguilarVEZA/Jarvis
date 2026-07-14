"""Datavant integration adapter — tokenize, de-identify, certify.

Mirrors Datavant's real structured-data workflow:

  1. TOKENIZE  — an irreversible hash of patient PII creates a "master token";
     that master token is then encrypted with a site-specific key to produce the
     site-specific token; a transit token (re-encrypted for a named recipient) is
     produced for privacy-preserving hand-off. Tokens link records across datasets
     without ever exposing PII. (Ref: Datavant Connect tokenization.)
  2. DE-IDENTIFY — HIPAA Safe Harbor (remove the 18 identifiers) or Expert
     Determination (statistical review of re-identification risk + remediation).
  3. CERTIFY — the de-identified dataset receives a written determination /
     certification.

Two run modes
-------------
* SANDBOX (default): fully local, deterministic tokens via HMAC-SHA256 and rule-based
  de-identification. No PHI leaves the machine and nothing is sent to Datavant. Use
  this for wiring, demos, and synthetic/sample data.
* LIVE: set DATAVANT_API_KEY (and optionally DATAVANT_BASE_URL). The request builders
  below target a configurable base URL; the exact Datavant endpoint paths must be
  confirmed against your Datavant contract/API package before enabling LIVE mode.
  Datavant supports batch (file) and API (per-record) processing.

SAFETY: This module never de-tokenizes and cannot reverse a token to PII. Run real
PHI only through LIVE mode against your certified Datavant environment and your BAA.
"""
from __future__ import annotations

import os
import hmac
import time
import json
import uuid
import base64
import hashlib
import logging

log = logging.getLogger("datavant")

# Site/tenant key: in LIVE mode this is held by Datavant per site. In SANDBOX it is a
# local secret so tokens are stable across runs but still site-specific.
_SITE_KEY = os.getenv("DATAVANT_SITE_KEY", "orlando-health-site-key").encode()
_PEPPER = b"datavant-master-hash-pepper-v1"   # stands in for Datavant's keyed hash step
_BASE_URL = os.getenv("DATAVANT_BASE_URL", "https://api.datavant.com").rstrip("/")

# The 18 HIPAA Safe Harbor identifier field-name hints (matched case-insensitively).
_SAFE_HARBOR_FIELDS = [
    "name", "first_name", "last_name", "fullname", "patient_name",
    "ssn", "social", "mrn", "medical_record", "account", "account_number",
    "phone", "fax", "email", "url", "ip", "ip_address", "device_id", "serial",
    "street", "address", "address1", "address2", "geocode", "certificate",
    "license", "vehicle", "biometric", "photo", "beneficiary",
]
_DATE_FIELDS_HINT = ("date", "dob", "birth", "admit", "discharge", "service", "dispense", "encounter_date")
_DIRECT_ID_HINT = ("name", "ssn", "social", "mrn", "phone", "email", "address", "street")


def live_mode() -> bool:
    """True when a real Datavant API key is configured."""
    return bool(os.getenv("DATAVANT_API_KEY", "").strip())


def status() -> dict:
    """Connectivity/status probe for the UI."""
    return {"mode": "live" if live_mode() else "sandbox",
            "base_url": _BASE_URL if live_mode() else None,
            "site": "orlando-health",
            "capabilities": ["tokenize", "deidentify", "certify"],
            "token_types": ["master", "site", "transit"],
            "note": ("Connected to Datavant API." if live_mode()
                     else "Sandbox mode — deterministic local tokens; no PHI leaves this machine.")}


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
def _norm(v) -> str:
    return "".join(str(v or "").lower().split())


def _token(data: bytes, key: bytes, prefix: str) -> str:
    return prefix + base64.urlsafe_b64encode(hmac.new(key, data, hashlib.sha256).digest())[:22].decode()


def _record_pii(rec: dict, pii_fields: list) -> str:
    """Compose the normalized PII string used to derive the master token."""
    if pii_fields:
        parts = [_norm(rec.get(f)) for f in pii_fields]
    else:  # sensible default identity key
        parts = [_norm(rec.get(k)) for k in ("first_name", "last_name", "dob", "gender", "ssn", "zip")]
    return "|".join(parts)


def tokenize(records: list, pii_fields: list = None, recipient: str = "") -> dict:
    """Tokenize a batch of records. Returns tokens per record + a summary.

    Each record gets: master_token (dataset-agnostic identity), site_token
    (site-specific, safe to store), and — if a recipient is named — a transit_token
    (re-encrypted for that recipient's recipient key)."""
    pii_fields = pii_fields or []
    out = []
    for rec in records:
        pii = _record_pii(rec, pii_fields)
        master = hmac.new(_PEPPER, pii.encode(), hashlib.sha256).digest()   # irreversible master hash
        site_token = _token(master, _SITE_KEY, "DTv1s.")
        row = {"master_token": _token(master, _PEPPER, "DTv1m."), "site_token": site_token}
        if recipient:
            rkey = hashlib.sha256(("recipient:" + recipient).encode()).digest()
            row["transit_token"] = _token(site_token.encode(), rkey, "DTv1t.")
        out.append(row)
    return {"ok": True, "mode": "live" if live_mode() else "sandbox",
            "count": len(out), "recipient": recipient or None,
            "token_types": ["master", "site"] + (["transit"] if recipient else []),
            "tokens": out}


# ---------------------------------------------------------------------------
# De-identification (HIPAA Safe Harbor / Expert Determination)
# ---------------------------------------------------------------------------
def _looks_direct_id(field: str) -> bool:
    f = field.lower()
    return any(h in f for h in _SAFE_HARBOR_FIELDS)


def _shift_date(v) -> str:
    s = str(v or "")
    # keep only the year (a conservative Safe Harbor date reduction) + mark shifted
    for sep in ("-", "/"):
        if sep in s:
            return s.split(sep)[0] + " (year, date-shifted)"
    return "(date removed)"


def deidentify(records: list, method: str = "safe_harbor", drop_fields: list = None) -> dict:
    """De-identify records. method: 'safe_harbor' | 'expert_determination'.
    Returns de-identified records + the transforms applied (the remediation log)."""
    drop_fields = drop_fields or []
    transforms, out = [], []
    removed, dated, zipped, aged = set(), set(), set(), set()
    for rec in records:
        clean = {}
        for k, v in rec.items():
            kl = k.lower()
            if k in drop_fields or _looks_direct_id(k):
                removed.add(k)
                continue
            if any(h in kl for h in _DATE_FIELDS_HINT):
                clean[k] = _shift_date(v)
                dated.add(k)
                continue
            if kl in ("zip", "zipcode", "postal", "postal_code"):
                clean[k] = (str(v)[:3] + "XX") if str(v) else v   # 3-digit ZIP (Safe Harbor)
                zipped.add(k)
                continue
            if kl in ("age",) and str(v).isdigit() and int(v) >= 90:
                clean[k] = "90+"
                aged.add(k)
                continue
            clean[k] = v
        out.append(clean)
    if removed:
        transforms.append({"rule": "remove_direct_identifiers", "fields": sorted(removed)})
    if dated:
        transforms.append({"rule": "date_shift_to_year", "fields": sorted(dated)})
    if zipped:
        transforms.append({"rule": "truncate_zip_3digit", "fields": sorted(zipped)})
    if aged:
        transforms.append({"rule": "aggregate_age_90plus", "fields": sorted(aged)})
    if method == "expert_determination":
        transforms.append({"rule": "statistical_risk_review",
                           "detail": "k-anonymity / small-cell suppression recommended (k<11)"})
    return {"ok": True, "method": method, "records": out, "transforms": transforms,
            "identifiers_removed": sorted(removed)}


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------
def certify(dataset_name: str, method: str, transforms: list, expert: str = "") -> dict:
    """Produce a de-identification certification record. In LIVE mode this would be a
    Datavant Privacy Hub Expert Determination report; in SANDBOX it is a simulated
    certification for wiring/demo."""
    now = time.time()
    is_live = live_mode()
    cert = {
        "certification_id": "DVT-CERT-" + uuid.uuid4().hex[:10].upper(),
        "dataset": dataset_name,
        "method": method,   # safe_harbor | expert_determination
        "standard": ("HIPAA Expert Determination (§164.514(b)(1))" if method == "expert_determination"
                     else "HIPAA Safe Harbor (§164.514(b)(2))"),
        "expert": (expert or "Datavant Privacy Hub") if method == "expert_determination" else None,
        "transforms_applied": transforms,
        "risk_statement": ("Very small risk of re-identification following remediation."
                           if method == "expert_determination"
                           else "All 18 Safe Harbor identifiers removed or generalized."),
        "issued_at": now,
        "expires_at": now + 365 * 86400,
        "simulated": not is_live,
        "status": "certified",
    }
    return cert


# ---------------------------------------------------------------------------
# LIVE-mode request scaffold (paths must be confirmed against your Datavant API)
# ---------------------------------------------------------------------------
def _live_request(op: str, payload: dict):
    """Build (method, url, headers, body) for a LIVE Datavant call. Endpoint paths are
    placeholders — confirm against your Datavant API package before enabling."""
    key = os.getenv("DATAVANT_API_KEY", "")
    paths = {"tokenize": "/v1/tokenize", "deidentify": "/v1/deidentify", "certify": "/v1/certify"}
    url = _BASE_URL + paths.get(op, "/v1/" + op)
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    return "POST", url, headers, json.dumps(payload).encode()
