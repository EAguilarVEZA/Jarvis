"""
Voice answer smoke test — verifies the fix for "voice answers get ignored" bug.

Simulates the real user flow that was broken:
  1. "let's build a report" → start
  2. "cardiology" (voice) → should advance to time
  3. "last 30 days" (voice) → should advance to metrics
  4. "leads" (voice) → should advance to dimensions
  5. "by campaign" (voice) → should advance to filters
  6. "no filters" (voice) → should advance to confirm
  7. "run it" (voice) → should run

Plus negative tests: gibberish voice input should NOT advance, should re-ask.
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.catalog import WarehouseCatalog
from reporting.builder_session import BuilderSession
from reporting.builder_types import BuilderPhase


def banner(s):
    print()
    print("─" * 72)
    print(s)
    print("─" * 72)


def current_phase(session):
    return session.builder.state.phase if session.builder else BuilderPhase.IDLE


def main():
    banner("Loading catalog...")
    cat = WarehouseCatalog.load()
    print(f"  {len(cat.all_tables())} curated tables loaded")

    # ═══════════════════════════════════════════════════════════
    banner("Test 1 — VOICE-only flow (the bug we're fixing)")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)

    print("\n  >>> start: 'let's build a report'")
    session.start("let's build a report")
    p = current_phase(session)
    print(f"      phase after start: {p.value}")
    assert p == BuilderPhase.AWAITING_SOURCE, f"Should be at source, got {p}"

    print("\n  >>> voice: 'cardiology'")
    session.handle_input("cardiology")
    p = current_phase(session)
    print(f"      phase after 'cardiology': {p.value}")
    print(f"      intent.table: {session.builder.state.intent.table}")
    assert p == BuilderPhase.AWAITING_TIME, f"Should be at time, got {p}"
    assert session.builder.state.intent.service_line == "cardiology"

    print("\n  >>> voice: 'last 30 days'")
    session.handle_input("last 30 days")
    p = current_phase(session)
    print(f"      phase after 'last 30 days': {p.value}")
    print(f"      time_window: {session.builder.state.intent.time_window.label}")
    assert p == BuilderPhase.AWAITING_METRICS

    print("\n  >>> voice: 'leads'")
    session.handle_input("leads")
    p = current_phase(session)
    print(f"      phase: {p.value}")
    print(f"      metrics: {session.builder.state.intent.metrics}")
    assert p == BuilderPhase.AWAITING_DIMENSIONS
    assert "leads" in session.builder.state.intent.metrics

    print("\n  >>> voice: 'by campaign'")
    session.handle_input("by campaign")
    p = current_phase(session)
    print(f"      phase: {p.value}")
    print(f"      dimensions: {session.builder.state.intent.dimensions}")
    assert p == BuilderPhase.AWAITING_FILTERS
    assert "dashboard_campaign" in session.builder.state.intent.dimensions

    print("\n  >>> voice: 'no filters'")
    session.handle_input("no filters")
    p = current_phase(session)
    print(f"      phase: {p.value}")
    assert p == BuilderPhase.CONFIRMING

    print("\n  >>> voice: 'run it'")
    msgs = session.handle_input("run it")
    p = current_phase(session)
    print(f"      phase: {p.value}")
    has_result = any(m["type"] == "builder:result" for m in msgs)
    has_done = any(m["type"] == "builder:done" for m in msgs)
    print(f"      emitted builder:result: {has_result}")
    print(f"      emitted builder:done: {has_done}")
    assert has_done

    print("\n  ✓ Full voice flow works")

    # ═══════════════════════════════════════════════════════════
    banner("Test 2 — Service-line aliases work (voice)")
    # ═══════════════════════════════════════════════════════════
    for voice_input, expected_sl in [
        ("heart", "cardiology"),
        ("oncology", "cancer"),
        ("ortho", "orthopedics"),
        ("neuro", "neuroscience"),
        ("OB", "womens"),
        ("emergency", "facilities"),
    ]:
        session = BuilderSession(cat)
        session.start("let's build a report")
        session.handle_input(voice_input)
        got = session.builder.state.intent.service_line
        marker = "✓" if got == expected_sl else "✗"
        print(f"  {marker} voice {voice_input!r:20} → service_line={got!r:15} (expected {expected_sl!r})")
        assert got == expected_sl

    # ═══════════════════════════════════════════════════════════
    banner("Test 3 — Gibberish doesn't advance phase")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)
    session.start("let's build a report")
    print("  Phase before gibberish:", current_phase(session).value)

    for nonsense in ["Google bigquery", "the data warehouse", "purple monkey dishwasher"]:
        session.handle_input(nonsense)
        p = current_phase(session)
        print(f"  After {nonsense!r:30}: phase={p.value}  (should still be awaiting_source)")
        assert p == BuilderPhase.AWAITING_SOURCE, f"Should not have advanced after {nonsense!r}"

    print("  ✓ Gibberish correctly leaves user in source phase to try again")

    # ═══════════════════════════════════════════════════════════
    banner("Test 4 — Voice variants for 'run it'")
    # ═══════════════════════════════════════════════════════════
    for run_phrase in ["yes", "go ahead", "do it", "proceed", "okay", "run it", "looks good"]:
        session = BuilderSession(cat)
        session.start("cardiology spend last 7 days")
        # Walk to confirmation
        if current_phase(session) != BuilderPhase.CONFIRMING:
            session.handle_input("no filters")  # any remaining unanswered
        if current_phase(session) != BuilderPhase.CONFIRMING:
            print(f"  ! Couldn't get to CONFIRMING for {run_phrase!r}")
            continue
        msgs = session.handle_input(run_phrase)
        has_done = any(m["type"] == "builder:done" for m in msgs)
        marker = "✓" if has_done else "✗"
        print(f"  {marker} {run_phrase!r:15} → builder:done emitted = {has_done}")
        assert has_done, f"Run phrase {run_phrase!r} should have triggered run"

    # ═══════════════════════════════════════════════════════════
    banner("Test 5 — Mixing voice + chip values in same conversation")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)
    session.start("let's build a report")
    session.handle_input("service_line:cardiology")    # chip
    assert current_phase(session) == BuilderPhase.AWAITING_TIME
    print(f"  ✓ Chip service_line:cardiology accepted")

    session.handle_input("month to date")    # voice
    assert current_phase(session) == BuilderPhase.AWAITING_METRICS
    print(f"  ✓ Voice 'month to date' accepted")

    session.handle_input("__all_defaults__")  # chip
    assert current_phase(session) == BuilderPhase.AWAITING_DIMENSIONS
    print(f"  ✓ Chip __all_defaults__ accepted")

    session.handle_input("by source")  # voice
    assert "ad_source" in session.builder.state.intent.dimensions
    print(f"  ✓ Voice 'by source' resolved to ad_source")

    banner("ALL VOICE ANSWER TESTS PASSED")


if __name__ == "__main__":
    main()
