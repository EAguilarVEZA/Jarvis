"""Martin orchestration API (prefix /api/orchestrator).

The supervisor brain: register specialist agents, plan a goal into a multi-agent
workflow, run it with a Compliance gate + human approval on side-effects, and read
the full audit trace.

Endpoints:
  GET  /api/orchestrator/agents            — specialist registry
  POST /api/orchestrator/agents            — add / update an agent
  POST /api/orchestrator/plan              — goal → plan (no execution)
  POST /api/orchestrator/run               — start a run (plan + execute-ready)
  POST /api/orchestrator/run/{id}/advance  — execute next ready step
  POST /api/orchestrator/run/{id}/approve  — approve a paused side-effect step
  GET  /api/orchestrator/run/{id}          — run status + blackboard + trace
  GET  /api/orchestrator/runs              — recent runs
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

import orchestrator as orch

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


def _err(status, error, detail=""):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


@router.get("/agents")
async def agents():
    return {"agents": orch.list_agents()}


class Agent(BaseModel):
    id: Optional[str] = None
    name: str
    role: Optional[str] = ""
    skills: Optional[list] = None
    tools: Optional[list] = None
    data_scope: Optional[str] = "rls"
    guardrail: Optional[bool] = False


@router.post("/agents")
async def save_agent(a: Agent):
    return {"ok": True, "agent": orch.upsert_agent(a.dict(exclude_none=True))}


class Goal(BaseModel):
    goal: str
    use_llm: Optional[bool] = True


@router.post("/plan")
async def plan(g: Goal):
    if not (g.goal or "").strip():
        return _err(400, "goal required")
    return orch.plan(g.goal, use_llm=bool(g.use_llm))


@router.post("/run")
async def run(g: Goal):
    if not (g.goal or "").strip():
        return _err(400, "goal required")
    run = orch.start_run(g.goal, use_llm=bool(g.use_llm))
    # advance through the ready steps until a pause (approval), block, or completion
    run = orch.run_to_completion(run["id"])
    return {"run": run}


@router.post("/run/{rid}/advance")
async def advance(rid: str):
    return orch.advance_run(rid)


class Approve(BaseModel):
    step: int
    approver: Optional[str] = "user"


@router.post("/run/{rid}/approve")
async def approve(rid: str, a: Approve):
    res = orch.approve_step(rid, a.step, a.approver or "user")
    if res.get("error"):
        return _err(404, res["error"])
    # continue running after approval
    run = orch.run_to_completion(rid)
    return {"ok": True, "run": run}


@router.get("/run/{rid}")
async def get_run(rid: str):
    r = orch.get_run(rid)
    return {"run": r} if r else _err(404, "run not found", rid)


@router.get("/runs")
async def runs(limit: int = 50):
    return {"runs": orch.list_runs(limit)}
