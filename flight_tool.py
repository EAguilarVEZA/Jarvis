"""
flight_tool — reliable live flight status via the web (Phase 2).

There's no licensed flight API wired yet, so this uses the ResearchGateway (Tavily live search, which
returns a synthesized answer + source URLs from flight trackers like FlightAware / Google Flights /
FlightRadar24). A dedicated tool makes "what's my flight status" deterministic instead of relying on the
model to remember to web-search.
"""
from __future__ import annotations

import datetime as _dt

from research_gateway import RESEARCH

FLIGHT_SCHEMA = {
    "name": "flight_status",
    "description": (
        "Get the CURRENT live status of a specific flight — departure/arrival times, delays, gate, "
        "terminal, and where the aircraft is. Accepts an airline + number like 'Frontier 4025', "
        "'F9 4025', 'Delta 215', or a route. Pulls from live flight-tracker web sources. Use this "
        "whenever the user asks about a flight, 'my flight', arrivals/departures, or delays."),
    "input_schema": {"type": "object", "properties": {
        "flight": {"type": "string", "description": "Airline + flight number, e.g. 'Frontier 4025' or 'F9 4025'"}},
        "required": ["flight"]},
}


async def flight_status(args: dict) -> dict:
    f = str((args or {}).get("flight", "")).strip()
    if not f:
        return {"output": "Which flight? Give me the airline and number (e.g. Frontier 4025).",
                "provenance": [], "is_error": True}
    today = _dt.date.today().strftime("%B %d, %Y")
    q = f"{f} flight status live today {today} departure arrival delay gate"
    res = await RESEARCH.search({"query": q})
    # research returns {output:{provider,answer,results}|str, provenance, is_error}
    if res.get("is_error"):
        return res
    out = res.get("output")
    if isinstance(out, dict):
        out = {"flight": f, "as_of": today, "answer": out.get("answer", ""), "sources": out.get("results", [])[:5]}
    return {"output": out, "provenance": res.get("provenance", []), "is_error": False}
