"""
ktx_brief_smoke.py — validate the ask-a-question → brief pipeline end to end.

Run from the Jarvis root in your venv (needs ANTHROPIC_API_KEY + ktx running):

    cd ~/Jarvis && source venv/bin/activate
    JARVIS_SQL_ENGINE=ktx python ktx_brief_smoke.py

For each question it: plans the query (Haiku) -> runs it through ktx ->
writes the brief (Sonnet), then prints the result. Read-only.
"""
import asyncio, os, sys, json

QUESTIONS = [
    "How is our ad spend trending over time?",
    "Summarize overall ad performance: spend, clicks, CTR and CPC.",
]


async def main():
    os.environ.setdefault("JARVIS_SQL_ENGINE", "ktx")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — load it from .env or export it."); sys.exit(1)
    from brief_engine import generate_brief

    for q in QUESTIONS:
        print("\n" + "=" * 70)
        print("Q:", q)
        print("=" * 70)
        try:
            b = await generate_brief(q, "2015-01-01", "2016-12-31")
        except Exception as e:
            print("  FAIL:", e); continue
        print("HEADLINE:", b["headline"])
        print("\nFINDINGS:")
        for f in b["findings"]:
            print("  •", f)
        if b.get("narrative"):
            print("\nNARRATIVE:\n ", b["narrative"][:600])
        if b.get("caveats"):
            print("\nCAVEATS:")
            for c in b["caveats"]:
                print("  ⚠", c)
        print("\nSOURCES:", ", ".join(b.get("sources", [])))
        if b.get("chart"):
            print("CHART:", b["chart"]["type"], "of", b["chart"]["y"], "by", b["chart"]["x"],
                  f"({len(b['chart']['labels'])} pts)")
        print("ENGINE:", b.get("engine"), "| queries run:", len(b.get("data", [])))
        # show the compiled SQL of the first query as proof it went through ktx
        if b.get("data"):
            print("SQL[0]:", b["data"][0]["sql"][:160])

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
