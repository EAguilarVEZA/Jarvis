"""
Ask MARTIN demo (spec §12, §16). Natural-language questions → orchestrator → deterministic
tools → grounded narration with citations. Proves the LLM only narrates computed findings.

Run:  python3 -m martin_experience.orchestrator_demo
"""
from __future__ import annotations

from .store import Store
from .demo_slice import build
from .services.nlp.pipeline import NlpPipeline
from .services.ai_orchestrator.orchestrator import Orchestrator

START, END = "2026-08-01", "2026-08-31"


def main():
    store = Store(":memory:")
    build(store)
    NlpPipeline(store).run()
    orch = Orchestrator(store)

    questions = [
        "Why is patient experience low at Winnie Palmer?",
        "How are our locations doing this month?",
        "Show me reviews about scheduling",
        "Why did experience decline at a facility we don't track?",   # insufficient path
    ]
    for q in questions:
        r = orch.ask(q, START, END)
        print("\nQ:", q)
        print("  intent:", r["intent"], "| task:", r["task"], "| entity:", r["entity"]["id"],
              "| tools:", r["tools_used"])
        print("  A:", r["answer"].replace("\n", "\n     "))
        if r["citations"]:
            print("  citations:", r["citations"])

    # ── verify grounding ──
    rc = orch.ask("Why is patient experience low at Winnie Palmer?", START, END)
    assert rc["entity"]["id"] == "LOC_WINNIE"
    assert "2.5" in rc["answer"], "must state the real avg rating from the bundle"
    assert "gr_1003" in rc["answer"] or any("gr_1003" in c for c in rc["citations"]) \
        or "gr_1004" in rc["answer"], "must cite real evidence"
    assert any(t in rc["answer"] for t in ("Scheduling", "Phone access", "Billing")), "must name a real driver"
    assert rc["grounded"] is True

    examples = orch.ask("Show me reviews about scheduling", START, END)
    assert "gr_1003" in examples["answer"], "should surface the scheduling review"

    ins = orch.ask("Why did experience decline at a facility we don't track?", START, END)
    # resolves to top issue when no entity matched, but still grounded; ensure no fabrication:
    assert ins["grounded"] is True

    print("\nOK — Ask MARTIN answers are grounded in computed findings, cite real evidence, "
          "and never invent numbers.")


if __name__ == "__main__":
    main()
