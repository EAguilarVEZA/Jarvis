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

import system_llm  # route LLM calls through the active system model
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
        return system_llm.anthropic_client(api_key=key)
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


class MartinChatRequest(BaseModel):
    messages: list = []
    directive: str | None = None   # optional extra steer (e.g., CVJ stage coaching)


@router.post("/martin")
async def martin_chat(body: MartinChatRequest):
    """Martin's own conversational brain, grounded on his second-brain + playbooks
    (report/brief/funnel/CVJ/intent-router). Powers in-pill answers and the
    step-by-step Customer Value Journey coaching. Best-effort; degrades gracefully."""
    msgs = [m for m in (body.messages or []) if isinstance(m, dict) and str(m.get("content", "")).strip()]
    if not msgs:
        return {"error": "Say something to Martin."}
    try:
        import martin_core
        system = martin_core.assemble_system_prompt(task=(body.directive or ""))
    except Exception:
        system = ("You are Martin, a warm, concise healthcare-marketing BI + marketing assistant. "
                  "Answer helpfully and briefly.")
    if body.directive:
        system += "\n\n## Right now\n" + body.directive
    conv = [{"role": ("assistant" if m.get("role") == "assistant" else "user"),
             "content": str(m.get("content", ""))} for m in msgs][-16:]

    # Route through the multi-LLM harness so the admin-selected "system model"
    # (e.g. a local Ollama model) powers Martin — with a safe Claude fallback.
    try:
        import system_llm, llm_router
        system_llm.ensure_seed()
        prov = system_llm.resolve(
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", "") or None,
            anthropic_model=os.getenv("JARVIS_MARTIN_MODEL",
                                      os.getenv("JARVIS_ASSISTANT_MODEL", "claude-haiku-4-5-20251001")),
            name="Claude")
    except Exception:
        prov = None
    if not prov:
        return {"ok": True, "answer": "I can’t reach a model right now — pick one in Admin → System model (or add an API key), and I’ll be right back.", "offline": True}
    try:
        # Local models keep everything on-device; egress is allowed for cloud
        # providers the admin explicitly configured.
        r = await llm_router.complete(prov, system, conv, max_tokens=900)
        if r.get("error"):
            raise RuntimeError(r["error"])
        answer = (r.get("output") or "").strip()
        if not answer:
            answer = "I didn’t get anything back from the model just now — try once more?"
    except Exception as e:
        log.warning(f"martin chat failed via {prov.get('name','?')}: {e}")
        answer = "Sorry — I couldn’t reach my model just now. If you just switched models in Admin, make sure it’s running (for a local model: `ollama serve`)."
    return {"ok": True, "answer": answer, "engine": prov.get("name"), "local": (prov.get("type") == "ollama")}


class WebRequest(BaseModel):
    intent: str = ""     # 'weather' | 'news' | 'fetch'
    query: str = ""      # place (weather), topic (news), or a URL/description (fetch)


