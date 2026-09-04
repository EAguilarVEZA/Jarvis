"""
realtime_openai — foundation for the OpenAI Realtime (WebRTC) voice path (Phase 2 step 6).

This is the SERVER side of ChatGPT-Voice-style low latency:
  • POST /api/realtime/session  — mints a short-lived ephemeral OpenAI Realtime session (REAL call
    to OpenAI with the server-side key; the key NEVER goes to the browser) and returns the client
    secret + model + Martin's tool schemas so the browser can open a WebRTC peer directly to OpenAI.
  • POST /api/realtime/tool     — the browser bridges the model's function calls here; we execute
    them through the SAME canonical ToolGateway (web/weather/time/calc/create_document, and future
    OH/MCP tools) and return the receipt, so voice and text share one tool surface + one conversation.

Uses OpenAI's supported WebRTC path (no Martin-owned TURN required unless testing shows we need it).
The browser client (co_realtime.js) consumes these endpoints. Fish Audio remains an optional fallback
TTS and does NOT sit in this realtime critical path.
"""
from __future__ import annotations

import os
import json
import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from conversation_runtime import RUNTIME, PERSONA, _CREATE_DOC_SCHEMA

router = APIRouter(prefix="/api/realtime", tags=["realtime"])
log = logging.getLogger("realtime_openai")

_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")


# Client-fulfilled tools: the browser answers these (reads/acts on the on-screen Martin app),
# so the voice model can see the current screen and open items by name.
_CLIENT_TOOLS = [
    {"type": "function", "name": "read_screen",
     "description": ("Read what is currently visible on the user's Martin app screen (the on-screen "
                     "brief, a dashboard, a list of items, the midday email/meeting brief, etc.). Use "
                     "when the user asks 'what's on my screen', 'read this', 'what does it say', or to "
                     "see the available items before opening one."),
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "open_item",
     "description": ("Click an item by its visible name on the CURRENT screen (a specific brief, "
                     "dashboard, campaign, row, or link). Use after navigating to a section (open_view) "
                     "and reading it (read_screen) to open the one the user chooses."),
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


def _openai_tools() -> list:
    """Martin's canonical tool schemas in OpenAI Realtime function format (+ client-fulfilled)."""
    out = []
    for t in (RUNTIME.tools.schemas() + [_CREATE_DOC_SCHEMA]):
        out.append({"type": "function", "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}})})
    return out + _CLIENT_TOOLS


@router.get("/config")
async def config():
    """Is the realtime voice path available (key present) and what tools/persona will it use."""
    return {"available": bool(os.getenv("OPENAI_API_KEY")), "model": _REALTIME_MODEL,
            "voice": _VOICE, "tools": [t["name"] for t in _openai_tools()]}


@router.post("/session")
async def session():
    """Mint an ephemeral OpenAI Realtime client secret (REAL, current API: /v1/realtime/client_secrets).
    Returns the ephemeral token + tools + instructions for the browser; the browser then does the SDP
    handshake against /v1/realtime/calls. The standing OPENAI_API_KEY never leaves the server."""
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return {"ok": False, "error": "OPENAI_API_KEY not set"}
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
           "OpenAI-Beta": "realtime=v1"}
    # create the session WITH tools/instructions so the model has them from turn one; fall back to
    # progressively simpler bodies if a given API version rejects fields.
    _tools = _openai_tools()
    bodies = [
        {"model": _REALTIME_MODEL, "instructions": PERSONA, "tools": _tools,
         "tool_choice": "auto", "turn_detection": {"type": "server_vad"},
         "input_audio_transcription": {"model": "whisper-1", "language": "en"}},
        {"session": {"type": "realtime", "model": _REALTIME_MODEL, "instructions": PERSONA,
                     "tools": _tools, "tool_choice": "auto"}},
        {"model": _REALTIME_MODEL},
    ]
    last = ""
    for body in bodies:
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post("https://api.openai.com/v1/realtime/client_secrets", headers=hdr, json=body)
                r.raise_for_status()
                data = r.json()
            secret = (data.get("client_secret") or {}).get("value") or data.get("value")
            if secret:
                return {"ok": True, "client_secret": secret, "model": _REALTIME_MODEL, "voice": _VOICE,
                        "instructions": PERSONA, "tools": _openai_tools(),
                        "expires_at": (data.get("client_secret") or {}).get("expires_at") or data.get("expires_at")}
            last = "no client_secret in response"
        except Exception as e:  # noqa
            last = str(e)[:200]
    log.warning("realtime client_secret mint failed: %s", last)
    return {"ok": False, "error": last}


class ToolCallIn(BaseModel):
    name: str
    arguments: dict | None = None
    conversation_id: str = "rt"


@router.post("/tool")
async def run_tool(body: ToolCallIn):
    """Execute a model-requested tool through the canonical ToolGateway and return the receipt.
    Keeps voice tool-calls on the exact same governed path as text."""
    args = body.arguments or {}
    if body.name == "create_document":
        s = RUNTIME.session(body.conversation_id)
        rec = await RUNTIME._do_document(s, args)
    else:
        rec = await RUNTIME.tools.invoke(body.name, args)
    return {"ok": not rec.get("is_error", False), "receipt": rec,
            "content": json.dumps(rec.get("output"))[:6000]}
