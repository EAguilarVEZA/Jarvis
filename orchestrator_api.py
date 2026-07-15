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


class Capture(BaseModel):
    kind: str
    text: str
    title: Optional[str] = None


@router.post("/capture")
async def capture(c: Capture):
    """Voice capture → vault: kind is 'goal' | 'rule' | 'knowledge'. Writes to the
    Obsidian vault (Active Priorities / House Rules / second-brain memory)."""
    if not (c.text or "").strip():
        return _err(400, "text required")
    try:
        import martin_core
        ok = martin_core.capture(c.kind, c.text, c.title or "")
        return {"ok": bool(ok), "kind": c.kind}
    except Exception as e:
        return _err(500, "capture failed", str(e))


class Code(BaseModel):
    name: str
    code: str


@router.post("/set_code")
async def set_code(c: Code):
    try:
        import martin_core
        return {"ok": bool(martin_core.set_code(c.name, c.code))}
    except Exception as e:
        return _err(500, "set_code failed", str(e))


@router.post("/verify_code")
async def verify_code(c: Code):
    try:
        import martin_core
        return {"ok": bool(martin_core.verify_code(c.name, c.code))}
    except Exception:
        return {"ok": False}


class Voiceprint(BaseModel):
    name: str
    sig: list


@router.post("/voiceprint")
async def voiceprint_save(v: Voiceprint):
    try:
        import martin_core
        return {"ok": bool(martin_core.save_voiceprint(v.name, v.sig))}
    except Exception as e:
        return _err(500, "voiceprint save failed", str(e))


@router.get("/voiceprints")
async def voiceprints_get():
    try:
        import martin_core
        return martin_core.get_voiceprints()
    except Exception:
        return {"prints": []}


@router.get("/verify_user")
async def verify_user(name: str):
    """Authorization gate: is this spoken name an active user in Admin (users.json)?
    Martin must not interview or produce for anyone who isn't an authorized user.
    Bootstrap-safe: if no users are configured yet, allow (so admins can set up)."""
    q = " ".join((name or "").lower().split())
    if not q:
        return {"authorized": False, "name": name}
    try:
        import users_api
        data = users_api._load_users()
    except Exception:
        return {"authorized": True, "name": name, "reason": "store-unavailable"}
    users = data.get("users", []) or []
    if not users:
        return {"authorized": True, "name": name, "reason": "no-users-yet"}
    qtok = set(q.split())
    for u in users:
        if str(u.get("status", "active")).lower() not in ("active", "invited", ""):
            continue
        cands = []
        if u.get("name"): cands.append(str(u["name"]).lower())
        fn, ln = str(u.get("first_name") or ""), str(u.get("last_name") or "")
        if fn or ln: cands.append((fn + " " + ln).strip().lower())
        if u.get("email"): cands.append(str(u["email"]).split("@")[0].replace(".", " ").replace("_", " ").lower())
        for c in cands:
            ctok = set(c.split())
            if not ctok:
                continue
            if q == c or qtok.issubset(ctok) or ctok.issubset(qtok):
                return {"authorized": True, "name": u.get("name") or name,
                        "role": u.get("role", "user"), "groups": u.get("groups", [])}
    return {"authorized": False, "name": name}


@router.get("/person")
async def person_get(name: str):
    try:
        import martin_core
        return martin_core.get_person(name)
    except Exception as e:
        return _err(500, "lookup failed", str(e))


class Person(BaseModel):
    name: str
    fields: Optional[dict] = None


@router.post("/person")
async def person_save(p: Person):
    if not (p.name or "").strip():
        return _err(400, "name required")
    try:
        import martin_core
        return {"ok": bool(martin_core.save_person(p.name, p.fields or {}))}
    except Exception as e:
        return _err(500, "save failed", str(e))


class SessionNote(BaseModel):
    name: str
    text: str


@router.post("/session_note")
async def session_note_post(s: SessionNote):
    try:
        import martin_core
        return {"ok": bool(martin_core.add_session_note(s.name, s.text))}
    except Exception as e:
        return _err(500, "note failed", str(e))


@router.get("/context")
async def context():
    """Martin's core-files + second-brain (Obsidian) status: which identity/soul/
    boundaries + agent files are loaded, and whether the vault memory is found."""
    try:
        import martin_core
        return martin_core.status()
    except Exception as e:
        return {"error": str(e)}


@router.get("/context/preview")
async def context_preview(agent_id: str = None):
    """Preview the exact grounding an agent runs on (core files + memory + role)."""
    try:
        import martin_core
        return {"agent_id": agent_id, "system_prompt": martin_core.assemble_system_prompt(agent_id)}
    except Exception as e:
        return {"error": str(e)}
