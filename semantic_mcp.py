"""Metrics MCP server — expose the governed semantic layer (metrics + topics) as MCP
tools, so any AI client (Claude Desktop, ChatGPT, Cursor, your Studio agents) can
query trusted metrics instead of writing raw SQL.

This is the "headless analytics via MCP" pattern (Tableau's open MCP architecture,
dbt's MCP server). The tools mirror the dbt Semantic Layer toolset:
  list_metrics · list_topics · get_metric · query_metric · ask_metrics

The tools call the running Smart-With-Martin server's governed API over HTTP, so
row-level security, the metric compiler, and BigQuery execution all apply exactly as
in the app — the agent never touches SQL or the warehouse directly.

Run (stdio, for a local MCP client):   python semantic_mcp.py
Env: METRICS_API_BASE (default https://localhost:8340), METRICS_API_USER (X-Jarvis-User).
"""
from __future__ import annotations

import os
import ssl
import json
import sys
import urllib.request

try:
    from mcp.server.fastmcp import FastMCP
except Exception:
    sys.stderr.write("The 'mcp' Python SDK is required: pip install mcp\n")
    raise

_BASE = os.getenv("METRICS_API_BASE", "https://localhost:8340").rstrip("/")
_USER = os.getenv("METRICS_API_USER", "edgar@smartwithmartin.com")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE   # self-signed localhost cert

mcp = FastMCP("smart-with-martin-metrics")


def _call(method: str, path: str, body: dict = None) -> dict:
    url = _BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Jarvis-User": _USER})
    try:
        with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{e}"}


@mcp.tool()
def list_metrics() -> dict:
    """List every governed metric in the semantic layer (name, aggregation, measure,
    time grain). Use this to discover the vocabulary before querying."""
    d = _call("GET", "/api/semantic/metric_lab")
    metrics = d.get("metrics", []) if isinstance(d, dict) else []
    return {"metrics": [{"id": m.get("id"), "name": m.get("name"), "description": m.get("description", ""),
                         "aggregation": m.get("aggregation"), "measure": m.get("measure_field"),
                         "default_grain": m.get("default_grain")} for m in metrics]}


@mcp.tool()
def list_topics() -> dict:
    """List subject areas (topics): a base table + its joins + a curated set of metrics
    and dimensions. Use a topic to scope which metrics/dimensions are relevant."""
    d = _call("GET", "/api/semantic/topics")
    return {"topics": d.get("topics", []) if isinstance(d, dict) else []}


@mcp.tool()
def get_metric(metric_id: str) -> dict:
    """Full definition of one governed metric by id."""
    d = _call("GET", "/api/semantic/metric_lab")
    for m in (d.get("metrics", []) if isinstance(d, dict) else []):
        if m.get("id") == metric_id:
            return {"metric": m}
    return {"error": "metric not found"}


@mcp.tool()
def query_metric(metric_id: str, grain: str = "", breakdown: str = "", filters: list = None) -> dict:
    """Run a governed metric and return its time series + latest value + delta. Provide
    a metric_id (from list_metrics), an optional grain (day/week/month/quarter/year),
    an optional breakdown dimension key, and optional extra filters
    ([{field, op, value}]). The metric compiler + row-level security are applied
    server-side — you never write SQL."""
    body = {"id": metric_id}
    if grain:
        body["grain"] = grain
    if breakdown:
        body["breakdown"] = breakdown
    if filters:
        body["extra_filters"] = filters
    return _call("POST", "/api/semantic/metric_lab/preview", body)


@mcp.tool()
def ask_metrics(question: str) -> dict:
    """Answer a natural-language analytics question by grounding on the governed
    metrics: the server selects the right metric + grain + breakdown + filters and runs
    it deterministically. Returns a plain-language answer + the metric used + the SQL."""
    return _call("POST", "/api/semantic/metric_ask", {"question": question})


if __name__ == "__main__":
    mcp.run(transport="stdio")
