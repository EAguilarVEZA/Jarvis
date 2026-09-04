"""
conversation_runtime — the ONE canonical runtime (Phase 2 P0).

Voice, text, web, and API clients all drive this single object. It owns conversation state
(ActiveContextGraph + semantic memory), model routing (ModelGateway/Router — provider-independent),
tool execution (ToolGateway, with PARALLEL independent calls), cancellation, artifacts, provenance,
policy, and per-stage latency instrumentation. Transports (convo_api HTTP, realtime_api WS) are thin.

No vendor SDK is imported here — only the gateways. Martin owns the transcript, so switching
providers never loses context. This is the eliminate-the-two-brains step of the directive.
"""
from __future__ import annotations

import os
import re
import json
import asyncio
import logging
from dataclasses import dataclass, field

import agent_tools
from model_gateway import Router, CancelToken
from tool_gateway import ToolGateway
from active_context import ActiveContextGraph
from obs_timing import TurnTrace, METRICS

from jarvis_convo.memory import ContextBuilder
from jarvis_convo.persistence import AuditLog
from jarvis_convo.vectors import DurableSemanticMemory, PgVectorBackend
from jarvis_convo.artifacts import ArtifactService, ArtifactStatus
from jarvis_convo.turn_manager import classify, Interruption

log = logging.getLogger("conversation_runtime")

_ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "convo_artifacts")
_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "convo_state")
AUDIT = AuditLog(os.path.join(_STATE_DIR, "audit.jsonl"))

_REMEMBER = re.compile(r"^\s*remember(?: that)?\s+(.+)", re.I)
_DECISION = re.compile(r"\b(we (decided|agreed|will)|let'?s go with|the plan is|"
                       r"remember that|going with|our (goal|target) is)\b", re.I)

PERSONA = (
    "ALWAYS respond in English by default — begin every conversation in English. Only switch to another "
    "language if Edgar explicitly asks you to, or clearly speaks to you in that language and wants the "
    "reply in it. Do not start in Spanish. "
    "You are Martin, Edgar's personal AI assistant. You are warm, direct, and genuinely helpful, and "
    "you can discuss virtually any topic naturally. You have REAL tools — use them whenever the answer "
    "needs fresh, external, current, or computed information (weather, news, prices, a specific web page, "
    "the date/time, math); never guess at those. "
    "IMPORTANT: you DO have access to Orlando Health's marketing data warehouse through the "
    "'orlando_health_data' tool (a governed BigQuery + KTX semantic layer). For ANY question about "
    "our/OH/Martin data — campaigns (including 'how is X campaign doing', 'pediatrics campaigns', "
    "'run/show/pull a campaign for <service line>'), spend, traffic, sessions, referrals, appointments, "
    "leads, conversion, ROI, service lines (cardiology, pediatrics, oncology, etc.), trends and "
    "comparisons — CALL that tool and pass the user's question in natural language (name the service "
    "line). Never say you lack access to the data warehouse; you have it. If a data question is "
    "ambiguous, call the tool with your best interpretation rather than refusing. "
    "When you answer a data question, the numbers automatically render as a CHART on the user's screen, "
    "so do NOT read long tables aloud. Give a short insight: the headline number and the key takeaway or "
    "trend in 1-2 sentences, and let the on-screen chart carry the detail. "
    "For real-world things you have no dedicated tool for (flight status, sports scores, store hours, "
    "a person or place), USE web_search and web_fetch to find the answer — never give up or say you "
    "can't look it up; you can. "
    "MARTIN HEALTH DATA EXCHANGE (synthetic demo): you can run the executive data-commercialization demo. "
    "Use 'data_exchange_inventory' for 'what data could we commercialize / show me the hip-imaging "
    "opportunity', and 'data_exchange_offer' when a buyer describes a cohort — it returns synthetic "
    "feasibility, eligible product tiers T0-T6 with reasons, a recommended tier, and a demo price. ALWAYS "
    "state the numbers and prices are SYNTHETIC DEMO DATA, not real Orlando Health data or market quotes. "
    "For BIG, sophisticated builds — a website, a web app, a script, or a whole AI agent/codebase — you "
    "escalate to a more powerful Claude builder via 'delegate_to_builder'. But FIRST interview the user: "
    "ask 2-4 focused questions to nail the goal, audience, key features, style, and what 'done' means; "
    "confirm your understanding in one line; THEN call delegate_to_builder with a thorough brief. It runs "
    "in the background — say you've started it and use 'builder_status' when they ask how it's going. "
    "You can SEE and DRIVE the user's Martin screen. 'read_screen' returns what is currently shown "
    "(use it when they say 'read this', 'what's on my screen', or to see the midday email/meeting "
    "brief). 'render_brief' displays a full on-screen strategic brief for a data question. 'open_view' "
    "navigates the platform (briefs, dashboards, campaigns, pulse, funnel, etc.) when they say "
    "show/open/take me to. When they say e.g. 'go to briefs', navigate there with open_view, then call "
    "read_screen to see the items, tell them what's available and ASK which one to open, then use "
    "'open_item' with the name they choose. Same pattern for dashboards and other sections. "
    "In live voice, keep replies to 1-3 sentences unless asked to go deeper. Never claim you did something "
    "a tool did not actually return."
)

