"""
dashboard_engine
────────────────
AI-driven dashboard design. Given a plain-English description, Haiku plans a set
of widgets (each = a chart type + a structured query over approved ktx measures),
which the builder then renders and the user can save as a View.

    plan = await plan_dashboard("cardiology spend trend, top campaigns by CPC, CTR by device",
                                "2015-01-01", "2015-12-31")
    # → {"name": ..., "widgets": [{"title","chart_type","query"}, ...]}

Reuses brief_engine's catalog + validation so it can only reference real,
approved fields. Requires ANTHROPIC_API_KEY.
"""
from __future__ import annotations

from brief_engine import (
    _load_catalog, _validate_query, _catalog_text, _get_client, _extract_json,
    _is_date_field, HAIKU,
)

PLAN_SYS = """You are a BI dashboard designer for a healthcare marketing warehouse (Orlando Health).
Design a dashboard of 4-8 widgets that answers the user's request, using ONLY the exact table keys,
metric keys, and dimension keys from the CATALOG. Never invent keys.

Output ONLY JSON:
{
  "name": "short dashboard title",
  "widgets": [
    {
      "title": "widget title",
      "chart_type": "kpi" | "line" | "bar" | "table",
      "query": {
        "primary_table": "<table>",
        "metrics": [{"table":"<table>","field":"<metric>"}],
        "dimensions": [{"table":"<table>","field":"<dimension>"}],
        "order_by": [{"field":"<metric or dimension>","direction":"desc"}],
        "limit": 20
      }
    }
  ]
}
Rules:
- "kpi" widgets: 1 metric, NO dimensions (a single headline number).
- "line" widgets: exactly one date/time dimension + 1-2 metrics (a trend).
- "bar"/"table" widgets: one categorical dimension + 1-3 metrics.
- Lead with 2-4 KPI widgets, then trends and breakdowns.
- The system applies the date-range filter automatically — do not add date filters."""


async def plan_dashboard(description: str, start: str, end: str) -> dict:
    catalog, _defs = _load_catalog()
    client = _get_client()
    resp = await client.messages.create(
        model=HAIKU, max_tokens=1800, system=PLAN_SYS,
        messages=[{"role": "user",
                   "content": f"Date range: {start} to {end}.\n\nCATALOG:\n{_catalog_text(catalog)}\n\n"
                              f"DASHBOARD REQUEST: {description}"}],
    )
    plan = _extract_json(resp.content[0].text)
    if not isinstance(plan, dict):
        plan = {}

    widgets = []
    for w in (plan.get("widgets") or []):
        if not isinstance(w, dict):
            continue
        vq = None
        try:
            vq = _validate_query(w.get("query", {}), catalog)
        except Exception:
            vq = None
        if not vq:
            continue
        # bound to the date range
        pt = vq["primary_table"]
        df = (catalog.get(pt) or {}).get("date_field")
        if df and not any(_is_date_field(f.get("field", "")) for f in vq.get("filters", [])):
            vq.setdefault("filters", []).append({"table": pt, "field": df, "op": "between", "value": [start, end]})
        ctype = w.get("chart_type", "bar")
        if ctype not in ("kpi", "line", "bar", "table"):
            ctype = "bar"
        widgets.append({"title": w.get("title", "Untitled"), "chart_type": ctype, "query": vq})

    if not widgets:
        # fallback: a couple of headline KPIs
        widgets = [
            {"title": "Ad spend", "chart_type": "kpi",
             "query": {"primary_table": "ad_performance", "metrics": [{"table": "ad_performance", "field": "spend"}],
                       "dimensions": [], "filters": [{"table": "ad_performance", "field": "date", "op": "between", "value": [start, end]}], "order_by": [], "limit": 1}},
            {"title": "Spend over time", "chart_type": "line",
             "query": {"primary_table": "ad_performance", "metrics": [{"table": "ad_performance", "field": "spend"}],
                       "dimensions": [{"table": "ad_performance", "field": "date"}],
                       "filters": [{"table": "ad_performance", "field": "date", "op": "between", "value": [start, end]}],
                       "order_by": [{"field": "date", "direction": "asc"}], "limit": 400}},
        ]
    return {"name": plan.get("name", "New dashboard"), "widgets": widgets}
