"""Datavant pipeline REST API — connect, tokenize, de-identify, certify, publish,
and contracted review. Thin layer over datavant_pipeline / datavant_client.

Routes (prefix /api/datavant):
  GET  /status                      Datavant connectivity + mode (sandbox/live)
  GET  /overview                    pipeline stage counts
  GET  /assets · GET /assets/{id}   data assets in the pipeline
  POST /assets                      register a new asset
  POST /assets/{id}/tokenize        master/site/transit tokens
  POST /assets/{id}/deidentify      Safe Harbor / Expert Determination
  POST /assets/{id}/certify         issue certification
  POST /assets/{id}/publish         publish certified asset to the marketplace
  GET  /contracts · POST /contracts/request     contracted review requests
  POST /contracts/{id}/grant · /revoke
  GET  /review/{token}              open a review room (schema + sample + stats)
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

import datavant_client as dvt
import datavant_pipeline as pipe

router = APIRouter(prefix="/api/datavant", tags=["datavant"])


@router.get("/status")
async def status():
    return dvt.status()


@router.get("/overview")
async def overview():
    return pipe.pipeline_overview()


@router.get("/assets")
async def list_assets():
    return {"assets": pipe.assets(), "stages": pipe.STAGES}


@router.get("/assets/{aid}")
async def get_asset(aid: str):
    a = pipe.asset(aid)
    return {"asset": a} if a else {"error": "not found"}


class AssetBody(BaseModel):
    name: str
    source: str = ""
    category: str = "Other"
    summary: str = ""
    sample: Optional[list] = None
    pii_fields: Optional[list] = None
    pricing: Optional[dict] = None
    domain: str = "healthcare"


@router.post("/assets")
async def register_asset(body: AssetBody):
    return {"ok": True, "asset": pipe.register_asset(
        body.name, body.source, body.category, body.summary,
        body.sample, body.pii_fields, body.pricing, body.domain)}


class TokenizeBody(BaseModel):
    recipient: str = ""


@router.post("/assets/{aid}/tokenize")
async def tokenize(aid: str, body: TokenizeBody):
    return pipe.run_tokenize(aid, recipient=body.recipient)


class DeidBody(BaseModel):
    method: str = "safe_harbor"   # safe_harbor | expert_determination


@router.post("/assets/{aid}/deidentify")
async def deidentify(aid: str, body: DeidBody):
    return pipe.run_deidentify(aid, method=body.method)


class CertifyBody(BaseModel):
    expert: str = ""


@router.post("/assets/{aid}/certify")
async def certify(aid: str, body: CertifyBody):
    return pipe.run_certify(aid, expert=body.expert)


@router.post("/assets/{aid}/publish")
async def publish(aid: str):
    return pipe.publish_to_marketplace(aid)


# ── Contracted review ──────────────────────────────────────────────────────
@router.get("/contracts")
async def list_contracts():
    return {"contracts": pipe.contracts()}


class ReviewRequestBody(BaseModel):
    target_id: str
    buyer_org: str
    buyer_email: str = ""
    intended_use: str = ""


@router.post("/contracts/request")
async def request_review(body: ReviewRequestBody):
    return pipe.request_review(body.target_id, body.buyer_org, body.buyer_email, body.intended_use)


class GrantBody(BaseModel):
    agreement_id: str = ""
    term_days: int = 30


@router.post("/contracts/{cid}/grant")
async def grant_review(cid: str, body: GrantBody):
    return pipe.grant_review(cid, agreement_id=body.agreement_id, term_days=body.term_days)


@router.post("/contracts/{cid}/revoke")
async def revoke_review(cid: str):
    return pipe.revoke_review(cid)


@router.get("/review/{token}")
async def review_room(token: str):
    return pipe.review_room(token, actor="buyer")
