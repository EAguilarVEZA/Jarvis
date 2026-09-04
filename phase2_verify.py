#!/usr/bin/env python3
"""
phase2_verify.py — REAL end-to-end evidence harness (run on the machine with the keys).

    cd ~/Jarvis && venv/bin/python3 phase2_verify.py    # (or: python3 phase2_verify.py)

Proves, against the ACTUAL services (no mocks): live providers, an Anthropic turn, an OpenAI turn in
the SAME conversation with context intact, a tool-calling OpenAI turn, a switch back to Anthropic,
a FORCED Anthropic failure that really falls back to OpenAI, a live Tavily search with source URLs,
and an OpenAI Realtime session mint. Writes phase2_evidence.md with per-test evidence.
"""
import os
import sys
import time
import json
import asyncio
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load_env():
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

REPORT = []
def log(s=""):
    print(s); REPORT.append(str(s))


def rec(name, provider, endpoint, ok, ms, mock, limit=""):
    log(f"\n## {name}")
    log(f"- provider used: {provider}")
    log(f"- endpoint/path: {endpoint}")
    log(f"- result: {'PASS' if ok else 'FAIL'}")
    log(f"- latency_ms: {ms}")
    log(f"- mock/fallback involved: {mock}")
    log(f"- limitation: {limit or 'none'}")
    return ok


