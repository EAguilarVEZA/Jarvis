"""
data_exchange_api — HTTP surface for the Martin Health Data Exchange demo (Vol 8 App.A / Vol 9 §11).

Demo endpoints over the synthetic dataset for a future marketplace UI + the conversational tools.
Production privacy/linkage/clean-room adapters report NOT_CONFIGURED until executed agreements exist.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

import data_exchange as DX

router = APIRouter(prefix="/v1/exchange", tags=["exchange"])


@router.get("/demo/inventory")
async def inventory(service_line: str | None = None):
    return DX.inventory(service_line)


class CohortBody(BaseModel):
    criteria: dict = {}


@router.post("/demo/cohorts")
async def cohorts(body: CohortBody):
    return DX.cohort(body.criteria)


class TierBody(BaseModel):
    spec: dict = {}


@router.post("/tier-eligibility")
async def tier_eligibility(body: TierBody):
    return DX.tier_eligibility(body.spec)


class QuoteBody(BaseModel):
    tier: str
    cohort_size: int = 0
    imaging: bool = False
    linkage: bool = False
    longitudinal_years: int = 0
    clean_room: bool = False


@router.post("/quotes/estimate")
async def quote_estimate(body: QuoteBody):
    return DX.estimate_quote(body.tier, cohort_size=body.cohort_size, imaging=body.imaging,
                             linkage=body.linkage, longitudinal_years=body.longitudinal_years,
                             clean_room=body.clean_room)


@router.get("/demo/tiers")
async def tiers():
    return {"label": DX.LABEL, "rate_card_version": DX.RATE_CARD_VERSION, "tiers": DX.TIERS}
