"""Martin — the orchestration brain (pure logic, unit-tested; no FastAPI).

Martin is a supervisor/planner that coordinates specialist agents to accomplish a
goal end-to-end, safely, with a human in the loop. This module implements:

  • Agent registry      — specialists with declared skills, tools (MCP) and data scope
  • Planner             — goal → ordered multi-agent plan (LLM if available, else a
                          deterministic keyword router). A Compliance gate is always
                          inserted before any side-effectful (activation/ops) step.
  • Blackboard          — shared run state agents read from and write to
  • Handoffs            — each step consumes prior results and produces a new one
  • Guardrails          — Compliance gate + human approval on side-effects
  • Audit               — every action recorded (who / what / when / why)

The control flow (plan, route, gate, handoff, approve, audit, aggregate) is the
"brain". In the live server each specialist step executes through its real tools
via MCP (governed metrics, CDP, workflows); here execution produces structured,
deterministic results so the orchestration is fully testable offline.
"""
from __future__ import annotations

import os
import re
import json
import time
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_PATH = os.path.join(_DIR, "orchestrator_agents.json")
RUNS_PATH = os.path.join(_DIR, "orchestrator_runs.json")


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default() if callable(default) else default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


# ═══════════════════════════ Agent registry ════════════════════════════════
DEFAULT_AGENTS = [
    {"id": "analyst", "name": "Analyst", "role": "Answers via governed metrics, forecasts, explains changes.",
     "skills": ["metric_ask", "forecast", "driver_analysis", "explain_change"],
     "tools": ["metrics_mcp"], "data_scope": "rls", "guardrail": False},
    {"id": "data_engineer", "name": "Data Engineer", "role": "Curates tables, builds metrics, validates the model.",
     "skills": ["auto_model", "build_metric", "validate_model", "suggest_joins"],
     "tools": ["semantic_api"], "data_scope": "rls", "guardrail": False},
    {"id": "marketer", "name": "Marketer", "role": "Builds segments, designs journeys, proposes audiences.",
     "skills": ["build_segment", "design_journey", "propose_audience"],
     "tools": ["cdp_api"], "data_scope": "rls", "guardrail": False},
    {"id": "compliance", "name": "Compliance", "role": "De-identification + consent + policy gate.",
     "skills": ["deidentify", "check_consent", "policy_gate", "audit"],
     "tools": ["datavant_api", "sql_security"], "data_scope": "phi_ok", "guardrail": True},
    {"id": "ops", "name": "Ops", "role": "Executes approved actions into connected systems.",
     "skills": ["activate_audience", "run_workflow", "send_message"],
     "tools": ["cdp_api", "workflows_api"], "data_scope": "rls", "guardrail": False, "side_effect": True},
    {"id": "researcher", "name": "Researcher", "role": "Web / deep research and enrichment.",
     "skills": ["web_research", "enrich", "summarize"],
     "tools": ["web"], "data_scope": "public", "guardrail": False},
]


def list_agents() -> list:
    a = _load(AGENTS_PATH, list)
    if not a:
        a = [dict(x) for x in DEFAULT_AGENTS]
        _save(AGENTS_PATH, a)
    return a


def get_agent(aid: str) -> dict | None:
    return next((a for a in list_agents() if a.get("id") == aid), None)


def upsert_agent(d: dict) -> dict:
    agents = list_agents()
    d = dict(d); d["id"] = d.get("id") or ("agent_" + uuid.uuid4().hex[:8])
    for i, a in enumerate(agents):
        if a.get("id") == d["id"]:
            agents[i] = d
            break
    else:
        agents.append(d)
    _save(AGENTS_PATH, agents)
    return d


# ═══════════════════════════ Planner ═══════════════════════════════════════
# Keyword → agent routing for the deterministic fallback. Order encodes a sensible
# analyse → build → gate → act → report flow.
_ROUTES = [
    ("analyst", ["why", "explain", "analyze", "analyse", "forecast", "trend", "driver", "at-risk", "at risk", "churn", "cohort", "understand"]),
    ("data_engineer", ["model", "table", "metric", "schema", "curate", "join"]),
    ("marketer", ["segment", "audience", "campaign", "journey", "win-back", "win back", "reengage", "re-engage", "target"]),
    ("ops", ["activate", "launch", "send", "push", "notify", "email", "run", "execute", "trigger"]),
    ("researcher", ["research", "web", "enrich", "look up", "find out", "competitor"]),
]
_SIDE_EFFECT_AGENTS = {"ops"}


def _deterministic_plan(goal: str) -> list:
    g = (goal or "").lower()
    chosen = []
    for aid, kws in _ROUTES:
        if any(k in g for k in kws):
            chosen.append(aid)
    if not chosen:
        chosen = ["analyst"]  # default: try to answer
    # ensure analyse-first if any downstream build/act present
    if any(a in chosen for a in ("marketer", "ops")) and "analyst" not in chosen:
        chosen = ["analyst"] + chosen
    steps = []
    for i, aid in enumerate(chosen):
        steps.append({"step": i + 1, "agent_id": aid, "task": _task_for(aid, goal),
                      "depends_on": [i] if i > 0 else [], "needs_approval": False})
    return steps