async def main():
    have_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    have_openai = bool(os.getenv("OPENAI_API_KEY"))
    have_tavily = bool(os.getenv("TAVILY_API_KEY"))
    log("# Phase 2 Evidence — real credential run")
    log(f"_generated: {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    log(f"- keys present: anthropic={have_anthropic} openai={have_openai} tavily={have_tavily}")

    import conversation_runtime as CR
    from model_gateway import Router, AnthropicAdapter, OpenAIAdapter
    from research_gateway import RESEARCH
    rt = CR.RUNTIME
    log(f"- live providers: {rt.router.names()} | search provider: {rt.research.active_provider()}")
    cid = "verify-" + str(int(time.time()))
    passes, total = 0, 0

    def tally(ok):
        nonlocal passes, total
        total += 1; passes += 1 if ok else 0

    # 1 — Anthropic turn
    t = time.time(); r = await rt.run_turn(cid, "In one short sentence, who are you?", provider="anthropic")
    tally(rec("Anthropic turn", r.provider, "api.anthropic.com messages.create",
              r.provider == "anthropic" and bool(r.answer), int((time.time()-t)*1000), "no"))
    log(f"  answer: {r.answer[:180]}")

    # seed a fact for the context-continuity checks
    await rt.run_turn(cid, "Remember that my favorite service line is cardiology.", provider="anthropic")

    if have_openai:
        # 2 — OpenAI turn in the SAME conversation, context intact
        t = time.time(); r = await rt.run_turn(cid, "Which service line did I just say is my favorite?", provider="openai")
        ok = r.provider == "openai" and "cardiolog" in r.answer.lower()
        tally(rec("OpenAI turn — same conversation, context intact", r.provider,
                  "api.openai.com/v1/chat/completions", ok, int((time.time()-t)*1000), "no",
                  "" if ok else "provider/context mismatch"))
        log(f"  answer: {r.answer[:180]}")

        # 3 — tool-capable OpenAI turn
        t = time.time(); r = await rt.run_turn(cid, "Use a tool to compute 1234 multiplied by 7.", provider="openai")
        ok = r.provider == "openai" and ("8638" in r.answer or "calculate" in r.tools_used)
        tally(rec("OpenAI tool-calling turn", r.provider,
                  "openai chat/completions + ToolGateway.calculate", ok, int((time.time()-t)*1000), "no"))
        log(f"  tools_used: {r.tools_used} | answer: {r.answer[:140]}")

    # 4 — switch back to Anthropic, context still intact
    t = time.time(); r = await rt.run_turn(cid, "Remind me what my favorite service line is.", provider="anthropic")
    ok = r.provider == "anthropic" and "cardiolog" in r.answer.lower()
    tally(rec("Switch back to Anthropic — context intact", r.provider, "api.anthropic.com",
              ok, int((time.time()-t)*1000), "no", "" if ok else "context lost on switch"))
    log(f"  answer: {r.answer[:180]}")

    if have_openai:
        # 5 — FORCED Anthropic failure -> REAL fallback to OpenAI (same turn actually completes)
        rt2 = CR.ConversationRuntime()
        rt2.router = Router([AnthropicAdapter(model="claude-nonexistent-model-xyz"), OpenAIAdapter()])
        t = time.time(); r = await rt2.run_turn("fb-" + cid, "Reply with a one-word greeting.")
        ok = r.fell_back and r.provider == "openai" and bool(r.answer)
        tally(rec("Forced Anthropic failure -> real OpenAI fallback", r.provider,
                  "anthropic(invalid model, fails) -> openai chat/completions", ok,
                  int((time.time()-t)*1000), "fallback executed for real (not simulated)",
                  "" if ok else "fallback did not actually execute"))
        log(f"  fell_back={r.fell_back} provider={r.provider} answer={r.answer[:80]}")

    if have_tavily:
        # 6 — Tavily LIVE search with real source URLs (proves live provider, not model memory)
        t = time.time(); res = await RESEARCH.search({"query": "top technology news headline today"})
        urls = [p.get("url") for p in res.get("provenance", []) if p.get("url")]
        _out = res.get("output")
        _pin = _out.get("provider") if isinstance(_out, dict) else _out       # guard: output may be an error string
        ok = (not res.get("is_error")) and len(urls) >= 1
        note = "" if ok else ("error: " + str(_out)[:160])
        if res.get("tavily_error"):
            note = (note + " | tavily_error: " + str(res["tavily_error"])[:160]).strip(" |")
        tally(rec("Tavily live web search", "tavily", "https://api.tavily.com/search", ok,
                  int((time.time()-t)*1000), "no" if not res.get("tavily_error") else "DDG fallback used", note))
        log(f"  provider_in_output: {_pin} | live_urls: {urls[:3]}")

        # 7 — model turn that actually uses the live web tool and returns citations
        t = time.time(); r = await rt.run_turn(cid, "Search the web for one current news headline and cite the source URL.")
        ok = bool(r.citations)
        tally(rec("Model turn using the live web tool", r.provider,
                  "model + ToolGateway.web_search(Tavily)", ok, int((time.time()-t)*1000), "no"))
        log(f"  citations: {r.citations[:3]}")

    if have_openai:
        # 8 — OpenAI Realtime ephemeral session mint (the voice foundation credential test)
        import realtime_openai as RO
        t = time.time(); sess = await RO.session()
        ok = bool(sess.get("ok") and sess.get("client_secret"))
        tally(rec("OpenAI Realtime session mint", "openai-realtime",
                  "https://api.openai.com/v1/realtime/sessions", ok, int((time.time()-t)*1000), "no",
                  "" if ok else str(sess.get("error"))[:160]))

    # 9 — Orlando Health governed data tool (real BigQuery via semantic layer)
    t = time.time()
    rec9 = await rt.tools.invoke("orlando_health_data",
                                 {"question": "How has total ad spend trended month over month across all campaigns?"})
    oh = rec9.get("output")
    ok = (not rec9.get("is_error")) and isinstance(oh, dict) and (oh.get("row_count", 0) > 0 or bool(oh.get("columns")))
    tally(rec("Orlando Health data tool (real BigQuery)", "orlando_health_data",
              "semantic_api.ask -> curated semantic layer -> BigQuery", ok,
              int((time.time()-t)*1000), "no", "" if ok else ("error: " + str(oh)[:200])))
    log(f"  row_count: {isinstance(oh, dict) and oh.get('row_count')} | columns: {isinstance(oh, dict) and (oh.get('columns') or [])[:6]}")

    from obs_timing import METRICS
    log("\n## Measured latency this run (real)\n```json")
    log(json.dumps(METRICS.report().get("stages", {}), indent=2))
    log("```")

    log(f"\n# SUMMARY: {passes}/{total} checks PASSED")
    out = HERE / "phase2_evidence.md"
    out.write_text("\n".join(REPORT))
    log(f"\nEvidence written to {out}")
    return 0 if passes == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
