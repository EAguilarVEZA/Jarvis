"""
research_gateway — provider-neutral web research (Phase 2 P0).

Tavily is the primary provider (REAL when TAVILY_API_KEY is set). DuckDuckGo HTML scraping is
demoted to a last-resort fallback only when no key is present. Retrieved web content is DATA, never
trusted instruction: it is sanitized at THIS boundary (prompt-injection defense) and carries
provenance (url / title / published date / retrieved_at) so the UI can cite sources.

Exposed to the model through the ToolGateway as the web_search / web_fetch tools, so Tavily results
pass through the same receipt / timeout / cancellation / governance path as every other tool.
"""
from __future__ import annotations

import os
import re
import time

import httpx
import agent_tools                       # DDG fallback + generic fetch reuse

# Instruction-like patterns in fetched content are neutralized (content is data, not commands).
_INJECTION = re.compile(
    r"(?is)("
    r"(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?\b(?:instructions|prompts?|rules|context)"
    r"|system prompt|you are now|new instructions?\s*:|as an ai language model|do anything now|"
    r"\bDAN\b|jailbreak|reveal (?:your |the )?(?:system )?(?:prompt|secrets?|instructions))")


def sanitize(text: str) -> str:
    return _INJECTION.sub("[filtered-instruction]", text or "")


class ResearchGateway:
    def __init__(self):
        self.tavily_key = os.getenv("TAVILY_API_KEY", "")
        self.provider = os.getenv("SEARCH_PROVIDER", "tavily" if self.tavily_key else "duckduckgo")

    def active_provider(self) -> str:
        return "tavily" if (self.provider == "tavily" and self.tavily_key) else "duckduckgo"

    async def search(self, args: dict) -> dict:
        q = str((args or {}).get("query", "")).strip()
        if not q:
            return {"output": "empty query", "provenance": [], "is_error": True}
        if self.active_provider() == "tavily":
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.post("https://api.tavily.com/search", json={
                        "api_key": self.tavily_key, "query": q, "max_results": 6,
                        "include_answer": True, "search_depth": "basic"})
                    r.raise_for_status()
                    d = r.json()
                items, prov = [], []
                for it in d.get("results", []):
                    title = sanitize(it.get("title", ""))
                    url = it.get("url", "")
                    snippet = sanitize(it.get("content", ""))[:800]
                    pub = it.get("published_date")
                    items.append({"title": title, "url": url, "snippet": snippet, "published": pub})
                    prov.append({"source": "tavily", "url": url, "title": title,
                                 "published": pub, "retrieved_at": time.time()})
                out = {"provider": "tavily", "answer": sanitize(d.get("answer") or ""), "results": items}
                if not items:            # Tavily returned 200 but nothing → degrade to DDG rather than fail
                    fb = await agent_tools._web_search({"query": q})
                    if not fb.get("is_error"):
                        return fb
                return {"output": out, "provenance": prov, "is_error": False}
            except httpx.HTTPStatusError as e:      # surface the real status + body for diagnosis
                body = ""
                try: body = e.response.text[:200]
                except Exception: pass
                # resilience: fall back to DDG so web search still works, but report the tavily error
                fb = await agent_tools._web_search({"query": q})
                if not fb.get("is_error"):
                    fb.setdefault("output", {})
                    return {"output": fb["output"], "provenance": fb.get("provenance", []),
                            "is_error": False, "tavily_error": f"{e.response.status_code}: {body}"}
                return {"output": f"tavily {e.response.status_code}: {body}", "provenance": [], "is_error": True}
            except Exception as e:  # noqa
                fb = await agent_tools._web_search({"query": q})
                if not fb.get("is_error"):
                    return fb
                return {"output": f"tavily search failed: {e}", "provenance": [], "is_error": True}
        # fallback only when no Tavily key
        rec = await agent_tools._web_search({"query": q})
        if isinstance(rec.get("output"), list):
            for it in rec["output"]:
                it["title"] = sanitize(it.get("title", ""))
        return rec

    async def fetch(self, args: dict) -> dict:
        url = str((args or {}).get("url", "")).strip()
        if not url.startswith("http"):
            return {"output": "url must start with http(s)://", "provenance": [], "is_error": True}
        if self.active_provider() == "tavily":
            try:
                async with httpx.AsyncClient(timeout=20.0) as c:
                    r = await c.post("https://api.tavily.com/extract",
                                     json={"api_key": self.tavily_key, "urls": [url]})
                    r.raise_for_status()
                    d = r.json()
                res = d.get("results") or []
                if res:
                    text = sanitize(res[0].get("raw_content") or "")[:5000]
                    return {"output": {"url": url, "text": text},
                            "provenance": [{"source": "tavily_extract", "url": url,
                                            "retrieved_at": time.time()}], "is_error": False}
            except Exception:  # noqa
                pass  # fall through to generic fetch
        rec = await agent_tools._web_fetch({"url": url})
        if isinstance(rec.get("output"), dict) and "text" in rec["output"]:
            rec["output"]["text"] = sanitize(rec["output"]["text"])
        return rec


RESEARCH = ResearchGateway()
