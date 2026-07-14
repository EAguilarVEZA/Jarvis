"""
End-to-end smoke test for the report builder.

Simulates a conversation, asserts the state machine produces sensible
questions, advances correctly, and generates valid SQL.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.catalog import WarehouseCatalog
from reporting.builder import ReportBuilder
from reporting.builder_types import BuilderPhase
from reporting.intent_extractor import is_report_trigger


def banner(s):
    print()
    print("─" * 72)
    print(s)
    print("─" * 72)


def show_response(label, response):
    print(f"  [{label}] phase={response.state.phase.value}")
    if response.state.pending:
        print(f"    PROMPT: {response.state.pending.prompt[:80]}")
        opts = response.state.pending.options[:5]
        if opts:
            chips = ", ".join(o.label for o in opts)
            print(f"    OPTIONS (first 5): {chips}")
    if response.state.error:
        print(f"    ERROR: {response.state.error}")
    if response.done:
        print(f"    DONE.")
        if response.state.last_result and response.state.last_result.get("sql"):
            print(f"    SQL preview:")
            for line in response.state.last_result["sql"].split("\n"):
                print(f"      {line}")
            if response.state.last_result.get("params"):
                print(f"    PARAMS: {response.state.last_result['params']}")


def main():
    banner("Loading catalog...")
    cat = WarehouseCatalog.load()
    print(f"  Loaded {len(cat.all_tables())} curated tables")

    # ═══════════════════════════════════════════════════════════
    # Scenario A — vague initial request, walks all questions
    # ═══════════════════════════════════════════════════════════
    banner("Scenario A — vague initial request")
    b = ReportBuilder(cat)
    r = b.start("Let's build a report")
    show_response("start", r)
    assert r.state.phase == BuilderPhase.AWAITING_SOURCE

    r = b.answer({"value": "service_line:cardiology"})
    show_response("source=cardiology", r)
    assert r.state.phase == BuilderPhase.AWAITING_TIME

    r = b.answer({"value": "last_30d"})
    show_response("time=last_30d", r)
    assert r.state.phase == BuilderPhase.AWAITING_METRICS

    r = b.answer({"value": "__all_defaults__"})
    show_response("metrics=all_defaults", r)
    assert r.state.phase == BuilderPhase.AWAITING_DIMENSIONS

    r = b.answer({"value": "dashboard_campaign"})
    show_response("dim=campaign", r)
    assert r.state.phase == BuilderPhase.AWAITING_FILTERS

    r = b.answer({"value": "__none__"})
    show_response("filters=none", r)
    assert r.state.phase == BuilderPhase.CONFIRMING

    r = b.answer({"value": "__run__"})
    show_response("run", r)
    assert r.done
    assert r.state.last_result is not None
    assert "SELECT" in r.state.last_result["sql"]
    print()
    print("  ✓ Scenario A passed")

    # ═══════════════════════════════════════════════════════════
    # Scenario B — specific request, skips most questions
    # NOTE: this scenario surfaces a known modeling tension. When the user
    # asks for `cost` AND `non-brand only`, the filter routes to .leads (the
    # only table with is_brand_lead) but .leads has no cost column. Phase 2
    # emits a clear validation error; a future enhancement would ask the user
    # how to resolve the conflict.
    # ═══════════════════════════════════════════════════════════
    banner("Scenario B — pre-filled request (with intentional cost/leads conflict)")
    b = ReportBuilder(cat)
    r = b.start("Build me a report on cardiology spend last quarter by campaign, non-brand only")
    show_response("start", r)
    print(f"\n  Intent state after one message:")
    print(f"    table:      {r.state.intent.table}")
    print(f"    metrics:    {r.state.intent.metrics}")
    print(f"    dimensions: {r.state.intent.dimensions}")
    print(f"    filters:    {[(f.column, f.op) for f in r.state.intent.filters]}")
    print(f"    time:       {r.state.intent.time_window.label}")
    assert r.state.phase == BuilderPhase.CONFIRMING
    assert r.state.intent.table.endswith(".leads"), \
        f"Should route to .leads when non-brand filter present, got {r.state.intent.table}"

    r = b.answer({"value": "__run__"})
    show_response("run", r)
    assert r.done
    assert r.state.error, "Should emit validation error (cost not available on .leads)"
    print(f"  ✓ Validation correctly caught the cost-on-leads conflict")

    # ═══════════════════════════════════════════════════════════
    # Scenario B2 — same intent shape but consistent (leads only, no cost)
    # ═══════════════════════════════════════════════════════════
    banner("Scenario B2 — non-brand leads only (no cost), should run cleanly")
    b = ReportBuilder(cat)
    r = b.start("Cardiology leads last 30 days, non-brand only")
    show_response("start", r)
    assert r.state.intent.table == "data_mart_cardiology.leads"
    while r.state.phase not in (BuilderPhase.CONFIRMING, BuilderPhase.IDLE, BuilderPhase.COMPLETE):
        opts = r.state.pending.options
        chosen = next((o.value for o in opts if o.is_recommended or o.is_default), opts[0].value if opts else "")
        r = b.answer({"value": chosen})
    show_response("at confirm", r)
    r = b.answer({"value": "__run__"})
    show_response("run", r)
    assert r.done
    assert not r.state.error, f"Should not error: {r.state.error}"
    print(f"  ✓ Scenario B2 passed")

    # ═══════════════════════════════════════════════════════════
    # Scenario C — keyword query (auto-picks paid_keywords table)
    # ═══════════════════════════════════════════════════════════
    banner("Scenario C — keyword-level report (auto-routes to paid_keywords_w_leads)")
    b = ReportBuilder(cat)
    r = b.start("Top keywords for cardiology last 30 days")
    print(f"\n  Auto-selected table: {r.state.intent.table}")
    assert r.state.intent.table == "data_mart_cardiology.paid_keywords_w_leads"
    # Should be at AWAITING_METRICS (no metrics specified yet)
    show_response("start", r)
    r = b.answer({"value": "leads"})
    show_response("metrics=leads", r)
    r = b.answer({"value": "__none__"})  # filters already past dim? Let's see
    show_response("dim or filters?", r)
    # If filters, answer it
    if r.state.phase == BuilderPhase.AWAITING_FILTERS:
        r = b.answer({"value": "__none__"})
        show_response("filters=none", r)
    r = b.answer({"value": "__run__"})
    show_response("run", r)
    assert r.done
    print()
    print("  ✓ Scenario C passed")

    # ═══════════════════════════════════════════════════════════
    # Scenario D — Defaults carry forward across builds
    # ═══════════════════════════════════════════════════════════
    banner("Scenario D — session defaults carry across builds")
    # Defaults populated from scenario C are now in b.state.session_defaults
    print(f"  Session defaults after Scenario C: {b.state.session_defaults}")
    # Start a fresh build — should inherit defaults
    r = b.start("Build me another report on spend")
    print(f"\n  After 'Build me another report on spend':")
    print(f"    service_line: {r.state.intent.service_line}")
    print(f"    table:        {r.state.intent.table}")
    print(f"    metrics:      {r.state.intent.metrics}")
    print(f"    time:         {r.state.intent.time_window.label}")
    # Should have cardiology + last_30d carried over
    assert r.state.intent.service_line == "cardiology", "Service line should carry forward"
    print()
    print("  ✓ Scenario D passed")

    # ═══════════════════════════════════════════════════════════
    # Scenario E — Edit flow (user wants to change something)
    # ═══════════════════════════════════════════════════════════
    banner("Scenario E — Edit flow (user changes their mind at confirmation)")
    b = ReportBuilder(cat)
    r = b.start("Cardiology spend last 30 days")
    # Some pieces may be missing — finish them
    while r.state.phase not in (BuilderPhase.CONFIRMING, BuilderPhase.IDLE, BuilderPhase.COMPLETE):
        # Pick first option for whatever's pending
        opts = r.state.pending.options
        chosen = next((o.value for o in opts if o.is_recommended or o.is_default), opts[0].value if opts else "")
        r = b.answer({"value": chosen})

    assert r.state.phase == BuilderPhase.CONFIRMING
    print("  Before edit:")
    show_response("confirm", r)

    # User clicks Edit → back to source
    r = b.answer({"value": "__edit__"})
    show_response("edit", r)
    assert r.state.phase == BuilderPhase.AWAITING_SOURCE

    # Pick neuroscience instead
    r = b.answer({"value": "service_line:neuroscience"})
    show_response("changed to neuroscience", r)
    # Walk to confirm (time/metrics already filled from before)
    while r.state.phase not in (BuilderPhase.CONFIRMING, BuilderPhase.IDLE, BuilderPhase.COMPLETE):
        opts = r.state.pending.options
        chosen = next((o.value for o in opts if o.is_recommended or o.is_default), opts[0].value if opts else "")
        r = b.answer({"value": chosen})
    show_response("at confirm again", r)
    assert "neuroscience" in r.state.intent.table
    print()
    print("  ✓ Scenario E passed")

    # ═══════════════════════════════════════════════════════════
    banner("ALL BUILDER TESTS PASSED")


if __name__ == "__main__":
    main()
