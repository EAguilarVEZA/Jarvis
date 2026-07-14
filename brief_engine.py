"""
brief_engine
────────────
Ask-a-question → grounded AI brief.

Pipeline:
  1. PLAN  (Haiku)  — translate the question into 1-3 structured queries using
                      ONLY the approved ktx measures/dimensions catalog.
  2. RUN            — execute each query through the semantic layer, which routes
                      to ktx when JARVIS_SQL_ENGINE=ktx (approved, unit-corrected).
  3. WRITE (Sonnet) — turn the real results into a brief: headline, key findings,
                      narrative, auto-surfaced caveats, and cited sources.

Numbers in charts come straight from the query results (never the model), so the
visuals can't hallucinate. The model writes prose and selects what matters.

Public API:
    brief = await generate_brief("how is our ad spend trending?")

Requires ANTHROPIC_API_KEY in the environment (same key Jarvis already uses).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

# Heuristic: which dimension fields are time axes (so we can anchor to recent data).
_DATE_HINT = re.compile(r"(date|month|week|day|year|quarter)", re.I)
_RECENT_DAYS = 90  # briefs focus on the most recent N data points for a time series


def _is_date_field(field: str) -> bool:
    return bool(field and _DATE_HINT.search(field))

import anthropic
import yaml

from semantic import load_model, build_sql, run_query, StructuredQuery

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

_SEMANTIC_YAML = Path(__file__).parent / "semantic" / "semantic.yaml"

# Curated caveats surfaced automatically whenever a measure is used in a brief.
CAVEATS = {
    "roas": "Conversion value is sparsely tracked in this warehouse (~$485 lifetime vs ~$133M spend). "
            "ROAS is unreliable here — prefer CTR, CPC, CPA, and conversion volume.",
    "conversion_value": "Sparsely populated (mostly null). Treat totals as a floor, not actuals.",
    "conversions": "Counts micro-conversions (secondary events), not only primary conversions.",
}

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.AsyncAnthropic(api_key=key)
    return _client


# ─── Catalog + definitions (from semantic.yaml) ─────────────────────
def _load_catalog() -> tuple[dict, dict]:
    """Return (catalog, defs).
    catalog: {table_key: {"dimensions":[keys], "metrics":[keys]}}
    defs:    {(table_key, field_key): {"name":..., "desc":..., "formula":...}}
    """
    sm = yaml.safe_load(open(_SEMANTIC_YAML))
    catalog, defs = {}, {}
    for tk, t in (sm.get("tables") or {}).items():
        fields = t.get("fields") or {}
        dates = [d["key"] for d in (fields.get("dates") or [])]
        dims = [d["key"] for d in (fields.get("dimensions") or [])] + dates
        mets = []
        for m in (fields.get("metrics") or []):
            mets.append(m["key"])
            defs[(tk, m["key"])] = {"name": m.get("display_name", m["key"]),
                                    "desc": m.get("description", ""), "formula": None}
        date_field = dates[0] if dates else next((x for x in dims if _is_date_field(x)), None)
        catalog[tk] = {"display": t.get("display_name", tk), "dimensions": dims,
                       "metrics": mets, "date_field": date_field}
    # calculated metrics attach to their available_in tables
    for cname, c in (sm.get("calculated_metrics") or {}).items():
        for tk in (c.get("available_in") or []):
            if tk in catalog:
                catalog[tk]["metrics"].append(cname)
                defs[(tk, cname)] = {"name": c.get("display_name", cname),
                                     "desc": c.get("description", ""), "formula": c.get("formula")}
    # combined dimensions attach as dimensions
    for dname, c in (sm.get("combined_dimensions") or {}).items():
        for tk in (c.get("available_in") or []):
            if tk in catalog:
                catalog[tk]["dimensions"].append(dname)
    return catalog, defs


def _catalog_text(catalog: dict) -> str:
    lines = []
    for tk, info in catalog.items():
        lines.append(f"- {tk} ({info['display']}): "
                     f"metrics=[{', '.join(sorted(set(info['metrics'])))}]; "
                     f"dimensions=[{', '.join(sorted(set(info['dimensions'])))}]")
    return "\n".join(lines)


def _extract_json(txt: str) -> Any:
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(json)?", "", txt).rsplit("```", 1)[0]
    start = min([i for i in (txt.find("{"), txt.find("[")) if i != -1] or [0])
    return json.loads(txt[start:])


# ─── 1. PLAN (Haiku) ────────────────────────────────────────────────
PLAN_SYS = """You are a query planner for a healthcare marketing analytics warehouse (Orlando Health).
Translate the user's question into 1-3 structured queries that answer it, using ONLY the exact
table keys, metric keys, and dimension keys from the CATALOG. Never invent keys.

