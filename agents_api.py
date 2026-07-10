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

import os
import re
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


class ChatRequest(BaseModel):
    message: str
    history: Optional[list] = None     # [{role:'user'|'assistant', content:str}]


@router.post("/{slug}/chat")
async def chat(slug: str, body: ChatRequest):
    a = _load_all().get(slug)
    if not a:
        return {"error": "Agent not found."}
    if not (body.message or "").strip():
        return {"error": "Say something to the agent."}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"error": "AI not configured (ANTHROPIC_API_KEY not set)."}
    system = (f"You ARE the '{a['name']}' agent. Fully embody this role.\n\n"
              f"Role: {a['role']}\n\nYour definition:\n{a['body'][:4000]}" + _PLATFORM_BRIEF)
    msgs = []
    for turn in (body.history or [])[-8:]:
        r = turn.get("role")
        c = (turn.get("content") or "").strip()
        if r in ("user", "assistant") and c:
            msgs.append({"role": r, "content": c})
    msgs.append({"role": "user", "content": body.message})
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(model=model, max_tokens=1400, system=system, messages=msgs)
        answer = resp.content[0].text if resp.content else ""
    except Exception as e:
        log.warning(f"agent chat failed: {e}")
        return {"error": f"Agent unavailable: {e}"}
    return {"ok": True, "agent": a["name"], "answer": answer}
