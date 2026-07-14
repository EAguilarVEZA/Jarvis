"""
agents_api
──────────
The Agency — a library of specialist AI agents bundled under ./agents (grouped
by division: Marketing, Paid Media, Sales, …). Each agent is a markdown file
with YAML frontmatter (slug, division, aliases, tags) + a role quote + a
"What it does" body. This module lists them, serves one, and lets you chat with
an agent (its definition becomes the system prompt, and the agent is told about
the Jarvis platform so it can drive real marketing analysis).

Routes (prefix /api/agents):
  GET  /api/agents                 — list all agents (id, name, division, role, tags)
  GET  /api/agents/divisions       — division counts
  GET  /api/agents/{slug}          — full agent (definition + body)
  POST /api/agents/{slug}/chat     — chat with the agent
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import logging
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/agents", tags=["agents"])
log = logging.getLogger("agents_api")

_AGENTS_DIR = os.getenv("JARVIS_AGENTS_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents"))


def _parse_agent(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if m:
        raw, body = m.group(1), m.group(2)
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip()
    slug = fm.get("slug") or os.path.splitext(os.path.basename(path))[0]
    division = (fm.get("division") or os.path.basename(os.path.dirname(path))).strip('"')
    name_m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    name = name_m.group(1).strip() if name_m else slug
    role_m = re.search(r"\[!quote\]\s*(.+)", body)
    role = role_m.group(1).strip() if role_m else ""
    # "What it does" paragraph
    wd = ""
    wm = re.search(r"##\s*What it does\s*\n+(.+?)(\n##|\n---|\Z)", body, re.DOTALL)
    if wm:
        wd = " ".join(wm.group(1).split())
    tags_raw = fm.get("tags", "")
    tags = [t.strip().strip("[]") for t in tags_raw.strip("[]").split(",") if t.strip() and t.strip() != "agent"]
    return {"slug": slug, "name": name, "division": division, "role": role,
            "what": wd, "tags": tags, "body": body}


@lru_cache(maxsize=1)
def _load_all() -> dict:
    """Scan the agents dir once. Returns {slug: agent}. Cached."""
    out = {}
    if not os.path.isdir(_AGENTS_DIR):
        log.warning("agents dir not found: %s", _AGENTS_DIR)
        return out
    for root, _dirs, files in os.walk(_AGENTS_DIR):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            if fn.lower().startswith("readme") or "index" in fn.lower():
                continue
            a = _parse_agent(os.path.join(root, fn))
            if a and a.get("slug"):
                out[a["slug"]] = a
    return out


def _reload():
    _load_all.cache_clear()
    return _load_all()


@router.get("")
async def list_agents(division: str = "", q: str = ""):
    agents = list(_load_all().values())
    if division:
        agents = [a for a in agents if a["division"].lower() == division.lower()]
    if q:
        ql = q.lower()
        agents = [a for a in agents if ql in a["name"].lower() or ql in a["role"].lower()
                  or ql in a["what"].lower() or ql in a["division"].lower()]
    agents.sort(key=lambda a: (a["division"], a["name"]))
    items = [{"slug": a["slug"], "name": a["name"], "division": a["division"],
              "role": a["role"], "tags": a["tags"]} for a in agents]
    return {"ok": True, "agents": items, "count": len(items)}


@router.get("/divisions")
async def divisions():
    counts = {}
    for a in _load_all().values():
        counts[a["division"]] = counts.get(a["division"], 0) + 1
    # marketing-forward ordering
    order = ["Marketing", "Paid Media", "Sales"]
    divs = [{"division": d, "count": counts[d]} for d in order if d in counts]
    divs += sorted(({"division": d, "count": c} for d, c in counts.items() if d not in order),
                   key=lambda x: x["division"])
    return {"ok": True, "divisions": divs, "total": sum(counts.values())}


@router.get("/{slug}")
async def get_agent(slug: str):
    a = _load_all().get(slug)
    if not a:
        return {"error": "Agent not found."}
    return {"ok": True, "agent": a}


_PLATFORM_BRIEF = (
    "\n\nYou are running inside Jarvis, a business-intelligence + marketing-automation platform. "
    "You can guide the user to concrete actions using its capabilities: Ask AI (plain-English data "
    "questions over their BigQuery via a semantic layer), the Query Builder, Report Designer, Dashboards, "
    "and the Test & Learn data-science lab (controlled experiments / difference-in-differences, matched & "
    "synthetic control, CUPED, forecasting, anomaly detection, driver analysis, correlation, segment scan, "
    "and one-click 'Explain a metric'). When a task calls for measuring impact or finding drivers, tell the "
    "user exactly which tool to use and how. Be concrete and action-oriented; give steps, examples, and "
    "specific recommendations rather than generic advice."
)


# ── Tools the agents can call (read-only analysis over the semantic layer) ──
_AGENT_TOOLS = [
    {"name": "list_datasets", "description": "List available curated datasets (tables) to analyze.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "describe_dataset", "description": "Show a dataset's dimensions, metrics, and date fields.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "query_data", "description": "Run an aggregate query: pick a table, dimensions, metrics, optional filters. Returns rows.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "dimensions": {"type": "array", "items": {"type": "string"}},
         "metrics": {"type": "array", "items": {"type": "string"}},
         "filters": {"type": "array", "items": {"type": "object"}},
         "limit": {"type": "integer"}}, "required": ["table", "metrics"]}},
    {"name": "explain_metric", "description": "One-shot analysis of a metric: trend, forecast, anomalies, and drivers.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"}, "metric_field": {"type": "string"}, "date_field": {"type": "string"},
         "window_start": {"type": "string"}, "window_end": {"type": "string"},
         "dimension_field": {"type": "string"}}, "required": ["table", "metric_field", "date_field", "window_start", "window_end"]}},
    {"name": "forecast_metric", "description": "Forecast a metric forward with confidence bands.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"}, "metric_field": {"type": "string"}, "date_field": {"type": "string"},
         "window_start": {"type": "string"}, "window_end": {"type": "string"}, "horizon": {"type": "integer"}},
         "required": ["table", "metric_field", "date_field", "window_start", "window_end"]}},
    {"name": "driver_analysis", "description": "Explain a metric change: decompose it into which segments drove it (prior vs current window).",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"}, "metric_field": {"type": "string"}, "dimension_field": {"type": "string"},
         "date_field": {"type": "string"}, "prior_start": {"type": "string"}, "prior_end": {"type": "string"},
         "current_start": {"type": "string"}, "current_end": {"type": "string"}},
         "required": ["table", "metric_field", "dimension_field", "date_field", "prior_start", "prior_end", "current_start", "current_end"]}},
]


async def _execute_tool(name: str, inp: dict) -> dict:
    """Dispatch an agent tool call to the semantic layer / stats engine. Read-only."""
    try:
        if name == "list_datasets":
            from semantic import load_model
            m = load_model()
            return {"datasets": [t.key for t in list(m.tables.values())[:60]]}
        if name == "describe_dataset":
            from semantic import load_model
            t = load_model().table(inp.get("table"))
            if not t:
                return {"error": f"Unknown table '{inp.get('table')}'."}
            return {"table": t.key,
                    "dimensions": [f.key for f in t.dimensions],
                    "metrics": [f.key for f in t.metrics],
                    "dates": [f.key for f in t.dates]}
        if name == "query_data":
            from semantic import load_model, build_sql, run_query, StructuredQuery
            import asyncio as _a
            m = load_model(); pt = inp.get("table")
            qdict = {"primary_table": pt,
                     "dimensions": [{"table": pt, "field": d} for d in (inp.get("dimensions") or [])],
                     "metrics": [{"table": pt, "field": x} for x in (inp.get("metrics") or [])],
                     "filters": [dict(f, table=f.get("table", pt)) for f in (inp.get("filters") or [])],
                     "limit": min(int(inp.get("limit") or 50), 200)}
            sql = build_sql(StructuredQuery.from_dict(qdict), m)
            res = await _a.get_running_loop().run_in_executor(None, run_query, sql)
            return {"columns": res.columns, "rows": (res.rows or [])[:100], "row_count": res.row_count}
        # stats-engine tools
        import experiments_api as X
        if name == "explain_metric":
            r = await X.auto_insights(X.AutoInsightsRequest(
                primary_table=inp["table"], metric_field=inp["metric_field"], date_field=inp["date_field"],
                window_start=inp["window_start"], window_end=inp["window_end"],
                dimension_field=inp.get("dimension_field"), metric_name=inp["metric_field"]))
            return {k: r.get(k) for k in ("narrative", "trend_change_pct", "error")} if isinstance(r, dict) else r
        if name == "forecast_metric":
            r = await X.forecast_from_data(X.ForecastFromDataRequest(
                primary_table=inp["table"], metric_field=inp["metric_field"], date_field=inp["date_field"],
                window_start=inp["window_start"], window_end=inp["window_end"], horizon=int(inp.get("horizon") or 6),
                metric_name=inp["metric_field"]))
            return {k: r.get(k) for k in ("summary", "forecast", "forecast_change_pct", "mape_pct", "error")} if isinstance(r, dict) else r
        if name == "driver_analysis":
            r = await X.driver_from_data(X.DriverFromDataRequest(
                primary_table=inp["table"], metric_field=inp["metric_field"], dimension_field=inp["dimension_field"],
                date_field=inp["date_field"], prior_start=inp["prior_start"], prior_end=inp["prior_end"],
                current_start=inp["current_start"], current_end=inp["current_end"], metric_name=inp["metric_field"]))
            return {k: r.get(k) for k in ("summary", "drivers", "total_change", "top_positive", "top_negative", "error")} if isinstance(r, dict) else r
        return {"error": f"Unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}


async def run_agent_turn(agent: dict, message: str, history=None, use_tools=True,
                         extra_system: str = "") -> dict:
    """Run one agent turn with the bounded tool-use loop. Reused by /chat and by
    the workflow orchestrator. Returns {answer, tools_used} or {error}."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"error": "AI not configured (ANTHROPIC_API_KEY not set)."}
    system = (f"You ARE the '{agent['name']}' agent. Fully embody this role.\n\n"
              f"Role: {agent['role']}\n\nYour definition:\n{agent['body'][:4000]}" + _PLATFORM_BRIEF)
    use_tools = use_tools and os.getenv("JARVIS_AGENT_TOOLS", "1") != "0"
    if use_tools:
        system += ("\n\nYou have TOOLS to analyze the user's real data (list_datasets, describe_dataset, "
                   "query_data, explain_metric, forecast_metric, driver_analysis). When a task needs numbers, "
                   "USE them: list/describe to find the right table & fields, run the analysis, then explain "
                   "the result in your voice with a clear recommendation. Prefer real data over assumptions.")
    if extra_system:
        system += "\n\n" + extra_system
    msgs = []
    for turn in (history or [])[-8:]:
        r = turn.get("role"); c = (turn.get("content") or "").strip()
        if r in ("user", "assistant") and c:
            msgs.append({"role": r, "content": c})
    msgs.append({"role": "user", "content": message})
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        tools_used, answer, trace = [], "", []
        for _ in range(6):
            kwargs = {"model": model, "max_tokens": 1600, "system": system, "messages": msgs}
            if use_tools:
                kwargs["tools"] = _AGENT_TOOLS
            resp = await client.messages.create(**kwargs)
            # Capture the agent's reasoning text on this step (before any tool call).
            think = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if think:
                trace.append({"kind": "thought", "text": think[:1200]})
            if getattr(resp, "stop_reason", "") == "tool_use":
                msgs.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if getattr(block, "type", "") == "tool_use":
                        out = await _execute_tool(block.name, block.input or {})
                        tools_used.append({"tool": block.name, "input": block.input})
                        summary = json.dumps(out, default=str)
                        trace.append({"kind": "tool", "tool": block.name,
                                      "input": block.input or {},
                                      "result": summary[:600]})
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": summary[:6000]})
                msgs.append({"role": "user", "content": results})
                continue
            answer = think
            break
    except Exception as e:
        log.warning(f"agent turn failed: {e}")
        return {"error": f"Agent unavailable: {e}"}
    return {"ok": True, "agent": agent["name"], "answer": answer,
            "tools_used": tools_used, "trace": trace}


