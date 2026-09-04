"""
agent_tools — REAL tools for the conversational engine (AI OS spec §7, §8, §15).

The whole point of Track B: the assistant is generic by default and specific when it
matters. These are actual, deterministic capabilities with real receipts + provenance —
never the "giant system prompt pretending to have tools" anti-pattern (§15). Each tool
returns {"output": ..., "provenance": [...], "is_error": bool}; on failure it says so
plainly so the model can never fabricate a result.

All network calls run on the user's machine (httpx), time-out, and degrade gracefully.
No API keys required beyond what the app already has (web/search/weather are keyless).
"""
from __future__ import annotations

import re
import time
import html
import datetime as _dt

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
        48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
        75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent showers",
        95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "severe thunderstorm"}


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ── individual tools (async, real) ───────────────────────────────────────────
async def _now(args: dict) -> dict:
    now = _dt.datetime.now().astimezone()
    return {"output": {"iso": now.isoformat(), "human": now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")},
            "provenance": [{"source": "system_clock", "at": time.time()}], "is_error": False}


async def _calculate(args: dict) -> dict:
    import ast, operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Pow: op.pow, ast.USub: op.neg, ast.Mod: op.mod}
    def ev(n):
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.BinOp): return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp): return ops[type(n.op)](ev(n.operand))
        raise ValueError("unsupported expression")
    try:
        val = ev(ast.parse(str(args["expression"]), mode="eval").body)
        return {"output": val, "provenance": [{"source": "calc"}], "is_error": False}
    except Exception as e:  # noqa
        return {"output": f"calc error: {e}", "provenance": [], "is_error": True}


async def _web_search(args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        return {"output": "empty query", "provenance": [], "is_error": True}
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=_UA) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": q})
        items, seen = [], set()
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
            url, title = html.unescape(m.group(1)), _strip_html(m.group(2))
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url); items.append({"title": title, "url": url})
            if len(items) >= 6:
                break
        if not items:
            return {"output": "no results", "provenance": [], "is_error": False}
        prov = [{"source": "web_search", "url": it["url"], "title": it["title"],
                 "retrieved_at": time.time()} for it in items]
        return {"output": items, "provenance": prov, "is_error": False}
    except Exception as e:  # noqa
        return {"output": f"search failed: {e}", "provenance": [], "is_error": True}


async def _web_fetch(args: dict) -> dict:
    url = str(args.get("url", "")).strip()
    if not url.startswith("http"):
        return {"output": "url must start with http(s)://", "provenance": [], "is_error": True}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_UA) as c:
            r = await c.get(url)
        title = ""
        tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
        if tm:
            title = _strip_html(tm.group(1))
        body = _strip_html(r.text)[:5000]
        return {"output": {"url": str(r.url), "title": title, "text": body, "status": r.status_code},
                "provenance": [{"source": "web_fetch", "url": str(r.url), "title": title,
                                "retrieved_at": time.time()}], "is_error": False}
    except Exception as e:  # noqa
        return {"output": f"fetch failed: {e}", "provenance": [{"source": "web_fetch", "url": url}], "is_error": True}


async def _weather(args: dict) -> dict:
    place = str(args.get("location", "")).strip() or "Orlando"
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=_UA) as c:
            g = await c.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": place, "count": 1})
            gj = g.json()
            if not gj.get("results"):
                return {"output": f"could not locate '{place}'", "provenance": [], "is_error": True}
            loc = gj["results"][0]
            lat, lon = loc["latitude"], loc["longitude"]
            w = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "auto", "forecast_days": 2})
            wj = w.json()
        cur = wj.get("current", {}); day = wj.get("daily", {})
        code = cur.get("weather_code")
        out = {
            "location": f"{loc.get('name')}, {loc.get('admin1') or loc.get('country_code','')}".strip(", "),
            "now": {"temp_f": cur.get("temperature_2m"), "feels_like_f": cur.get("apparent_temperature"),
                    "conditions": _WMO.get(code, f"code {code}"), "humidity_pct": cur.get("relative_humidity_2m"),
                    "wind_mph": cur.get("wind_speed_10m"), "precip": cur.get("precipitation")},
            "today": {"high_f": (day.get("temperature_2m_max") or [None])[0],
                      "low_f": (day.get("temperature_2m_min") or [None])[0],
                      "precip_chance_pct": (day.get("precipitation_probability_max") or [None])[0]},
        }
        return {"output": out, "provenance": [{"source": "open-meteo", "location": out["location"],
                                               "retrieved_at": time.time()}], "is_error": False}
    except Exception as e:  # noqa
        return {"output": f"weather failed: {e}", "provenance": [], "is_error": True}


# ── Anthropic tool schemas (the model sees these) ────────────────────────────
TOOL_SCHEMAS = [
    {"name": "get_datetime", "description": "Get the current local date and time. Use for anything time-relative (today, now, this week).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "web_search", "description": "Search the public web for fresh information (news, prices, facts, current events). Returns titles + URLs. Follow up with web_fetch to read a page.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "web_fetch", "description": "Open a specific web page (URL) and return its readable text. Use to read a site, follow a link, or verify a claim. Keep source URL for citation.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "get_weather", "description": "Get current conditions and today's forecast for a place. Live data.",
     "input_schema": {"type": "object", "properties": {"location": {"type": "string", "description": "City or place, e.g. 'Orlando'"}}, "required": ["location"]}},
    {"name": "calculate", "description": "Evaluate an arithmetic expression exactly.",
     "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
]

_DISPATCH = {"get_datetime": _now, "web_search": _web_search, "web_fetch": _web_fetch,
             "get_weather": _weather, "calculate": _calculate}


async def run_tool(name: str, args: dict) -> dict:
    """Execute a real tool; always returns a deterministic receipt (never raises)."""
    fn = _DISPATCH.get(name)
    if not fn:
        return {"output": f"unknown tool '{name}'", "provenance": [], "is_error": True}
    try:
        return await fn(args or {})
    except Exception as e:  # noqa
        return {"output": f"tool error: {e}", "provenance": [], "is_error": True}
