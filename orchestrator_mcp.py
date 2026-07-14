"""Martin orchestration MCP server — expose the supervisor brain as MCP tools so
external AI clients (Claude Desktop, ChatGPT, other agents) can inspect Martin's
specialist registry and see how it would plan a goal across agents. This is the
agent-to-agent (A2A) surface.

SAFE BY DESIGN: this server is read/plan-only. It can list agents, plan a goal, and
read run status/traces — but it does NOT start or advance runs. Execution stays
inside the app behind the Compliance gate + human approval.

Run (stdio): python orchestrator_mcp.py
Env: ORCH_API_BASE (default https://localhost:8340), ORCH_API_USER (X-Jarvis-User).
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

_BASE = os.getenv("ORCH_API_BASE", "https://localhost:8340").rstrip("/")
_USER = os.getenv("ORCH_API_USER", "edgar@smartwithmartin.com")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

mcp = FastMCP("smart-with-martin-orchestrator")


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
def list_agents() -> dict:
    """List Martin's specialist agents (Analyst, Data Engineer, Marketer, Compliance,
    Ops, Researcher) with their skills, tools, and data scope."""
    return _call("GET", "/api/orchestrator/agents")


@mcp.tool()
def plan_goal(goal: str) -> dict:
    """Plan a goal into an ordered multi-agent workflow WITHOUT executing it. Returns
    the steps (agent + task), with a Compliance gate before any activation. Read-only."""
    return _call("POST", "/api/orchestrator/plan", {"goal": goal})


@mcp.tool()
def list_runs() -> dict:
    """List recent orchestration runs and their status."""
    return _call("GET", "/api/orchestrator/runs")


@mcp.tool()
def get_run(run_id: str) -> dict:
    """Inspect one run: plan, per-step status, shared blackboard, and the full audit trace."""
    return _call("GET", f"/api/orchestrator/run/{run_id}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
