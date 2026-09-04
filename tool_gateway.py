"""
tool_gateway — one registry for every tool the runtime can call (Phase 2 P0).

Local real tools now (agent_tools + create_document); the MCP/connector bridge and the governed
Orlando Health tool register here later through the same interface. Deterministic receipts, and
**parallel execution** of independent calls (asyncio.gather) so a multi-tool turn — e.g. the flight
fan-out — does not cost N serial round-trips.

Receipt shape: {tool, request_id, output, provenance[], is_error, latency_ms}.
Tokens never enter prompts; connector auth (scopes.py) gates writes when those tools are added.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import agent_tools


class ToolGateway:
    def __init__(self):
        self._fns = {}          # name -> async fn(args) -> partial receipt {output, provenance, is_error}
        self._schemas = []      # anthropic/openai-neutral tool schemas

    def register(self, schema: dict, fn):
        name = schema["name"]
        self._schemas = [s for s in self._schemas if s["name"] != name]   # replace-by-name
        self._schemas.append(schema)
        self._fns[name] = fn

    def register_agent_tools(self):
        """Wire the real capabilities from agent_tools (web_search/web_fetch/weather/time/calc)."""
        for sch in agent_tools.TOOL_SCHEMAS:
            name = sch["name"]
            self.register(sch, (lambda n: (lambda args: agent_tools.run_tool(n, args)))(name))

    def schemas(self) -> list:
        return list(self._schemas)

    def has(self, name: str) -> bool:
        return name in self._fns

    async def invoke(self, name: str, args: dict, cancel=None) -> dict:
        t0 = time.time()
        rid = uuid.uuid4().hex
        fn = self._fns.get(name)
        if not fn:
            return {"tool": name, "request_id": rid, "output": f"unknown tool '{name}'",
                    "provenance": [], "is_error": True, "latency_ms": 0}
        try:
            rec = await fn(args or {})
            if not isinstance(rec, dict):
                rec = {"output": rec, "provenance": [], "is_error": False}
        except Exception as e:  # noqa
            rec = {"output": f"tool error: {e}", "provenance": [], "is_error": True}
        rec.setdefault("provenance", [])
        rec.setdefault("is_error", False)
        rec.update({"tool": name, "request_id": rid, "latency_ms": int((time.time() - t0) * 1000)})
        return rec

    async def invoke_many(self, calls: list) -> list:
        """calls: list[(name, args)]. Independent tools run concurrently."""
        if not calls:
            return []
        return await asyncio.gather(*[self.invoke(n, a) for (n, a) in calls])
