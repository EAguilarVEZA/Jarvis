"""Data Marketplace — MCP server.

Exposes the Orlando Health Data Marketplace as MCP tools so ANY AI agent — your own
Jarvis Studio agents (internal) or an outside buyer company's AI (external) — can
browse, price, and license data conversationally. It shares `marketplace_core` with
the REST API, so there is exactly one backend and one source of truth.

Transports
----------
  python marketplace_mcp.py               # stdio  (internal: Studio spawns it)
  python marketplace_mcp.py --http        # streamable-HTTP on :8765 (external agents)
  python marketplace_mcp.py --sse         # SSE     on :8765 (older MCP clients)
  python marketplace_mcp.py --http --port 9001

External scoping
----------------
Set MARKETPLACE_MCP_EXTERNAL=1 (or pass --external) to serve outside companies:
only PUBLISHED (active) products are visible, and admin/product-mutation tools are
never exposed here regardless. Money never moves — `pay_order` records an external
payment reference that your billing provider already settled, which unlocks
fulfillment (the agents "code the final output").

Security note: this server is READ + order-lifecycle only. It cannot create, edit,
or delete data products, change pricing, or touch anything outside the marketplace
store. Point external clients at the --http endpoint behind your own auth proxy.
"""
from __future__ import annotations

import os
import sys
import argparse
import logging

import marketplace_core as core

log = logging.getLogger("marketplace_mcp")

try:
    from mcp.server.fastmcp import FastMCP
except Exception as e:  # pragma: no cover - clear message if SDK missing
    sys.stderr.write("The 'mcp' Python SDK is required: pip install mcp\n")
    raise

_EXTERNAL = os.getenv("MARKETPLACE_MCP_EXTERNAL", "0") == "1"


def _external() -> bool:
    return _EXTERNAL


mcp = FastMCP("orlando-health-data-marketplace")


# ── Discovery ──────────────────────────────────────────────────────────────
@mcp.tool()
def browse_catalog(category: str = "", q: str = "", domain: str = "") -> dict:
    """List data products in the Orlando Health Data Marketplace.

    Optional filters: category (Clinical/Financial/Operational/Research), q (free
    text), domain. Returns each product's summary, compliance posture, row count,
    pricing model and tags."""
    items = core.list_products(category=category, q=q, domain=domain, published_only=_external())
    return {"products": items, "count": len(items)}


@mcp.tool()
def list_categories() -> dict:
    """Return the catalog categories with product counts."""
    return {"categories": core.categories()}


@mcp.tool()
def get_product(product_id: str) -> dict:
    """Get one data product in full: schema (field-by-field), a de-identified
    sample, compliance details, refresh cadence, and a price estimate."""
    p = core.product(product_id)
    if not p or (_external() and p.get("status") != "active"):
        return {"error": "not found"}
    return {"product": p, "price_estimate": core.price_product(p)}


@mcp.tool()
async def search_catalog(query: str) -> dict:
    """Conversational catalog search. Describe a need in plain English (e.g.
    'predict 30-day readmission for heart-failure patients') and get the matching
    data products plus a concierge recommendation on how to combine them."""
    return await core.search_catalog(query, published_only=_external())


# ── Pricing / bundling ─────────────────────────────────────────────────────
@mcp.tool()
def price_bundle(product_ids: list, volume: int = 0, months: int = 12) -> dict:
    """Price a bundle of data products together. Returns per-product line items, a
    multi-product discount, the total upfront license fee, any revenue-share terms,
    and a projected downstream value. volume = rows/queries wanted (0 = full)."""
    prods = [p for p in (core.product(i) for i in product_ids) if p
             and (not _external() or p.get("status") == "active")]
    if not prods:
        return {"error": "no valid products"}
    return {"bundle": core.price_bundle(prods, volume=volume, months=months),
            "products": [{"id": p["id"], "name": p["name"]} for p in prods]}


@mcp.tool()
async def recommend_bundle(use_case: str) -> dict:
    """Given a buyer use case, recommend which data products to combine into a
    sellable package, with a price and a rationale for why the combination is
    valuable."""
    return await core.recommend_bundle(use_case, published_only=_external())