Output ONLY JSON of this shape:
{
  "queries": [
    {"primary_table": "<table>",
     "metrics": [{"table":"<table>","field":"<metric>"}],
     "dimensions": [{"table":"<table>","field":"<dimension>"}],
     "filters": [{"table":"<table>","field":"<dimension>","op":"between|eq|in|gte|lte","value": ...}],
     "order_by": [{"field":"<metric or dimension>","direction":"desc"}],
     "limit": 50}
  ],
  "chart": {"query_index": 0, "type": "line|bar|none", "x": "<dimension field>", "y": "<metric field>"}
}
Guidance: prefer a time series (dimension = a date field) when the question is about trends.
For trends, the system will automatically focus on the MOST RECENT period — do not analyze
years-old data. Use a small number of focused queries. Omit filters if none are needed. Keep limit <= 200."""


async def _plan(question: str, catalog: dict, start: str, end: str) -> dict:
    client = _get_client()
    resp = await client.messages.create(
        model=HAIKU, max_tokens=900,
        system=PLAN_SYS,
        messages=[{"role": "user",
                   "content": f"The analysis is restricted to the date range {start} to {end} "
                              f"(the system applies this filter automatically — do not add date filters yourself).\n\n"
                              f"CATALOG:\n{_catalog_text(catalog)}\n\nQUESTION: {question}"}],
    )
    return _extract_json(resp.content[0].text)


def _validate_query(q, catalog: dict) -> Optional[dict]:
    """Robust to the planner returning refs as dicts OR strings ('table.field' or 'field')."""
    if not isinstance(q, dict):
        return None
    pt = q.get("primary_table")
    if pt not in catalog:
        return None

    def norm(ref):
        if isinstance(ref, dict):
            return {"table": ref.get("table", pt), "field": ref.get("field")}
        if isinstance(ref, str):
            if "." in ref:
                t, f = ref.split(".", 1)
                return {"table": t, "field": f}
            return {"table": pt, "field": ref}
        return None

    def ok(ref, kind):
        return ref and ref.get("table") in catalog and ref.get("field") in catalog[ref["table"]][kind]

    mets = [r for r in (norm(m) for m in (q.get("metrics") or [])) if ok(r, "metrics")]
    dims = [r for r in (norm(d) for d in (q.get("dimensions") or [])) if ok(r, "dimensions")]
    if not mets:
        return None

    filters = []
    for f in (q.get("filters") or []):
        if not isinstance(f, dict):
            continue
        fld, tbl = f.get("field"), f.get("table", pt)
        if isinstance(fld, str) and "." in fld and not f.get("table"):
            tbl, fld = fld.split(".", 1)
        if fld and f.get("op"):
            filters.append({"table": tbl, "field": fld, "op": f["op"], "value": f.get("value")})

    order = [o for o in (q.get("order_by") or []) if isinstance(o, dict) and o.get("field")]
    return {"primary_table": pt, "metrics": mets, "dimensions": dims,
            "filters": filters, "order_by": order,
            "limit": min(int(q.get("limit", 50) or 50), 200)}


# ─── 2. RUN (through ktx via the semantic layer) ────────────────────
def _run_one(qdict: dict, model) -> dict:
    sq = StructuredQuery.from_dict(qdict)
    sql = build_sql(sq, model)
    res = run_query(sql)
    return {"columns": res.columns, "rows": res.rows, "row_count": res.row_count,
            "sql": sql, "query": qdict}


# ─── 3. WRITE (Sonnet) ──────────────────────────────────────────────
WRITE_SYS = """You are a senior marketing analyst writing a brief for Orlando Health leadership.
You are given the user's QUESTION, the DATA returned by approved metric definitions, and DEFINITIONS
(with any CAVEATS) for the measures used. Write a trustworthy, decision-ready brief.

Rules:
- Use ONLY numbers present in the DATA. Never invent or extrapolate figures.
- If a measure has a caveat, you MUST include it in "caveats" and temper any claim that relies on it.
- Be concrete and specific; lead with what matters to leadership.

