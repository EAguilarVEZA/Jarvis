"""
assistant_api
─────────────
A context-aware AI helper that lives on every screen. Two capabilities:

  POST /api/assistant/help    — "How do I …?" guidance, aware of the current
                                screen. Returns a short markdown explanation +
                                suggested follow-up questions.
  POST /api/assistant/advise  — Data-science advisor for the Test & Learn lab:
                                given a plain-English goal, recommends WHICH
                                statistical tool to use (experiment / t-test /
                                proportions / power / matched control / segment
                                scan), explains the method, and says which tab.

Both are best-effort and degrade gracefully when the model/key is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import logging

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
log = logging.getLogger("assistant_api")


# ── Screen guide: what each surface is for + how to do the common tasks ──
_SCREEN_GUIDE = {
    "pulse": "Pulse is the home overview — goals needing attention, on-track goals, recent briefs, recent activity, and quick links to recent/favorite queries & reports.",
    "ask": "Ask AI is a conversational data chat. Type a business question in plain English; Jarvis writes the query, runs it on BigQuery via the semantic layer, and returns a narrative brief with a chart. It can ask a clarifying question when torn, and auto-run a Test & Learn experiment for causal 'did it work?' questions.",
    "query": "Query Builder is the drag-and-drop analysis surface. Add dimensions and metrics from the left fields rail, click Run, switch chart types under 'Show me', use the Marks card for color/size/label encodings, add calculated fields, parameters, and table calculations, then Save the query.",
    "designer": "Report Designer builds picture-perfect multi-page reports. Drag widgets onto pages, bind them to saved queries or SQL, apply brand themes, add global filters and variables, then export to PPTX/PDF, email on a schedule, or share a read-only link.",
    "dashboards": "Dashboards is the live dashboard builder — interactive widgets with cross-filtering, parameter actions, and drill-through.",
    "experiments": "Test & Learn is the experimentation + statistics lab. Tabs: Analyze experiment (difference-in-differences incrementality with significance, confidence intervals, permutation robustness, and ROI — paste numbers OR pull cohorts from your BigQuery data), Segment scan (find which segments respond best for targeted rollout), Significance test (t-test for means or two-proportion z-test for rates), Sample size & power (design an adequately-powered test), and Matched control (build a bias-minimized control group by nearest-neighbor matching).",
    "metrics": "Metrics browses every metric, business rule, and saved query in the semantic layer.",
    "knowledge": "Knowledge is the business-rules library — reusable definitions and 'always/auto' rules that steer the AI's answers.",
    "goals": "Goals tracks targets vs. actuals and flags off-track goals on Pulse.",
    "funnel": "Funnel visualizes stage-to-stage conversion built from curated metrics.",
    "connect": "Connect manages data sources — native warehouses (BigQuery) and the Airbyte connector catalog.",
    "automations": "Automations schedules report emails and triggers app-to-app actions when conditions are met.",
    "admin": "Admin covers users & groups, usage, settings, email/SMTP, API keys, alerts, activity, jobs, SQL reviews, and evaluations.",
    "curate": "Schema · Curate is the semantic-model canvas — curate tables, define joins by dragging fields, and publish the layer the whole app queries.",
}

_HELP_SYSTEM = (
    "You are the in-app assistant for Jarvis, a business-intelligence + experimentation platform. "
    "Answer the user's 'how do I…' question about USING the app, concisely and concretely, in GitHub-flavored "
    "markdown. Prefer short numbered steps. Reference real screens by name (Pulse, Ask AI, Query Builder, "
    "Report Designer, Dashboards, Test & Learn, Metrics, Knowledge, Goals, Funnel, Connect, Automations, Admin, "
    "Schema·Curate). If the task is causal ('did X work', incrementality, control groups, significance, sample "
    "size), point them to the Test & Learn lab and name the right tab. Keep it under ~150 words. Do not invent "
    "features that aren't described in the screen guide; if unsure, say what's most likely and suggest where to look."
)

_ADVISE_SYSTEM = (
    "You are a data-science advisor inside a Test & Learn lab. Given the user's goal, recommend the single best "
    "tool and explain the method briefly. Respond with ONLY JSON:\n"
    '{"tool": "experiment|ttest|proportions|power|matched|segment", "tab": "analyze|significance|power|matched|segment", '
    '"mode": "paste|data", "title": "<short>", "rationale": "<1-2 sentences why this tool>", '
    '"method": "<plain-English of what it computes>", "steps": ["<step>", "<step>"]}\n'
    "Guidance:\n"
    "- experiment (tab=analyze): measure the causal impact of an initiative with a test vs control group over "
    "before/after periods (difference-in-differences). Use mode='data' if they mention their own data/metrics, else 'paste'.\n"
    "- segment (tab=segment): they want to know WHERE/for whom the program worked best (targeted rollout).\n"
    "- ttest (tab=significance): compare the average of two groups on a continuous metric.\n"
    "- proportions (tab=significance): compare two rates / conversion percentages (a 'p-ratio').\n"
    "- power (tab=power): decide how many units/sample size they need before running a test.\n"
    "- matched (tab=matched): build a fair control group by matching units on characteristics.\n"
    "Pick the closest single tool."
)


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


class HelpRequest(BaseModel):
    question: str
    screen: str = ""            # current screen key (pulse, ask, query, …)


class AdviseRequest(BaseModel):
    goal: str


def _client():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=key)
    except Exception:
        return None


@router.post("/help")
async def help_endpoint(body: HelpRequest):
    if not (body.question or "").strip():
        return {"error": "Ask a question."}
    client = _client()
    guide = _SCREEN_GUIDE.get((body.screen or "").lower())
    ctx = (f"The user is currently on the '{body.screen}' screen. About it: {guide}\n\n" if guide else "")
    # Whole-app map so cross-screen questions work too.
    app_map = "All screens:\n" + "\n".join(f"- {k}: {v}" for k, v in _SCREEN_GUIDE.items())
    if not client:
        # Deterministic fallback so the assistant still helps offline.
        return {
            "ok": True,
            "answer": (f"**{body.screen or 'This screen'}** — {guide}" if guide else
                       "I can explain any screen. Try asking about Query Builder, Report Designer, or Test & Learn."),
            "suggestions": _suggestions(body.screen),
            "model": "offline",
        }
    try:
        model = os.getenv("JARVIS_ASSISTANT_MODEL", "claude-haiku-4-5-20251001")
        resp = await client.messages.create(
            model=model, max_tokens=500, system=_HELP_SYSTEM,
            messages=[{"role": "user", "content": ctx + app_map + "\n\nQuestion: " + body.question}],
        )
        answer = resp.content[0].text if resp.content else ""
    except Exception as e:
        log.warning(f"assistant help failed: {e}")
        answer = (f"**{body.screen or 'This screen'}** — {guide}" if guide else
                  "Sorry, I couldn't reach the assistant model just now.")
    return {"ok": True, "answer": answer, "suggestions": _suggestions(body.screen)}


def _suggestions(screen: str):
    s = (screen or "").lower()
    base = {
        "query": ["How do I add a calculated field?", "How do I save and schedule this query?", "How do I change the chart type?"],
        "designer": ["How do I add a chart to a page?", "How do I email this report on a schedule?", "How do I apply a brand theme?"],
        "experiments": ["Which test should I use?", "How do I run an experiment on my BigQuery data?", "What is difference-in-differences?"],
        "ask": ["What kinds of questions can I ask?", "How do I turn an answer into a report?", "Can it run a control-group experiment?"],
        "pulse": ["How do I set a goal?", "How do I create an alert?", "Where are my saved briefs?"],
    }
    return base.get(s, ["How do I run a Test & Learn experiment?", "How do I build a report?", "How do I ask the AI a data question?"])


@router.post("/advise")
async def advise(body: AdviseRequest):
    """Recommend the right statistical tool for the user's goal."""
    if not (body.goal or "").strip():
        return {"error": "Describe your goal."}
    client = _client()
    if not client:
        # keyword fallback
        g = body.goal.lower()
        tool, tab = ("experiment", "analyze")
        if any(w in g for w in ("segment", "where", "which region", "who responded")):
            tool, tab = "segment", "segment"
        elif any(w in g for w in ("how many", "sample size", "power", "before we run")):
            tool, tab = "power", "power"
        elif any(w in g for w in ("rate", "conversion", "percent", "proportion")):
            tool, tab = "proportions", "significance"
        elif any(w in g for w in ("control group", "match", "comparable")):
            tool, tab = "matched", "matched"
        elif any(w in g for w in ("average", "mean", "compare two groups")):
            tool, tab = "ttest", "significance"
        return {"ok": True, "tool": tool, "tab": tab, "mode": "data",
                "title": "Recommended tool", "rationale": "Chosen from keywords (assistant model offline).",
                "method": "", "steps": [], "model": "offline"}
    try:
        model = os.getenv("JARVIS_ASSISTANT_MODEL", "claude-haiku-4-5-20251001")
        resp = await client.messages.create(
            model=model, max_tokens=500, system=_ADVISE_SYSTEM,
            messages=[{"role": "user", "content": "Goal: " + body.goal}],
        )
        rec = _extract_json(resp.content[0].text if resp.content else "") or {}
    except Exception as e:
        log.warning(f"assistant advise failed: {e}")
        return {"error": "Couldn't reach the assistant model."}
    rec["ok"] = True
    return rec