def _task_for(aid: str, goal: str) -> str:
    return {
        "analyst": f"Analyse the situation for: {goal}. Identify the relevant cohort/metric and explain drivers.",
        "data_engineer": "Ensure the required tables/metrics exist and are governed; model anything missing.",
        "marketer": "Build the target segment and design the journey/campaign for the goal.",
        "compliance": "De-identify the audience, verify consent + policy before any activation.",
        "ops": "Execute the approved plan (activate the audience / run the workflow).",
        "researcher": f"Research and enrich context for: {goal}.",
    }.get(aid, f"Work on: {goal}")


def _insert_compliance_gate(steps: list) -> list:
    """Guardrail: a Compliance step must precede any side-effect (Ops/activation)
    step, and side-effect steps require human approval."""
    out = []
    gate_added = False
    for s in steps:
        if s["agent_id"] in _SIDE_EFFECT_AGENTS and not gate_added:
            out.append({"step": 0, "agent_id": "compliance",
                        "task": "Mandatory gate: de-identify + verify consent/policy before activation.",
                        "depends_on": [], "needs_approval": False, "gate": True})
            gate_added = True
        if s["agent_id"] in _SIDE_EFFECT_AGENTS:
            s = dict(s); s["needs_approval"] = True
        out.append(s)
    # renumber + rewire depends_on to previous index
    for i, s in enumerate(out):
        s["step"] = i + 1
        s["depends_on"] = [i] if i > 0 else []
    return out


def plan(goal: str, use_llm: bool = True) -> dict:
    """Return {goal, steps, method}. Tries the LLM planner if configured, else a
    deterministic keyword router. Always inserts the Compliance gate."""
    steps, method = None, "deterministic"
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if use_llm and key:
        try:
            steps = _llm_plan(goal, key)
            method = "llm"
        except Exception:
            steps = None
    if not steps:
        steps = _deterministic_plan(goal)
    steps = _insert_compliance_gate(steps)
    return {"goal": goal, "steps": steps, "method": method, "agents_used": sorted({s["agent_id"] for s in steps})}


def _llm_plan(goal: str, key: str) -> list:
    import llm_router
    agents = [{"id": a["id"], "role": a["role"]} for a in list_agents()]
    prov = {"type": "anthropic", "api_key": key,
            "model": os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"), "name": "Claude"}
    sysp = ("You are Martin, an orchestration planner. Decompose the user's goal into an ordered plan of "
            "sub-tasks, each assigned to ONE specialist agent from the catalog. Respond with ONLY JSON: "
            '{"steps":[{"agent_id":str,"task":str}]}. Use only agent_ids from the catalog. Prefer to analyse '
            "before building, and building before acting. Do NOT include a compliance step — it is added "
            "automatically before any activation.")
    msg = f"Goal: {goal}\n\nAgent catalog (JSON):\n{json.dumps(agents)}"
    r = llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=700)
    txt = r.get("output", "") if isinstance(r, dict) else str(r)
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    data = json.loads(m.group(0)) if m else {}
    raw = data.get("steps") or []
    ids = {a["id"] for a in list_agents()}
    steps = []
    for i, s in enumerate(raw):
        aid = s.get("agent_id")
        if aid in ids:
            steps.append({"step": i + 1, "agent_id": aid, "task": s.get("task") or _task_for(aid, goal),
                          "depends_on": [i] if i > 0 else [], "needs_approval": False})
    return steps or _deterministic_plan(goal)


# ═══════════════════════════ Run lifecycle ═════════════════════════════════
def _runs():
    return _load(RUNS_PATH, dict)


def _write_runs(r):
    _save(RUNS_PATH, r)


def _audit(run: dict, actor: str, action: str, detail: str, extra: dict = None):
    run["trace"].append({"t": time.time(), "actor": actor, "action": action,
                         "detail": detail, **(extra or {})})


def start_run(goal: str, use_llm: bool = True) -> dict:
    p = plan(goal, use_llm=use_llm)
    rid = "run_" + uuid.uuid4().hex[:12]
    steps = []
    for s in p["steps"]:
        steps.append({**s, "status": "pending", "result": None, "approved": False})
    run = {"id": rid, "goal": goal, "method": p["method"], "status": "running",
           "steps": steps, "blackboard": {"goal": goal}, "trace": [],
           "created_at": time.time(), "updated_at": time.time()}
    _audit(run, "martin", "plan", f"Planned {len(steps)} steps ({p['method']})",
           {"agents": p["agents_used"]})
    runs = _runs(); runs[rid] = run; _write_runs(runs)
    return run


def get_run(rid: str) -> dict | None:
    return _runs().get(rid)


def list_runs(limit: int = 50) -> list:
    return sorted(_runs().values(), key=lambda r: -r.get("created_at", 0))[:limit]