# ── Order lifecycle: quote -> pay -> fulfill ───────────────────────────────
@mcp.tool()
def create_order(product_ids: list, buyer_org: str, buyer_email: str = "",
                 use_case: str = "", volume: int = 0, months: int = 12) -> dict:
    """Create a licensing order (status 'quote') for the given products. Records the
    buyer org, contact and use case, and returns the quote. Creating a quote does
    NOT charge anyone."""
    prods = [i for i in product_ids
             if core.product(i) and (not _external() or core.product(i).get("status") == "active")]
    if not prods:
        return {"error": "no valid products"}
    o = core.create_order(prods, buyer_org, buyer_email, use_case, volume, months)
    return o


@mcp.tool()
def get_order(order_id: str) -> dict:
    """Fetch an order by id, including its quote, status, and (once fulfilled) the
    licensed delivery spec."""
    o = core.order(order_id)
    return o or {"error": "not found"}


@mcp.tool()
def pay_order(order_id: str, payment_ref: str) -> dict:
    """Record that an EXTERNAL payment cleared for this order (payment_ref is the
    reference from your billing provider — this server never moves money). Moves the
    order to 'paid', which unlocks fulfillment."""
    return core.pay_order(order_id, payment_ref)


@mcp.tool()
async def fulfill_order(order_id: str) -> dict:
    """Fulfill a PAID order: the data-engineering agent 'codes the final output' —
    a licensed delivery spec with the assembly SQL, transforms, Safe-Harbor
    de-identification steps, and a data dictionary for the ordered dataset."""
    return await core.fulfill_order(order_id)


@mcp.tool()
def marketplace_stats() -> dict:
    """Summary stats: product count, orders, revenue booked, quoted pipeline, and
    projected downstream value."""
    return core.stats()


# ── Phase 2: licensed delivery + revenue attribution ───────────────────────
@mcp.tool()
def access_delivery(delivery_token: str) -> dict:
    """Retrieve the licensed output for a fulfilled order using its secure delivery
    token. Enforces license expiry/revocation and logs the access. This is how a
    buyer's agent collects the delivered dataset spec."""
    return core.access_delivery(delivery_token, actor="external-agent" if _external() else "internal-agent")


@mcp.tool()
def report_downstream_revenue(order_id: str, amount: float) -> dict:
    """For revenue-share licenses: report downstream revenue the buyer earned from
    the licensed data. Returns the running total and the share owed to Orlando
    Health."""
    return core.record_downstream_revenue(order_id, amount)


@mcp.tool()
def marketplace_insights() -> dict:
    """Revenue attribution: revenue by product, buyer and category, the order
    funnel, monthly trend, and revenue-share owed. Best kept to internal agents."""
    if _external():
        return {"error": "insights are not available to external clients"}
    return core.insights()


@mcp.tool()
def datavant_status() -> dict:
    """Datavant integration status: sandbox vs live mode, capabilities, token types."""
    import datavant_client as dvt
    return dvt.status()


@mcp.tool()
def pipeline_assets() -> dict:
    """List data assets in the Datavant pipeline with their stage
    (connected → tokenized → deidentified → certified → published)."""
    if _external():
        return {"error": "pipeline is internal-only"}
    import datavant_pipeline as pipe
    return {"assets": pipe.assets(), "overview": pipe.pipeline_overview()}


@mcp.tool()
def request_data_review(target_id: str, buyer_org: str, buyer_email: str = "",
                        intended_use: str = "") -> dict:
    """Request contracted review of a data product or asset. Returns a review request
    that an Orlando Health admin grants against a signed agreement; once granted you
    receive a review token to open the data room."""
    import datavant_pipeline as pipe
    return pipe.request_review(target_id, buyer_org, buyer_email, intended_use)


@mcp.tool()
def open_review_room(review_token: str) -> dict:
    """Open a contracted review room with a granted token. Returns the schema, a
    de-identified sample, and summary stats only — never the full dataset. Access is
    logged and watermarked to your organization + agreement."""
    import datavant_pipeline as pipe
    return pipe.review_room(review_token, actor="external-agent" if _external() else "internal-agent")


def main():
    global _EXTERNAL
    ap = argparse.ArgumentParser(description="Orlando Health Data Marketplace MCP server")
    ap.add_argument("--http", action="store_true", help="Serve streamable-HTTP (external agents)")
    ap.add_argument("--sse", action="store_true", help="Serve SSE (older MCP clients)")
    ap.add_argument("--external", action="store_true", help="External mode: published products only")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.external:
        _EXTERNAL = True
        os.environ["MARKETPLACE_MCP_EXTERNAL"] = "1"
    try:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    except Exception:
        pass
    if args.http:
        mcp.run(transport="streamable-http")
    elif args.sse:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
