"""Data Marketplace — REST API for the human UI.

Thin FastAPI layer over `marketplace_core` (all logic lives there, shared with the
MCP server so agents and humans hit one identical backend).

Routes (prefix /api/marketplace):
  GET  /products / /products/{pid} / POST /products / DELETE /products/{pid}
  GET  /categories · POST /search · POST /bundle/price · POST /bundle/recommend
  GET  /orders · POST /orders · GET /orders/{oid}
  POST /orders/{oid}/pay · POST /orders/{oid}/fulfill · GET /stats
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

import marketplace_core as core

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@router.get("/products")
async def list_products(category: str = "", q: str = "", domain: str = ""):
    items = core.list_products(category=category, q=q, domain=domain)
    return {"products": items, "count": len(items)}


@router.get("/categories")
async def categories():
    return {"categories": core.categories()}


@router.get("/products/{pid}")
async def get_product(pid: str):
    p = core.product(pid)
    if not p:
        return {"error": "not found"}
    return {"product": p, "price_estimate": core.price_product(p)}


class ProductBody(BaseModel):
    id: Optional[str] = None
    name: str
    category: str = "Other"
    domain: str = "healthcare"
    summary: str = ""
    description: str = ""
    compliance: str = ""
    refresh: str = "Monthly"
    row_count: int = 0
    fields: Optional[list] = None
    sample: Optional[list] = None
    pricing: Optional[dict] = None
    tags: Optional[list] = None
    status: str = "active"


@router.post("/products")
async def upsert_product(body: ProductBody):
    return {"ok": True, "product": core.upsert_product(body.dict())}


@router.delete("/products/{pid}")
async def retire_product(pid: str):
    core.retire_product(pid)
    return {"ok": True}


class SearchBody(BaseModel):
    query: str


@router.post("/search")
async def search(body: SearchBody):
    return await core.search_catalog(body.query)


class BundlePriceBody(BaseModel):
    product_ids: List[str]
    volume: int = 0
    months: int = 12


@router.post("/bundle/price")
async def bundle_price(body: BundlePriceBody):
    prods = [p for p in (core.product(i) for i in body.product_ids) if p]
    if not prods:
        return {"error": "no valid products"}
    return {"bundle": core.price_bundle(prods, volume=body.volume, months=body.months),
            "products": [{"id": p["id"], "name": p["name"]} for p in prods]}


class RecommendBody(BaseModel):
    use_case: str


@router.post("/bundle/recommend")
async def bundle_recommend(body: RecommendBody):
    return await core.recommend_bundle(body.use_case)


class OrderBody(BaseModel):
    product_ids: List[str]
    buyer_org: str
    buyer_email: str = ""
    use_case: str = ""
    volume: int = 0
    months: int = 12


@router.post("/orders")
async def create_order(body: OrderBody):
    o = core.create_order(body.product_ids, body.buyer_org, body.buyer_email,
                          body.use_case, body.volume, body.months)
    if o.get("error"):
        return o
    return {"ok": True, "order": o}


@router.get("/orders")
async def list_orders():
    return {"orders": core.orders()}


@router.get("/orders/{oid}")
async def get_order(oid: str):
    o = core.order(oid)
    return {"order": o} if o else {"error": "not found"}


class PayBody(BaseModel):
    payment_ref: str  # external reference from YOUR billing provider — no funds moved here


@router.post("/orders/{oid}/pay")
async def mark_paid(oid: str, body: PayBody):
    o = core.pay_order(oid, body.payment_ref)
    return o if o.get("error") else {"ok": True, "order": o}


@router.post("/orders/{oid}/fulfill")
async def fulfill(oid: str):
    o = await core.fulfill_order(oid)
    return o if o.get("error") else {"ok": True, "order": o}


@router.get("/stats")
async def stats():
    return core.stats()


# ── Phase 2: licensing, secure delivery, revenue attribution ──────────────
@router.get("/insights")
async def insights():
    return core.insights()


@router.get("/licenses")
async def list_licenses():
    return {"licenses": core.licenses()}


@router.get("/delivery/{token}")
async def get_delivery(token: str):
    """Buyer-facing secure delivery: token exchanges for the licensed output."""
    return core.access_delivery(token, actor="buyer")


@router.post("/licenses/{lid}/revoke")
async def revoke_license(lid: str):
    return core.revoke_license(lid)


class DownstreamBody(BaseModel):
    amount: float


@router.post("/orders/{oid}/downstream")
async def report_downstream(oid: str, body: DownstreamBody):
    return core.record_downstream_revenue(oid, body.amount)