def agent_by_slug(slug):  # non-route accessor for other modules (workflows)
    return _load_all().get(slug)


class ChatRequest(BaseModel):
    message: str
    history: Optional[list] = None     # [{role:'user'|'assistant', content:str}]
    tools: bool = True                 # allow the agent to run analyses


@router.post("/{slug}/chat")
async def chat(slug: str, body: ChatRequest):
    a = _load_all().get(slug)
    if not a:
        return {"error": "Agent not found."}
    if not (body.message or "").strip():
        return {"error": "Say something to the agent."}
    return await run_agent_turn(a, body.message, body.history, body.tools)


# ── Saved agent chats ────────────────────────────────────────────────────────
_CHAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_chats.json")


def _chats_load():
    try:
        with open(_CHAT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"chats": []}


def _chats_save(d):
    tmp = _CHAT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, _CHAT_PATH)


class SaveChatRequest(BaseModel):
    agent_slug: str
    agent_name: str
    title: Optional[str] = None
    messages: list                      # [{role, content}]


@router.post("/chats/save")
async def save_chat(body: SaveChatRequest):
    if not body.messages:
        return {"error": "Nothing to save."}
    d = _chats_load()
    rec = {"id": uuid.uuid4().hex[:12], "created_at": int(time.time()),
           "agent_slug": body.agent_slug, "agent_name": body.agent_name,
           "title": (body.title or (body.messages[0].get("content", "")[:60])),
           "messages": body.messages}
    d.setdefault("chats", []).insert(0, rec)
    _chats_save(d)
    return {"ok": True, "id": rec["id"]}


@router.get("/chats")
async def list_chats():
    d = _chats_load()
    items = [{"id": c["id"], "created_at": c.get("created_at"), "agent_name": c.get("agent_name"),
              "agent_slug": c.get("agent_slug"), "title": c.get("title"),
              "turns": len(c.get("messages", []))} for c in d.get("chats", [])]
    return {"ok": True, "chats": items, "count": len(items)}


@router.get("/chats/{cid}")
async def get_chat(cid: str):
    d = _chats_load()
    c = next((x for x in d.get("chats", []) if x.get("id") == cid), None)
    return {"ok": True, "chat": c} if c else {"error": "Not found."}


@router.delete("/chats/{cid}")
async def delete_chat(cid: str):
    d = _chats_load()
    before = len(d.get("chats", []))
    d["chats"] = [x for x in d.get("chats", []) if x.get("id") != cid]
    _chats_save(d)
    return {"ok": True, "deleted": before - len(d["chats"])}
