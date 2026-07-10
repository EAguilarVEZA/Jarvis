"""
workflows_api
─────────────
Agent Studio — orchestrate the agent library into TEAMS that work in SEQUENCE.
A workflow is an ordered list of steps; each step assigns an agent a task, and
each agent receives the outputs of the steps before it as context (so agent 2
builds on agent 1's work). Agents can run real analyses via their tools, so a
workflow can, e.g.: audit paid media → design new creative → write the email →
verify tracking — end to end, then be scheduled.

Routes (prefix /api/workflows):
  GET    /api/workflows                 — list saved workflows
  POST   /api/workflows                 — create/update a workflow
  GET    /api/workflows/{id}            — one workflow
  DELETE /api/workflows/{id}
  POST   /api/workflows/{id}/run        — run a saved workflow (sequential)
  POST   /api/workflows/run_adhoc       — run an unsaved steps array
  POST   /api/workflows/suggest         — AI proposes a sequence for a goal
  GET    /api/workflows/templates       — built-in marketing sequences
  POST   /api/workflows/{id}/schedule   — set a cron schedule
  GET    /api/workflows/runs            — recent run history
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/workflows", tags=["workflows"])
log = logging.getLogger("workflows_api")

_WF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows.json")
_RUN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_runs.json")


def _load(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {key: []}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ── Built-in marketing team templates (agent slugs must exist in the library) ──
_TEMPLATES = [
    {"id": "tpl-fullfunnel", "name": "Full-funnel campaign launch",
     "description": "Plan, buy, create, capture, and measure a new campaign end-to-end.",
     "steps": [
         {"agent_slug": "growth-hacker", "task": "Propose the highest-leverage campaign to launch this quarter and the target segment. Be specific."},
         {"agent_slug": "paid-media-auditor", "task": "Audit current paid media for waste and headroom that this campaign should exploit."},
         {"agent_slug": "ad-creative-strategist", "task": "Design the ad creative concepts and hooks for the campaign above."},
         {"agent_slug": "email-marketing-strategist", "task": "Design the nurture email sequence that converts the traffic this campaign drives."},
         {"agent_slug": "tracking-measurement-specialist", "task": "Specify the tracking + measurement plan so we can prove impact in Test & Learn."},
     ]},
    {"id": "tpl-seo-content", "name": "SEO + content growth sprint",
     "description": "From keyword strategy to content to distribution.",
     "steps": [
         {"agent_slug": "seo-specialist", "task": "Identify the top keyword opportunities and content gaps to target."},
         {"agent_slug": "content-creator", "task": "Turn those opportunities into a concrete content plan with titles and angles."},
         {"agent_slug": "social-media-strategist", "task": "Plan how to distribute and amplify this content across channels."},
     ]},
    {"id": "tpl-explain-drop", "name": "Diagnose a metric drop",
     "description": "Investigate why a KPI moved, then plan the fix.",
     "steps": [
         {"agent_slug": "growth-hacker", "task": "Use explain_metric / driver_analysis on the metric the user names to find what drove the change. Report the drivers."},
         {"agent_slug": "paid-social-strategist", "task": "Given those drivers, recommend concrete paid-social actions to recover or accelerate."},
     ]},
    {"id": "tpl-launch-week", "name": "Product launch week",
     "description": "PR, social, and email working together for a launch.",
     "steps": [
         {"agent_slug": "pr-communications-manager", "task": "Draft the launch narrative and press angle."},
         {"agent_slug": "linkedin-content-creator", "task": "Turn the narrative into a LinkedIn launch content series."},
         {"agent_slug": "email-marketing-strategist", "task": "Write the launch email campaign to the list."},
     ]},
]


class Step(BaseModel):
    agent_slug: str
    task: str
    id: Optional[str] = None


class WorkflowRequest(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    steps: list                       # [{agent_slug, task}]
    schedule: Optional[str] = None
    context: Optional[str] = ""        # optional shared goal/brief prepended to step 1


@router.get("")
async def list_workflows():
    data = _load(_WF_PATH, "workflows")
    items = [{"id": w["id"], "name": w["name"], "description": w.get("description", ""),
              "steps": len(w.get("steps", [])), "schedule": w.get("schedule"),
              "last_run": w.get("last_run")} for w in data.get("workflows", [])]
    return {"ok": True, "workflows": items, "count": len(items)}


@router.get("/templates")
async def templates():
    return {"ok": True, "templates": _TEMPLATES}


@router.post("")
async def save_workflow(body: WorkflowRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    if not body.steps:
        return {"error": "Add at least one step."}
    data = _load(_WF_PATH, "workflows")
    wid = body.id or uuid.uuid4().hex[:12]
    steps = [{"id": s.get("id") or uuid.uuid4().hex[:8],
              "agent_slug": s["agent_slug"], "task": s.get("task", "")} for s in body.steps]
    rec = {"id": wid, "name": body.name.strip(), "description": body.description or "",
           "steps": steps, "schedule": body.schedule, "context": body.context or "",
           "updated_at": int(time.time())}
    wfs = data.setdefault("workflows", [])
    existing = next((i for i, w in enumerate(wfs) if w["id"] == wid), None)
    if existing is not None:
        rec["last_run"] = wfs[existing].get("last_run")
        wfs[existing] = rec
    else:
        wfs.insert(0, rec)
    _save(_WF_PATH, data)
    return {"ok": True, "id": wid}


@router.get("/{wid}")
async def get_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    return {"ok": True, "workflow": w} if w else {"error": "Not found."}


@router.delete("/{wid}")
async def delete_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    before = len(data.get("workflows", []))
    data["workflows"] = [x for x in data.get("workflows", []) if x["id"] != wid]
    _save(_WF_PATH, data)
    return {"ok": True, "deleted": before - len(data["workflows"])}


# ── Execution ────────────────────────────────────────────────────────────────

async def _run_steps(steps, shared_context="", name="workflow"):
    """Execute steps in order, threading each agent's output into the next as
    context. Returns a run record with per-step outputs."""
    import agents_api
    results = []
    transcript = ""   # accumulated context handed to later agents
    if shared_context:
        transcript += f"Shared brief for the whole team:\n{shared_context}\n\n"
    for i, st in enumerate(steps):
        slug = st.get("agent_slug")
        agent = agents_api.agent_by_slug(slug)
        if not agent:
            results.append({"agent_slug": slug, "agent_name": slug, "error": "Agent not found."})
            continue
        prior = (f"Prior teammates already did the following work — build on it, don't repeat it:\n\n{transcript}\n"
                 if transcript.strip() else "")
        task = (f"{prior}Your task as the {agent['name']}: {st.get('task','')}\n\n"
                "Be concrete and hand off clear outputs the next teammate can use.")
        turn = await agents_api.run_agent_turn(agent, task, history=None, use_tools=True)
        answer = turn.get("answer") or turn.get("error") or ""
        results.append({"agent_slug": slug, "agent_name": agent["name"],
                        "task": st.get("task", ""), "answer": answer,
                        "tools_used": turn.get("tools_used", []), "error": turn.get("error")})
        transcript += f"— {agent['name']} —\n{answer}\n\n"
    return {"ok": True, "name": name, "steps": results,
            "ran_at": int(time.time()), "step_count": len(results)}


def _record_run(wid, name, run):
    data = _load(_RUN_PATH, "runs")
    rec = {"id": uuid.uuid4().hex[:12], "workflow_id": wid, "name": name,
           "ran_at": run.get("ran_at"), "step_count": run.get("step_count"),
           "summary": " → ".join(s.get("agent_name", "?") for s in run.get("steps", []))}
    runs = data.setdefault("runs", [])
    runs.insert(0, rec)
    del runs[50:]
    _save(_RUN_PATH, data)


@router.post("/{wid}/run")
async def run_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Not found."}
    run = await _run_steps(w.get("steps", []), w.get("context", ""), w.get("name", "workflow"))
    w["last_run"] = int(time.time())
    _save(_WF_PATH, data)
    _record_run(wid, w.get("name"), run)
    return run


class AdhocRunRequest(BaseModel):
    steps: list
    context: Optional[str] = ""
    name: Optional[str] = "Ad-hoc run"


@router.post("/run_adhoc")
async def run_adhoc(body: AdhocRunRequest):
    if not body.steps:
        return {"error": "Add at least one step."}
    return await _run_steps(body.steps, body.context or "", body.name or "Ad-hoc run")


# ── AI: suggest a sequence for a goal ────────────────────────────────────────

class SuggestRequest(BaseModel):
    goal: str


@router.post("/suggest")
async def suggest(body: SuggestRequest):
    if not (body.goal or "").strip():
        return {"error": "Describe your goal."}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    import agents_api
    roster = agents_api._load_all()
    # compact roster the planner can choose from
    catalog = "\n".join(f"- {a['slug']} ({a['division']}): {a['role']}" for a in roster.values())
    if not key:
        # heuristic fallback: pick a couple of marketing agents
        picks = [s for s in ("growth-hacker", "paid-media-auditor", "content-creator") if s in roster][:3]
        return {"ok": True, "name": body.goal[:60],
                "steps": [{"agent_slug": s, "task": f"Contribute your part toward: {body.goal}"} for s in picks],
                "model": "offline"}
    system = (
        "You assemble a small TEAM of agents into an ordered sequence to accomplish a goal. "
        "Choose 2-5 agents from the roster whose roles fit, ordered so each builds on the previous. "
        "Respond with ONLY JSON: {\"name\": str, \"steps\": [{\"agent_slug\": str, \"task\": str}]}. "
        "agent_slug MUST be an exact slug from the roster. Each task is a concrete instruction for that agent.")
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=model, max_tokens=800, system=system,
            messages=[{"role": "user", "content": f"Goal: {body.goal}\n\nRoster:\n{catalog[:9000]}"}])
        text = resp.content[0].text if resp.content else ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        spec = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"error": f"Suggestion failed: {e}"}
    # keep only valid slugs
    steps = [s for s in (spec.get("steps") or []) if s.get("agent_slug") in roster][:6]
    if not steps:
        return {"error": "Couldn't map that goal to agents — try rephrasing."}
    return {"ok": True, "name": spec.get("name") or body.goal[:60], "steps": steps}


# ── Scheduling ───────────────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    cron: Optional[str] = None


@router.post("/{wid}/schedule")
async def schedule_workflow(wid: str, body: ScheduleRequest):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Not found."}
    w["schedule"] = (body.cron or "").strip() or None
    _save(_WF_PATH, data)
    return {"ok": True, "schedule": w.get("schedule")}


@router.get("/runs")
async def list_runs():
    return {"ok": True, "runs": _load(_RUN_PATH, "runs").get("runs", [])}


async def workflow_tick(now=None):
    """Scheduler hook: run any workflow whose cron is due (once per minute)."""
    import datetime as _d
    now = now or _d.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    try:
        from brain.runner import cron_due
    except Exception:
        return
    data = _load(_WF_PATH, "workflows")
    changed = False
    for w in data.get("workflows", []):
        cron = w.get("schedule")
        if not cron or not cron_due(cron, now) or w.get("_last_min") == stamp:
            continue
        w["_last_min"] = stamp
        changed = True
        try:
            run = await _run_steps(w.get("steps", []), w.get("context", ""), w.get("name", "workflow"))
            w["last_run"] = int(time.time())
            _record_run(w["id"], w.get("name"), run)
            try:
                import jobs
                jobs.record(f"Workflow: {w['name']}", "workflow", "success", "scheduled")
            except Exception:
                pass
        except Exception as e:
            log.warning(f"scheduled workflow {w.get('id')} failed: {e}")
    if changed:
        _save(_WF_PATH, data)
