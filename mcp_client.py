"""Lightweight MCP (Model Context Protocol) client for JARVIS.

Lets Agent Studio call tools on external MCP servers — the same connectors the
user has elsewhere (Gmail, Google Sheets, Drive, Slack, …) — without JARVIS
having to re-implement each API or its OAuth. A server is configured once
(stdio command or a remote SSE / streamable-HTTP URL); nodes then introspect
its tools and call them.

The `mcp` Python SDK is imported lazily so the rest of JARVIS runs fine when it
isn't installed — callers get a clear, actionable error instead of an ImportError
at startup. Install with:  pip install "mcp>=1.0"
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger("mcp_client")

_DEFAULT_TIMEOUT = 45


class MCPUnavailable(RuntimeError):
    """Raised when the MCP SDK is not installed."""


def _require_sdk():
    try:
        import mcp  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover - env dependent
        raise MCPUnavailable(
            "The MCP client library isn't installed. Run: pip install \"mcp>=1.0\""
        ) from e


@contextlib.asynccontextmanager
async def _open_session(cfg: dict):
    """Open an initialized ClientSession for a server config across transports.

    cfg = {transport: 'stdio'|'sse'|'http', command, args[], env{}, url, headers{}}
    """
    _require_sdk()
    from mcp import ClientSession
    transport = (cfg.get("transport") or ("stdio" if cfg.get("command") else "sse")).lower()

    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(
            command=cfg.get("command") or "",
            args=list(cfg.get("args") or []),
            env=(cfg.get("env") or None),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    url = cfg.get("url") or ""
    headers = cfg.get("headers") or None
    if transport in ("http", "streamable-http", "streamable_http"):
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(url, headers=headers) as (read, write, _extra):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    # default: SSE
    from mcp.client.sse import sse_client
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _text_of(result) -> str:
    """Flatten a CallToolResult / content list into text."""
    parts = []
    content = getattr(result, "content", None)
    if content is None and isinstance(result, (list, tuple)):
        content = result
    for block in (content or []):
        t = getattr(block, "text", None)
        if t is not None:
            parts.append(t)
        elif getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
        else:
            # resource / image / other — describe it briefly
            parts.append(str(getattr(block, "type", "content")))
    return "\n".join(p for p in parts if p).strip()


async def list_tools(cfg: dict, timeout: int = _DEFAULT_TIMEOUT) -> list:
    """Return [{name, description, input_schema}] for a server."""
    async def _run():
        async with _open_session(cfg) as session:
            res = await session.list_tools()
            out = []
            for t in getattr(res, "tools", []) or []:
                out.append({
                    "name": getattr(t, "name", ""),
                    "description": getattr(t, "description", "") or "",
                    "input_schema": getattr(t, "inputSchema", None) or {},
                })
            return out
    return await asyncio.wait_for(_run(), timeout=timeout)


async def call_tool(cfg: dict, tool: str, arguments: dict, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Call one tool; returns {output} or {error}."""
    async def _run():
        async with _open_session(cfg) as session:
            res = await session.call_tool(tool, arguments=arguments or {})
            if getattr(res, "isError", False):
                return {"error": _text_of(res) or "The MCP tool reported an error."}
            return {"output": _text_of(res)}
    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except MCPUnavailable as e:
        return {"error": str(e)}
    except asyncio.TimeoutError:
        return {"error": f"MCP call timed out after {timeout}s."}
    except Exception as e:
        return {"error": f"MCP error: {e}"}


async def probe(cfg: dict, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Connect and list tools; returns {ok, tools} or {error}."""
    try:
        tools = await list_tools(cfg, timeout=timeout)
        return {"ok": True, "tools": tools}
    except MCPUnavailable as e:
        return {"error": str(e), "sdk_missing": True}
    except asyncio.TimeoutError:
        return {"error": f"Connection timed out after {timeout}s."}
    except Exception as e:
        return {"error": f"Could not connect: {e}"}