def _next_ready(run: dict):
    """Return the next pending step whose dependencies are done, or None."""
    done = {i for i, s in enumerate(run["steps"]) if s["status"] == "done"}
    for i, s in enumerate(run["steps"]):
        if s["status"] != "pending":
            continue
        if all((d - 1) in done for d in (s.get("depends_on") or [])):
            return i, s
    return None, None


def _act(agent: dict, step: dict, blackboard: dict) -> dict:
    """Specialist executes its task. Reference implementation returns a structured,
    side-effect-free result (dry-run) grounded in the blackboard; the live server
    swaps in real MCP tool calls. The Compliance agent performs a real gate check."""
    aid = agent["id"]
    prior = blackboard.get("last_result")
    if aid == "compliance":
        # real gate: pass unless the blackboard flags raw PHI / missing consent
        problems = blackboard.get("compliance_flags") or []
        ok = not problems
        return {"agent": aid, "gate_passed": ok, "checked": ["de-identification", "consent", "policy"],
                "problems": problems, "summary": "Compliance gate passed — safe to proceed." if ok
                else "Compliance gate BLOCKED: " + "; ".join(problems)}
    if aid == "analyst":
        return {"agent": aid, "summary": f"Analysed goal and identified the target cohort + key drivers for: {step['task']}",
                "produced": "cohort_definition", "grounded_on": "governed metrics"}
    if aid == "marketer":
        return {"agent": aid, "summary": "Built a target segment and drafted the journey.",
                "produced": "segment + journey draft", "depends_on_result": prior}
    if aid == "data_engineer":
        return {"agent": aid, "summary": "Verified/curated required tables + metrics (governed).", "produced": "model_ready"}
    if aid == "ops":
        return {"agent": aid, "summary": "Executed the approved action (dry-run in reference).",
                "action": "activate_audience", "dry_run": True}
    if aid == "researcher":
        return {"agent": aid, "summary": "Gathered supporting research + enrichment.", "produced": "research_brief"}
    return {"agent": aid, "summary": f"Completed: {step['task']}"}


def advance_run(rid: str) -> dict:
    """Execute the next ready step. Side-effect steps that need approval pause the
    run until approved. Returns the updated run + what happened."""
    runs = _runs(); run = runs.get(rid)
    if not run:
        return {"error": "run not found"}
    if run["status"] in ("done", "blocked"):
        return {"run": run, "message": f"run already {run['status']}"}
    i, step = _next_ready(run)
    if step is None:
        run["status"] = "done" if all(s["status"] == "done" for s in run["steps"]) else run["status"]
        run["updated_at"] = time.time(); _write_runs(runs)
        return {"run": run, "message": "no ready step" if run["status"] != "done" else "run complete"}
    if step.get("needs_approval") and not step.get("approved"):
        step["status"] = "awaiting_approval"; run["status"] = "awaiting_approval"
        _audit(run, "martin", "pause", f"Step {step['step']} ({step['agent_id']}) needs human approval")
        run["updated_at"] = time.time(); _write_runs(runs)
        return {"run": run, "awaiting_approval": step["step"]}
    agent = get_agent(step["agent_id"]) or {"id": step["agent_id"], "name": step["agent_id"]}
    step["status"] = "running"
    result = _act(agent, step, run["blackboard"])
    step["result"] = result; step["status"] = "done"
    run["blackboard"]["last_result"] = result
    run["blackboard"][f"step_{step['step']}_{step['agent_id']}"] = result
    _audit(run, step["agent_id"], "execute", step["task"], {"result": result.get("summary")})
    # compliance blocking → block the run
    if step["agent_id"] == "compliance" and result.get("gate_passed") is False:
        run["status"] = "blocked"
        _audit(run, "martin", "block", "Compliance gate blocked the run", {"problems": result.get("problems")})
    elif all(s["status"] == "done" for s in run["steps"]):
        run["status"] = "done"; _audit(run, "martin", "complete", "All steps done — aggregating result")
    else:
        run["status"] = "running"
    run["updated_at"] = time.time(); _write_runs(runs)
    return {"run": run, "executed_step": step["step"]}


def approve_step(rid: str, step_no: int, approver: str = "user") -> dict:
    runs = _runs(); run = runs.get(rid)
    if not run:
        return {"error": "run not found"}
    for s in run["steps"]:
        if s["step"] == step_no:
            s["approved"] = True
            if s["status"] == "awaiting_approval":
                s["status"] = "pending"
            run["status"] = "running"
            _audit(run, approver, "approve", f"Approved step {step_no} ({s['agent_id']})")
            run["updated_at"] = time.time(); _write_runs(runs)
            return {"ok": True, "run": run}
    return {"error": "step not found"}


def run_to_completion(rid: str, max_steps: int = 20) -> dict:
    """Advance until done, blocked, or awaiting approval (auto-approves nothing)."""
    for _ in range(max_steps):
        r = advance_run(rid)
        run = r.get("run") or {}
        if run.get("status") in ("done", "blocked", "awaiting_approval"):
            break
        if "message" in r and r["message"].startswith("no ready"):
            break
    return get_run(rid)