_CREATE_DOC_SCHEMA = {
    "name": "create_document",
    "description": ("Create a real Markdown document file from content you provide. Returns a validated "
                    "file on disk. Use when the user asks to write/draft a document, brief, plan, or report."),
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string"}, "content": {"type": "string", "description": "Full Markdown body"}},
        "required": ["title", "content"]},
}


@dataclass
class TurnResult:
    answer: str = ""
    task: str = "answer"
    tools_used: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    artifact: dict | None = None
    trace: dict = field(default_factory=dict)
    provider: str = ""
    fell_back: bool = False
    ui_action: dict | None = None


class _Session:
    def __init__(self, conversation_id: str, scope: str):
        self.id = conversation_id
        self.scope = scope
        self.ctx = ActiveContextGraph(conversation_id)       # working memory + entity/task graph
        dsn = os.getenv("DATABASE_URL", "")
        pg = PgVectorBackend(dsn) if dsn else None
        self.memory = DurableSemanticMemory(os.path.join(
            _STATE_DIR, "mem-" + re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id) + ".json"), pg=pg)
        self.artifacts = ArtifactService()


class ConversationRuntime:
    def __init__(self):
        self.router = Router()                                # Anthropic + OpenAI (whichever keys present)
        self.tools = ToolGateway()
        self.tools.register_agent_tools()                     # datetime / weather / calc (+ default web)
        # route web_search/web_fetch through the ResearchGateway (Tavily primary; DDG only fallback)
        from research_gateway import RESEARCH
        self.research = RESEARCH
        for sch in agent_tools.TOOL_SCHEMAS:
            if sch["name"] == "web_search":
                self.tools.register(sch, RESEARCH.search)
            elif sch["name"] == "web_fetch":
                self.tools.register(sch, RESEARCH.fetch)
        # Orlando Health governed data tool (real BigQuery via the semantic layer)
        try:
            from oh_tool import OH_TOOL_SCHEMA, orlando_health_data
            self.tools.register(OH_TOOL_SCHEMA, orlando_health_data)
        except Exception as e:  # noqa
            log.warning("OH data tool not registered: %s", e)
        # platform-aware UI tools: render on-screen briefs + navigate the app the user built
        try:
            from ui_tools import RENDER_BRIEF_SCHEMA, OPEN_VIEW_SCHEMA, render_brief, open_view
            self.tools.register(RENDER_BRIEF_SCHEMA, render_brief)
            self.tools.register(OPEN_VIEW_SCHEMA, open_view)
        except Exception as e:  # noqa
            log.warning("UI tools not registered: %s", e)
        # escalation: delegate heavy builds (website / app / agent / codebase) to Claude Code
        try:
            from builder_tool import DELEGATE_SCHEMA, STATUS_SCHEMA, delegate_to_builder, builder_status
            self.tools.register(DELEGATE_SCHEMA, delegate_to_builder)
            self.tools.register(STATUS_SCHEMA, builder_status)
        except Exception as e:  # noqa
            log.warning("builder tool not registered: %s", e)
        # reliable live flight status via the web
        try:
            from flight_tool import FLIGHT_SCHEMA, flight_status
            self.tools.register(FLIGHT_SCHEMA, flight_status)
        except Exception as e:  # noqa
            log.warning("flight tool not registered: %s", e)
        # Health Data Exchange (synthetic demo): inventory discovery, cohort feasibility, tiers, quotes
        try:
            from exchange_tool import (INVENTORY_SCHEMA, OFFER_SCHEMA,
                                       data_exchange_inventory, data_exchange_offer)
            self.tools.register(INVENTORY_SCHEMA, data_exchange_inventory)
            self.tools.register(OFFER_SCHEMA, data_exchange_offer)
        except Exception as e:  # noqa
            log.warning("data exchange tools not registered: %s", e)
        self.sessions: dict[str, _Session] = {}

    def session(self, cid: str, scope: str = "global") -> _Session:
        s = self.sessions.get(cid)
        if not s:
            s = _Session(cid, scope); self.sessions[cid] = s
        if scope and scope != "global":
            s.scope = scope
        return s

    # ── context assembly ──
    def _context_prompt(self, s: _Session, text: str) -> str:
        ctx = ContextBuilder(s.memory).build(s.ctx.working, text, scope=s.scope)
        lines = []
        active = s.ctx.context_lines()
        if active:
            lines.append("Active context:")
            lines += ["  " + a for a in active]
        if ctx.get("recent_turns"):
            lines.append("Recent conversation:")
            for m in ctx["recent_turns"]:
                lines.append(f"  {m['role']}: {m['text']}")
        if ctx.get("relevant_memory"):
            lines.append("Relevant memory:")
            for it in ctx["relevant_memory"]:
                lines.append(f"  - [{it['kind']}] {it['text']}")
        if ctx.get("preferences"):
            lines.append("User preferences: " + " | ".join(ctx["preferences"]))
        lines.append(f"\nUser: {text}")
        return "\n".join(lines)

    async def _do_document(self, s: _Session, args: dict) -> dict:
        os.makedirs(_ART_DIR, exist_ok=True)
        title = str((args or {}).get("title") or "document")
        content = str((args or {}).get("content") or "")
        fname = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "document"
        fname += ".md"
        art = s.artifacts.run("markdown", fname, lambda cancel: content)
        info = art.to_dict()
        if art.status == ArtifactStatus.COMPLETED:
            try:
                with open(os.path.join(_ART_DIR, fname), "w", encoding="utf-8") as f:
                    f.write(art.content)
                info["path"] = os.path.join(_ART_DIR, fname)
            except Exception as e:  # noqa
                art.status = ArtifactStatus.FAILED; info["error"] = str(e)
        ok = art.status == ArtifactStatus.COMPLETED
        return {"tool": "create_document",
                "output": (f"created {fname} ({len(art.content)} chars)" if ok
                           else f"failed: {info.get('error') or art.error}"),
                "provenance": [], "is_error": not ok, "_artifact": info}

    async def _complete_with_fallback(self, gateway, trace, **kw):
        """One model call with REAL provider fallback: if the chosen provider raises, actually
        re-run the same call on another provider (not merely report that fallback exists)."""
        try:
            with trace.span("model_call"):
                return await gateway.complete(**kw), gateway, False
        except Exception as e:  # noqa
            gw2 = self.router.fallback(gateway)
            if not gw2:
                raise
            AUDIT.append("provider_fallback", trace.conversation_id,
                         frm=getattr(gateway, "name", "?"), to=gw2.name, err=str(e)[:160])
            log.warning("provider %s failed (%s) -> falling back to %s",
                        getattr(gateway, "name", "?"), e, gw2.name)
            with trace.span("model_call"):
                return await gw2.complete(**kw), gw2, True

    # ── the one turn (used by every surface) ──
    async def run_turn(self, conversation_id: str, text: str, scope: str = "global",
                       spoken_only: str = "", provider: str | None = None,
                       screen_context: str = "") -> TurnResult:
        s = self.session(conversation_id, scope)
        text = (text or "").strip()
        if not text:
            return TurnResult(answer="I'm listening.")

        trace = TurnTrace(conversation_id)
        with trace.span("orchestrator"):
            if spoken_only:
                s.ctx.commit_assistant_spoken(spoken_only)
            kind = classify(text, assistant_speaking=bool(spoken_only))
            if kind == Interruption.CORRECTION:
                s.ctx.add_correction(text)
            else:
                s.ctx.commit_user_turn(text)
            mr = _REMEMBER.match(text)
            if mr:
                s.memory.add("project", mr.group(1).strip(), scope=s.scope, provenance="explicit")
            system = PERSONA
            prompt = self._context_prompt(s, text)
            if screen_context:
                prompt = ("What is currently on the user's Martin screen (use it if they refer to "
                          "'this', 'the screen', or the brief/report shown):\n---\n"
                          + screen_context[:6000] + "\n---\n\n" + prompt)
            messages = [{"role": "user", "content": prompt}]
            all_tools = self.tools.schemas() + [_CREATE_DOC_SCHEMA]

        gateway = self.router.choose({"provider": provider})
        tools_used, citations, artifact_info, final = [], [], None, ""
        provider_used, fell_back, ui_action = gateway.name, False, None

        try:
            for _hop in range(5):                              # bounded — no infinite loops (§14)
                comp, gateway, fb = await self._complete_with_fallback(
                    gateway, trace, system=system, messages=messages, tools=all_tools, max_tokens=1500)
                provider_used = gateway.name
                fell_back = fell_back or fb
                messages.append({"role": "assistant", "content": comp.assistant_blocks})
                if comp.stop_reason != "tool_use" or not comp.tool_calls:
                    final = comp.text
                    break

                async def _run_one(tc):
                    if tc.name == "create_document":
                        return tc.id, await self._do_document(s, tc.input or {})
                    return tc.id, await self.tools.invoke(tc.name, tc.input or {})

                with trace.span("tools"):                       # PARALLEL independent tool calls
                    pairs = await asyncio.gather(*[_run_one(tc) for tc in comp.tool_calls])

                results = []
                for tid, rec in pairs:
                    tools_used.append(rec.get("tool"))
                    for p in rec.get("provenance", []):
                        if p.get("url"):
                            citations.append(p["url"])
                    if rec.get("_artifact"):
                        artifact_info = rec["_artifact"]
                    if rec.get("ui_action"):
                        ui_action = rec["ui_action"]
                    AUDIT.append("tool", conversation_id, tool=rec.get("tool"),
                                 is_error=bool(rec.get("is_error")), latency_ms=rec.get("latency_ms"))
                    results.append({"type": "tool_result", "tool_use_id": tid,
                                    "content": json.dumps(rec.get("output"))[:6000],
                                    "is_error": bool(rec.get("is_error"))})
                messages.append({"role": "user", "content": results})
            if not final:
                final = "I ran out of tool steps before finishing — ask me to continue."
        except Exception as e:  # noqa
            log.warning("runtime turn failed: %s", e)
            g2 = self.router.fallback(gateway)
            final = ("I couldn't reach my model just now — check the API balance, then try again."
                     if not g2 else "")
            if not final:
                final = "I hit an error mid-turn; try again."

        s.ctx.commit_assistant_spoken(final)
        if _DECISION.search(text):
            s.memory.add("episodic", text, scope=s.scope, provenance="conversation", confidence=0.8)
        stages = trace.finalize(); METRICS.record(stages)
        task = "artifact" if artifact_info else ("tool" if tools_used else "answer")
        AUDIT.append("turn", conversation_id, task=task, provider=provider_used,
                     fell_back=fell_back, ms=int(stages.get("total_turn", 0)))
        return TurnResult(answer=final, task=task,
                          tools_used=list(dict.fromkeys([t for t in tools_used if t])),
                          citations=list(dict.fromkeys(citations))[:6],
                          artifact=artifact_info, trace=stages,
                          provider=provider_used, fell_back=fell_back, ui_action=ui_action)


# process-wide singleton shared by every transport (voice + text + api)
RUNTIME = ConversationRuntime()
