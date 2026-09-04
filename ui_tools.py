"""
ui_tools — platform-aware tools (Phase 2). Let the agent DRIVE the existing Martin UI: render the
on-screen strategic brief and navigate to sections/dashboards the user already built. These tools
carry no server logic — they return a `ui_action` that the browser executes with the app's own
functions (doResearch / setNav / nav clicks), so voice and text both get "put it on my screen"
and "open the cardiology dashboard" using everything already there.
"""
from __future__ import annotations

_VIEWS = ["ask", "pulse", "campaigns", "dashboards", "goals", "funnel", "experiments",
          "agents", "metrics", "knowledge", "query", "designer", "marketplace",
          "automations", "admin", "martin"]

RENDER_BRIEF_SCHEMA = {
    "name": "render_brief",
    "description": (
        "Render a FULL on-screen strategic brief / dashboard for a business or data question using the "
        "platform's existing Ask engine (KTX + knowledge + rules + BigQuery semantic layer). Use when "
        "the user says show / pull up / display / put it on screen / run a brief, or clearly wants to "
        "SEE the analysis and charts, not just hear a number. Provide the question in natural language."),
    "input_schema": {"type": "object", "properties": {
        "question": {"type": "string"}}, "required": ["question"]},
}

OPEN_VIEW_SCHEMA = {
    "name": "open_view",
    "description": (
        "Navigate the Martin platform to a section the user built. Use for 'take me to / open / show me' "
        "a screen. Allowed views: " + ", ".join(_VIEWS) + "."),
    "input_schema": {"type": "object", "properties": {
        "view": {"type": "string", "enum": _VIEWS}}, "required": ["view"]},
}


async def render_brief(args: dict) -> dict:
    q = str((args or {}).get("question", "")).strip()
    if not q:
        return {"output": "empty question", "provenance": [], "is_error": True}
    return {"output": "Rendering the brief on screen for: " + q, "provenance": [], "is_error": False,
            "ui_action": {"action": "ask", "prompt": q}}


async def open_view(args: dict) -> dict:
    v = str((args or {}).get("view", "")).strip().lower()
    if v not in _VIEWS:
        return {"output": "unknown view '" + v + "'", "provenance": [], "is_error": True}
    return {"output": "Opening " + v + " on screen.", "provenance": [], "is_error": False,
            "ui_action": {"action": "nav", "view": v}}