Output ONLY JSON:
{
 "headline": "one sharp sentence",
 "findings": ["3-5 bullet findings, each with a real number from the data"],
 "narrative": "1-2 short paragraphs of analysis and recommendation",
 "caveats": ["any data caveats that affect interpretation"],
 "sources": ["measure — source table (approved)", ...]
}"""


async def _write(question: str, runs: list[dict], defs: dict, start: str, end: str) -> dict:
    client = _get_client()
    # collect definitions + caveats for measures used
    used_defs, caveats_used, sources = [], [], []
    for r in runs:
        for m in r["query"].get("metrics", []):
            d = defs.get((m["table"], m["field"]), {})
            used_defs.append(f"{m['field']}: {d.get('name','')} — {d.get('desc') or d.get('formula') or 'approved measure'}")
            sources.append(f"{m['field']} — {m['table']} (approved)")
            if m["field"] in CAVEATS and CAVEATS[m["field"]] not in caveats_used:
                caveats_used.append(CAVEATS[m["field"]])
    # compact data payload (cap rows to keep tokens sane)
    data_payload = []
    for r in runs:
        data_payload.append({"columns": r["columns"], "rows": r["rows"][:40], "row_count": r["row_count"]})
    msg = (f"QUESTION: {question}\n"
           f"DATE RANGE: {start} to {end} (all figures are for this period only — say so).\n\n"
           f"DEFINITIONS:\n" + "\n".join(sorted(set(used_defs))) + "\n\n"
           f"CAVEATS (must include any that apply):\n" + ("\n".join(caveats_used) or "none") + "\n\n"
           f"DATA:\n{json.dumps(data_payload, default=str)}")
    resp = await client.messages.create(model=SONNET, max_tokens=1400, system=WRITE_SYS,
                                         messages=[{"role": "user", "content": msg}])
    brief = _extract_json(resp.content[0].text)
    # guarantee caveats present even if the model dropped them
    for c in caveats_used:
        if c not in (brief.get("caveats") or []):
            brief.setdefault("caveats", []).append(c)
    brief.setdefault("sources", sorted(set(sources)))
    return brief


def _build_chart(plan: dict, runs: list[dict]) -> Optional[dict]:
    ch = plan.get("chart") if isinstance(plan, dict) else None
    if not isinstance(ch, dict) or ch.get("type") in (None, "none") or not runs:
        return None
    qi = ch.get("query_index", 0) or 0
    if qi >= len(runs):
        qi = 0
    r = runs[qi]
    cols = r["columns"]
    x, y = ch.get("x"), ch.get("y")
    if x not in cols or y not in cols:
        return None
    xi, yi = cols.index(x), cols.index(y)
    labels = [row[xi] for row in r["rows"]]
    data = [row[yi] for row in r["rows"]]
    return {"type": ch["type"], "x": x, "y": y, "labels": labels,
            "datasets": [{"label": y, "data": data}]}


# ─── Orchestrator ───────────────────────────────────────────────────
async def generate_brief(question: str, start: str, end: str) -> dict:
    """Generate a brief for the question, bounded to the [start, end] date range.
    Every query is filtered to that window — the data has dirty dates (2014–2043),
    so an explicit range is required for results to make sense."""
    catalog, defs = _load_catalog()
    model = load_model()

    try:
        plan = await _plan(question, catalog, start, end)
    except Exception:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    raw_queries = plan.get("queries") if isinstance(plan.get("queries"), list) else []
    valid = []
    for q in raw_queries:
        try:
            v = _validate_query(q, catalog)
            if v:
                valid.append(v)
        except Exception:
            pass
    if not valid:
        valid = [{"primary_table": "ad_performance",
                  "metrics": [{"table": "ad_performance", "field": k} for k in ("spend", "clicks", "ctr", "cpc")],
                  "dimensions": [], "filters": [], "order_by": [], "limit": 1}]

    # Bound EVERY query to the requested date range, and keep time series chronological.
    for q in valid:
        pt = q["primary_table"]
        df = (catalog.get(pt) or {}).get("date_field")
        if df and not any(_is_date_field(f.get("field", "")) for f in q.get("filters", [])):
            q.setdefault("filters", []).append(
                {"table": pt, "field": df, "op": "between", "value": [start, end]})
        tf = next((d["field"] for d in q.get("dimensions", []) if _is_date_field(d["field"])), None)
        if tf:
            q["order_by"] = [{"field": tf, "direction": "asc"}]
            q["limit"] = min(q.get("limit") or 400, 400)

    runs = await asyncio.gather(*[asyncio.to_thread(_run_one, q, model) for q in valid])

    brief = await _write(question, runs, defs, start, end)
    chart = _build_chart(plan, runs)

    return {
        "question": question,
        "period": {"start": start, "end": end},
        "headline": brief.get("headline", ""),
        "findings": brief.get("findings", []),
        "narrative": brief.get("narrative", ""),
        "caveats": brief.get("caveats", []),
        "sources": brief.get("sources", []),
        "chart": chart,
        "data": [{"columns": r["columns"], "rows": r["rows"][:50], "sql": r["sql"]} for r in runs],
        "engine": os.environ.get("JARVIS_SQL_ENGINE", "bigquery"),
    }
