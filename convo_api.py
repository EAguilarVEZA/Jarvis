"""
convo_api — thin HTTP adapter over the ONE ConversationRuntime (Phase 2 P0).

This used to own a private Anthropic call + tool loop; that logic now lives in conversation_runtime
so voice (realtime_api) and text (this) share exactly one brain. This file only translates HTTP
<-> runtime and exposes memory + latency-metrics endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from conversation_runtime import RUNTIME
from obs_timing import METRICS

router = APIRouter(prefix="/api/convo", tags=["convo"])


class TurnRequest(BaseModel):
    conversation_id: str = "default"
    text: str
    scope: str = "global"
    confirm: bool = False
    spoken_only: str = ""          # assistant text actually heard before a barge-in
    provider: str | None = None    # force a provider ("anthropic"|"openai"); None = router default
    screen_context: str = ""       # visible text on the Martin screen right now (so it can "read" it)


class TurnResponse(BaseModel):
    answer: str = ""
    task: str = "answer"
    tools_used: list = []
    citations: list = []
    artifact: dict | None = None
    needs_confirm: dict | None = None
    grounded: bool = True
    trace: dict = {}
    provider: str = ""
    fell_back: bool = False
    ui_action: dict | None = None


@router.post("/turn", response_model=TurnResponse)
async def turn(body: TurnRequest):
    r = await RUNTIME.run_turn(body.conversation_id, body.text, scope=body.scope,
                               spoken_only=body.spoken_only, provider=body.provider,
                               screen_context=body.screen_context)
    return TurnResponse(answer=r.answer, task=r.task, tools_used=r.tools_used,
                        citations=r.citations, artifact=r.artifact, trace=r.trace,
                        provider=r.provider, fell_back=r.fell_back, ui_action=r.ui_action)


@router.get("/providers")
async def providers():
    """Which model providers are actually live in this process (real keys present)."""
    return {"providers": RUNTIME.router.names(),
            "search_provider": RUNTIME.research.active_provider()}


class MemDelete(BaseModel):
    conversation_id: str = "default"
    item_id: str


@router.get("/memory")
async def list_memory(conversation_id: str = "default"):
    s = RUNTIME.sessions.get(conversation_id)
    items = [i.to_dict() for i in s.memory.active()] if s else []
    return {"ok": True, "items": items}


@router.post("/memory/forget")
async def forget_memory(body: MemDelete):
    s = RUNTIME.sessions.get(body.conversation_id)
    ok = s.memory.forget(body.item_id) if s else False
    return {"ok": ok}


@router.get("/metrics")
async def metrics():
    """Measured per-stage latency (p50/p95/p99). Not a claim targets are met — real samples."""
    return METRICS.report()
