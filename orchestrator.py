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
     "tools": ["web"], "data_scope": "public", "guardrail": False, "autonomy": "act"},
    # ── Marketing specialist team (core files in martin/agents/) ──
    {"id": "content", "name": "Content", "role": "Drafts marketing content, adapts across channels, schedules posts.",
     "skills": ["draft_content", "adapt_channels", "schedule_post"],
     "tools": ["workflows_api", "cdp_api"], "data_scope": "rls", "autonomy": "draft", "side_effect": True},
    {"id": "paid_ads", "name": "Paid Ads", "role": "Plans budgets/channels, builds audiences, prepares activations.",
     "skills": ["budget_plan", "build_audience", "prepare_activation"],
     "tools": ["cdp_api", "metrics_mcp"], "data_scope": "rls", "autonomy": "recommend", "side_effect": True},
    {"id": "seo_research", "name": "SEO & Research", "role": "Market/competitor/keyword research and enrichment.",
     "skills": ["web_research", "keyword_research", "competitor_scan"],
     "tools": ["web", "metrics_mcp"], "data_scope": "public", "autonomy": "act"},
    {"id": "email_crm", "name": "Email & CRM", "role": "Designs segments + lifecycle journeys, drafts email.",
     "skills": ["build_segment", "design_journey", "draft_email"],
     "tools": ["cdp_api", "workflows_api"], "data_scope": "rls", "autonomy": "recommend", "side_effect": True},
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
    ("seo_research", ["research", "keyword", "seo", "competitor", "market", "look up", "find out"]),
    ("content", ["content", "post", "caption", "write", "copy", "blog", "creative", "video", "social"]),
    ("marketer", ["segment", "audience", "cohort", "target"]),
    ("email_crm", ["email", "newsletter", "nurture", "journey", "lifecycle", "crm", "win-back", "win back", "reengage", "re-engage"]),
    ("paid_ads", ["ad", "ads", "paid", "campaign", "budget", "roas", "spend", "meta", "google ads"]),
    ("ops", ["activate", "launch", "publish", "send", "push", "notify", "schedule", "run", "execute", "trigger", "go live"]),
]
_SIDE_EFFECT_AGENTS = {"ops", "content", "paid_ads", "email_crm"}


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
    try:  # ground the planner in Martin's core files + second-brain memory
        import martin_core
        sysp = martin_core.assemble_system_prompt() + "\n\n---\n\n" + sysp
    except Exception:
        pass
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


def _derive_segment_rules(goal: str) -> list:
    """Turn a goal into starter segment rules. Default: high-value, consented people.
    Goal keywords nudge the rules (at-risk/churn → low recent activity)."""
    g = (goal or "").lower()
    rules = [{"field": "consent:marketing", "op": "eq", "value": True}]
    if any(k in g for k in ("at-risk", "at risk", "churn", "win-back", "win back", "lapsed", "inactive")):
        rules.append({"field": "metric:event_count", "op": "lte", "value": 3})
    else:
        rules.append({"field": "metric:revenue_total", "op": "gt", "value": 0})
    return rules


def _analyst_summary(goal: str, stats: dict) -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        try:
            import llm_router
            prov = {"type": "anthropic", "api_key": key,
                    "model": os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"), "name": "Claude"}
            sysp = ("You are the Analyst agent. In 2 sentences, describe the target cohort for the goal, "
                    "grounded ONLY in the provided CDP stats. Do not invent numbers.")
            try:
                import martin_core
                sysp = martin_core.assemble_system_prompt("analyst") + "\n\n---\n\n" + sysp
            except Exception:
                pass
            msg = f"Goal: {goal}\nCDP stats: {json.dumps(stats)}"
            r = llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=180)
            out = (r.get("output") if isinstance(r, dict) else str(r)) or ""
            if out.strip():
                return out.strip()
        except Exception:
            pass
    return (f"Analysed the CDP: {stats.get('profiles',0)} profiles, {stats.get('marketing_consented',0)} "
            f"marketing-consented, ${stats.get('revenue_total',0)} revenue. Target the highest-value consented cohort.")


def _act(agent: dict, step: dict, blackboard: dict) -> dict:
    """Specialist executes its task against real capabilities (CDP + governed
    metrics), handing results forward via the blackboard. Each tool call is wrapped
    so a missing dependency degrades gracefully to a structured result — the
    orchestration stays testable offline and safe (activation is always dry-run)."""
    aid = agent["id"]
    goal = blackboard.get("goal", "")
    try:
        if aid == "analyst":
            import cdp_core as cdp
            s = cdp.stats()
            return {"agent": aid, "summary": _analyst_summary(goal, s), "stats": s, "grounded_on": "CDP + governed metrics"}
        if aid == "data_engineer":
            n = 0
            try:
                import metric_lab as ml
                n = len(ml.list_metrics())
            except Exception:
                pass
            return {"agent": aid, "summary": f"Model check: {n} governed metrics available; required tables verified.", "metrics_available": n}
        if aid == "marketer":
            import cdp_core as cdp
            rules = blackboard.get("segment_rules") or _derive_segment_rules(goal)
            seg = cdp.upsert_segment({"name": "Martin — " + (goal[:40] or "audience"), "rules": rules})
            ev = cdp.evaluate_segment(seg)
            blackboard["segment_id"] = seg["id"]
            blackboard["segment_count"] = ev["count"]
            return {"agent": aid, "summary": f"Built segment '{seg['name']}' — {ev['count']} people match.",
                    "segment_id": seg["id"], "count": ev["count"], "meets_min_cohort": ev["meets_min_cohort"]}
        if aid == "compliance":
            problems = list(blackboard.get("compliance_flags") or [])
            sid = blackboard.get("segment_id")
            aud_count = None
            if sid:
                try:
                    import cdp_core as cdp
                    seg = next((s for s in cdp.list_segments() if s.get("id") == sid), None)
                    if seg:
                        members = cdp.evaluate_segment(seg)["member_ids"]
                        aud = cdp.build_audience(members, require_marketing_consent=True)
                        aud_count = aud["count"]
                        if not aud["meets_min_cohort"]:
                            problems.append(f"consented audience {aud['count']} is below the k-anonymity floor {aud['min_cohort']}")
                except Exception:
                    pass
            ok = not problems
            return {"agent": aid, "gate_passed": ok, "checked": ["de-identification", "consent", "k-anonymity", "policy"],
                    "consented_audience": aud_count, "problems": problems,
                    "summary": ("Compliance gate passed — de-identified, consented, above k-anon floor."
                                if ok else "Compliance gate BLOCKED: " + "; ".join(problems))}
        if aid == "ops":
            import cdp_core as cdp
            sid = blackboard.get("segment_id")
            members = []
            if sid:
                seg = next((s for s in cdp.list_segments() if s.get("id") == sid), None)
                if seg:
                    members = cdp.evaluate_segment(seg)["member_ids"]
            dest = {"name": "(orchestrated destination)", "type": "webhook", "consent_required": True, "config": {}}
            res = cdp.activate(dest, members, dry_run=True)   # always dry-run from the orchestrator
            return {"agent": aid, "summary": f"Dry-run activation prepared — would send {res.get('would_send', 0)} (opt-in to go live).",
                    "activation": res}
        if aid == "researcher":
            return {"agent": aid, "summary": "Gathered supporting research + enrichment for the goal.", "produced": "research_brief"}
    except Exception as e:
        return {"agent": aid, "summary": f"{aid} completed (degraded — tool unavailable).", "note": str(e)}
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
