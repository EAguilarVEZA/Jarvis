"""
oh_tool — Orlando Health governed data tool for the ConversationRuntime (Phase 2 P1).

Exposes the EXISTING governed pipeline (semantic_api.ask → NL → structured query → BigQuery via the
curated semantic layer) as a single tool the voice/text engine can call. The LLM interprets intent and
narrates; it never writes raw SQL or invents metrics — the deterministic semantic layer does the query.
This is REAL: it hits the live warehouse through semantic_api (same path the app's Ask AI uses).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("oh_tool")

OH_TOOL_SCHEMA = {
    "name": "orlando_health_data",
    "description": (
        "Answer questions about Orlando Health / our marketing data using the GOVERNED BigQuery "
        "semantic layer: campaigns, ad spend, web & app traffic, sessions, referrals, appointments, "
        "leads, conversion, ROI, by service line (cardiology, oncology, etc.), trends and comparisons. "
        "Use this tool for ANY question about our/OH/Martin data, metrics, numbers, performance, or "
        "'the data warehouse'. Returns real rows plus an analyst brief. Never invent metrics — always "
        "call this tool for our data."),
    "input_schema": {"type": "object", "properties": {
        "question": {"type": "string", "description": "The business/data question in natural language"}},
        "required": ["question"]},
}


async def orlando_health_data(args: dict) -> dict:
    q = str((args or {}).get("question", "")).strip()
    if not q:
        return {"output": "empty question", "provenance": [], "is_error": True}
    try:
        import semantic_api                      # lazy import to avoid an import cycle
        # no_brief=True: skip the slow analyst-brief model call — the agent narrates rows itself.
        res = await semantic_api.ask(semantic_api.AskRequest(prompt=q, no_clarify=True, no_brief=True))
    except Exception as e:  # noqa
        log.warning("OH ask failed: %s", e)
        return {"output": f"data query failed: {e}", "provenance": [], "is_error": True}

    # semantic_api.ask returns a dict on success, or a JSONResponse (via _err) on failure
    if not isinstance(res, dict):
        try:
            body = json.loads(bytes(res.body).decode())
            return {"output": f"data unavailable: {body.get('error')} — {body.get('detail')}",
                    "provenance": [], "is_error": True}
        except Exception:
            return {"output": "data unavailable", "provenance": [], "is_error": True}
    if not res.get("ok"):
        return {"output": res.get("message") or res.get("error") or "no result",
                "provenance": [], "is_error": bool(res.get("error"))}

    cols = res.get("columns") or []
    rows = res.get("rows") or []
    brief = res.get("brief")
    widget = res.get("widget") or {}
    out = {
        "engine": "Orlando Health BigQuery (governed semantic layer)",
        "columns": cols,
        "rows": rows[:12],                       # compact preview for the model to narrate
        "row_count": res.get("row_count", len(rows)),
        "interpreted_query": res.get("interpreted"),
        "brief": brief if isinstance(brief, dict) else (str(brief) if brief else None),
    }
    # rich visualization: hand the client a chart spec so the answer renders as text + a chart
    ui_action = None
    if rows and cols:
        qd = widget.get("query") or {}
        ui_action = {"action": "chart", "title": q[:80],
                     "subtitle": f"{res.get('row_count', len(rows))} rows · Orlando Health (governed semantic layer)",
                     "chart_type": widget.get("type") or "bar",
                     "columns": cols, "rows": rows[:200],
                     "dimensions": qd.get("dimensions"), "metrics": qd.get("metrics"),
                     "metricFormats": widget.get("metricFormats")}
    rec = {"output": out,
           "provenance": [{"source": "orlando_health_bigquery",
                           "engine": res.get("engine", "curated semantic layer")}],
           "is_error": False}
    if ui_action:
        rec["ui_action"] = ui_action
    return rec