def _http_get(url: str, timeout: int = 12) -> str:
    """Fetch a URL with a real User-Agent (stdlib only). Returns decoded text."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "SmartWithMartin/1.0 (+https://smartwithmartin.ai)",
        "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


async def _llm_summarize(system: str, user: str, max_tokens: int = 400) -> str:
    """Summarize fetched content through the active system model (Claude by default)."""
    try:
        import system_llm, llm_router
        system_llm.ensure_seed()
        prov = system_llm.resolve(
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", "") or None,
            anthropic_model=os.getenv("JARVIS_MARTIN_MODEL", "claude-haiku-4-5-20251001"),
            name="Claude")
        if not prov:
            return ""
        r = await llm_router.complete(prov, system, [{"role": "user", "content": user}], max_tokens=max_tokens)
        return (r.get("output") or "").strip()
    except Exception as e:
        log.warning(f"web summarize failed: {e}")
        return ""


@router.post("/web")
async def web_action(body: WebRequest):
    """Live-web actions for Martin: weather (wttr.in) and news (Google News RSS),
    summarized for a natural spoken reply. Best-effort; degrades gracefully."""
    import urllib.parse
    intent = (body.intent or "").lower().strip()
    q = (body.query or "").strip()
    try:
        if intent == "weather":
            place = q or "Orlando"
            fmt = "%l:+%C,+%t+(feels+%f),+humidity+%h,+wind+%w"
            raw = _http_get("https://wttr.in/" + urllib.parse.quote(place) + "?format=" + fmt + "&u", timeout=12)
            raw = (raw or "").strip()
            if not raw or "Unknown location" in raw or "<" in raw[:1]:
                return {"ok": False, "answer": f"I couldn’t find the weather for “{place}”. Try a city name."}
            return {"ok": True, "answer": raw, "source": "wttr.in"}

        if intent == "news":
            topic = q.strip()
            if topic:
                url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(topic)
                       + "&hl=en-US&gl=US&ceid=US:en")
            else:
                url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
            xml = _http_get(url, timeout=12)
            titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml) or re.findall(r"<title>(.*?)</title>", xml)
            titles = [t.strip() for t in titles if t.strip()][1:9]   # drop the feed title
            if not titles:
                return {"ok": False, "answer": "I couldn’t pull the news just now — try again in a moment."}
            headlines = "\n".join("- " + t for t in titles)
            summ = await _llm_summarize(
                "You are Martin, a concise briefing assistant. Turn these live headlines into a short, "
                "natural spoken news digest of 4–6 sentences. Group related items, no preamble, no markdown headers.",
                ("Topic: " + topic + "\n\n" if topic else "") + "Headlines:\n" + headlines,
                max_tokens=350)
            return {"ok": True, "answer": summ or headlines, "headlines": titles, "source": "Google News"}

        if intent == "fetch":
            if not q or not re.match(r"^https?://", q):
                return {"ok": False, "answer": "Give me a full URL (starting with http) and I’ll summarize it."}
            html = _http_get(q, timeout=14)
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()[:6000]
            summ = await _llm_summarize(
                "Summarize this web page in 4–6 plain sentences for a spoken reply. No markdown, no preamble.",
                text, max_tokens=350)
            return {"ok": True, "answer": summ or "I fetched the page but couldn’t summarize it.", "source": q}

        return {"ok": False, "answer": "I’m not sure what to look up — try weather, news, or a URL."}
    except Exception as e:
        log.warning(f"web_action {intent} failed: {e}")
        return {"ok": False, "answer": "That live lookup didn’t go through just now — try again in a moment."}


class OpsRequest(BaseModel):
    intent: str = ""   # calendar_today | calendar_next | calendar_upcoming | mail_unread | mail_recent | mail_search
    query: str = ""
    count: int = 8
    hours: int = 6


@router.post("/ops")
async def ops_action(body: OpsRequest):
    """Read-only personal ops for Martin: today's calendar / next event / upcoming,
    and unread / recent / searched email. Uses the local Apple Calendar + Mail
    bridges (macOS). Read-only by design — never sends or deletes anything."""
    intent = (body.intent or "").lower().strip()
    try:
        if intent.startswith("calendar"):
            import calendar_access as cal
            if intent == "calendar_next":
                ev = await cal.get_next_event()
                return {"ok": True, "answer": cal.format_schedule_summary([ev]) if ev else "Nothing else on your calendar right now."}
            if intent == "calendar_upcoming":
                evs = await cal.get_upcoming_events(hours=body.hours or 6)
                return {"ok": True, "answer": cal.format_schedule_summary(evs) if evs else "Nothing coming up in the next few hours."}
            evs = await cal.get_todays_events()
            return {"ok": True, "answer": cal.format_schedule_summary(evs) if evs else "Your calendar is clear today."}

        if intent.startswith("mail"):
            import mail_access as mail
            if intent == "mail_search":
                msgs = await mail.search_mail(body.query or "", count=body.count or 8)
                return {"ok": True, "answer": mail.format_messages_for_voice(msgs) if msgs else f"I didn’t find any email matching “{body.query}”."}
            if intent == "mail_recent":
                msgs = await mail.get_recent_messages(count=body.count or 8)
                return {"ok": True, "answer": mail.format_messages_for_voice(msgs) if msgs else "No recent messages."}
            msgs = await mail.get_unread_messages(count=body.count or 8)
            return {"ok": True, "answer": mail.format_messages_for_voice(msgs) if msgs else "You’re all caught up — no unread email."}

        return {"ok": False, "answer": "I can check your calendar or your email — which would you like?"}
    except Exception as e:
        log.warning(f"ops_action {intent} failed: {e}")
        return {"ok": False, "answer": "I couldn’t reach your Mail/Calendar just now. On your Mac it needs permission under System Settings → Privacy & Security → Automation (allow this app to control Mail and Calendar)."}


class CampaignRequest(BaseModel):
    brief: str = ""


class CampaignSaveRequest(BaseModel):
    brief: str = ""
    stages: list = []


@router.post("/campaign")
async def campaign(body: CampaignRequest):
    """Martin-as-manager builds a full Customer Value Journey campaign: plans the
    funnel, then delegates the lead magnet, tripwire, core offer, and profit
    maximizer to the specialist Jobs. Draft-only."""
    brief = (body.brief or "").strip()
    if len(brief) < 8:
        return {"ok": False, "answer": "Tell me the offer and who it's for, and I'll build the campaign."}
    try:
        import campaign_pipeline
        return await campaign_pipeline.run_campaign(brief)
    except Exception as e:
        log.warning(f"campaign failed: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/campaign_save")
async def campaign_save(body: CampaignSaveRequest):
    """Persist an assembled campaign to the Obsidian vault (03-Projects/Campaigns)."""
    try:
        import campaign_pipeline
        p = campaign_pipeline.save_campaign(body.brief, body.stages or [])
        return {"ok": True, "path": p}
    except Exception as e:
        log.warning(f"campaign_save failed: {e}")
        return {"ok": False, "error": str(e)}


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
