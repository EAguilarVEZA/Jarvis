"""Martin CDP MCP server — expose the governed CDP (profiles, segments, audiences,
activation SQL) as MCP tools so any AI client (Claude Desktop, ChatGPT, Cursor, and
Martin's own orchestrator agents) can build and reason about audiences without
touching raw data. Mirrors the pattern of semantic_mcp.py / marketplace_mcp.py.

Tools call the running Smart-With-Martin server over HTTP, so consent gating,
k-anonymity, and the PHI guard all apply exactly as in the app.

Run (stdio): python cdp_mcp.py
Env: CDP_API_BASE (default https://localhost:8340), CDP_API_USER (X-Jarvis-User).
"""
from __future__ import annotations

import os
import ssl
import sys
import json
import urllib.request

try:
    from mcp.server.fastmcp import FastMCP
except Exception:
    sys.stderr.write("The 'mcp' Python SDK is required: pip install mcp\n")
    raise

_BASE = os.getenv("CDP_API_BASE", "https://localhost:8340").rstrip("/")
_USER = os.getenv("CDP_API_USER", "edgar@smartwithmartin.com")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

mcp = FastMCP("smart-with-martin-cdp")


def _call(method: str, path: str, body: dict = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Jarvis-User": _USER})
    try:
        with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{e}"}


@mcp.tool()
def cdp_stats() -> dict:
    """CDP overview: profile count, marketing-consented count, event volume, revenue."""
    return _call("GET", "/api/cdp/stats")


@mcp.tool()
def list_segments() -> dict:
    """List saved audience segments (name + rule count)."""
    return _call("GET", "/api/cdp/segments")


@mcp.tool()
def preview_segment(rules: list, match: str = "all") -> dict:
    """Count how many people match a set of segment rules WITHOUT saving. Each rule is
    {field, op, value}; field is trait:<k> | metric:<k> | event:<Name> | consent:<marketing|analytics>.
    Returns the count + whether it clears the k-anonymity floor."""
    return _call("POST", "/api/cdp/segments/preview", {"rules": rules, "match": match})


@mcp.tool()
def list_destinations() -> dict:
    """List activation destinations (ads/CRM/ESP/warehouse/webhook) and their types."""
    return _call("GET", "/api/cdp/destinations")


@mcp.tool()
def warehouse_segment_sql(rules: list, match: str = "all") -> dict:
    """Compile a segment to governed BigQuery SQL that runs in the warehouse (PHI never
    leaves it). Returns the SQL + any rules that need an identify sub-model."""
    return _call("POST", "/api/cdp/warehouse/segment_sql", {"rules": rules, "match": match})


@mcp.tool()
def profile(person_id: str) -> dict:
    """Fetch one Customer 360 profile (traits + computed metrics + recent events)."""
    return _call("GET", f"/api/cdp/profiles/{person_id}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
