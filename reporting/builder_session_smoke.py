"""
End-to-end test of BuilderSession — simulates the server.py dispatch flow.

Verifies:
  1. is_report_trigger fires on build phrases, not on existing TRIGGER_PHRASES
  2. BuilderSession.start emits builder:state + builder:question
  3. Each subsequent input produces sensible WebSocket messages
  4. Cancel phrase exits builder mode cleanly
  5. Run completes with builder:result + builder:done
  6. After done, in_builder_mode goes False — subsequent input bypasses builder
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.catalog import WarehouseCatalog
from reporting.builder_session import BuilderSession, is_cancel_phrase
from reporting.intent_extractor import is_report_trigger


def banner(s):
    print()
    print("─" * 72)
    print(s)
    print("─" * 72)


def show_msgs(msgs):
    """Pretty-print the list of message dicts the session returned."""
    for i, m in enumerate(msgs):
        typ = m.get("type", "?")
        if typ == "builder:state":
            intent = m["state"]["intent"]
            print(f"  [{i}] {typ}")
            print(f"        phase={m['state']['phase']}")
            print(f"        table={intent.get('table')}")
            print(f"        metrics={intent.get('metrics')}")
            print(f"        dimensions={intent.get('dimensions')}")
            print(f"        filters={intent.get('filters')}")
        elif typ == "builder:question":
            print(f"  [{i}] {typ}  phase={m['phase']}")
            print(f"        prompt: {m['prompt'][:60]}")
            opts = m.get("options", [])
            if opts:
                chips = ", ".join(o["label"] for o in opts[:4])
                print(f"        chips: {chips} ({len(opts)} total)")
            if m.get("_spoken"):
                print(f"        _spoken: {m['_spoken'][:60]!r}")
        elif typ == "builder:result":
            print(f"  [{i}] {typ}  rows={m.get('row_count')}")
            if m.get("sql"):
                sql_line = m["sql"].split("\n")[0]
                print(f"        sql: {sql_line}...")
        elif typ == "builder:done":
            print(f"  [{i}] {typ}  reason={m.get('reason')}  _spoken={m.get('_spoken')!r}")
        elif typ == "builder:error":
            print(f"  [{i}] {typ}  message={m.get('message')!r}")
        else:
            print(f"  [{i}] {typ}  {m}")


def main():
    banner("Loading catalog (no BQ runner — generate SQL only)")
    cat = WarehouseCatalog.load()
    print(f"  Loaded {len(cat.all_tables())} curated tables")

    # ═══════════════════════════════════════════════════════════
    banner("Test 1 — is_report_trigger distinguishes from TRIGGER_PHRASES")
    # ═══════════════════════════════════════════════════════════
    cases = [
        # (phrase, expected)
        ("let's build a report", True),
        ("build me a report on cardiology", True),
        ("create a report", True),
        ("start a new report", True),
        ("show me the dashboard", False),
        ("marketing report", False),
        ("how are we performing", False),
        ("hello jarvis", False),
        ("cancel", False),
    ]
    for phrase, expected in cases:
        got = is_report_trigger(phrase)
        marker = "✓" if got == expected else "✗"
        print(f"  {marker} is_report_trigger({phrase!r}) = {got}  (expected {expected})")
        assert got == expected

    # ═══════════════════════════════════════════════════════════
    banner("Test 2 — Cancel phrase detection")
    # ═══════════════════════════════════════════════════════════
    for phrase, expected in [
        ("cancel", True),
        ("cancel that", True),
        ("never mind", True),
        ("um, cancel", True),
        ("nevermind that report", False),  # close but not in our list
        ("hello", False),
    ]:
        got = is_cancel_phrase(phrase)
        marker = "✓" if got == expected else "✗"
        print(f"  {marker} is_cancel_phrase({phrase!r}) = {got}")
        assert got == expected

    # ═══════════════════════════════════════════════════════════
    banner("Test 3 — Full conversation: vague start, walk all chips, run")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)  # no query_runner = SQL preview only
    assert not session.in_builder_mode

    print("\n  >>> start: 'let's build a report'")
    msgs = session.start("let's build a report")
    show_msgs(msgs)
    assert session.in_builder_mode

    # Walk through chips
    for picks in ["service_line:cardiology", "last_30d", "__all_defaults__",
                  "dashboard_campaign", "__none__"]:
        print(f"\n  >>> handle_input: {picks!r}")
        msgs = session.handle_input(picks)
        show_msgs(msgs)

    # Final: run it
    print(f"\n  >>> handle_input: '__run__'")
    msgs = session.handle_input("__run__")
    show_msgs(msgs)

    # After run completes (no runner = preview), session should drop out of builder mode
    assert not session.in_builder_mode
    # Last message should be builder:done with reason=complete
    last = msgs[-1]
    assert last["type"] == "builder:done"
    assert last["reason"] == "complete"
    print("\n  ✓ Test 3 passed (full happy path)")

    # ═══════════════════════════════════════════════════════════
    banner("Test 4 — Pre-filled request skips ahead but still asks unspecified pieces")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)
    print("\n  >>> start: 'cardiology spend last 7 days by campaign'")
    msgs = session.start("cardiology spend last 7 days by campaign")
    show_msgs(msgs)
    assert session.in_builder_mode
    last_q = next((m for m in msgs if m["type"] == "builder:question"), None)
    assert last_q is not None
    # Source, time, metric, dimension all filled — only filters left to ask
    assert last_q["phase"] == "awaiting_filters", f"Expected awaiting_filters, got {last_q['phase']}"
    print(f"\n  ✓ Pre-filled skipped to filters question (last unspecified piece)")

    # Skip filters, land at confirm
    msgs = session.handle_input("__none__")
    last_q = next((m for m in msgs if m["type"] == "builder:question"), None)
    assert last_q["phase"] == "confirming"
    print(f"  ✓ Landed at confirmation after filter answer")

    # Now cancel via the cancel-phrase route
    print(f"\n  >>> handle_input: 'cancel'")
    msgs = session.handle_input("cancel")
    show_msgs(msgs)
    assert not session.in_builder_mode
    assert msgs[-1]["type"] == "builder:done"
    assert msgs[-1]["reason"] == "cancelled"
    print(f"\n  ✓ Cancel works")

    # ═══════════════════════════════════════════════════════════
    banner("Test 5 — Validation error surfaces as builder:error")
    # ═══════════════════════════════════════════════════════════
    session = BuilderSession(cat)
    print("\n  >>> start: 'cardiology spend last quarter by campaign, non-brand only'")
    # This routes to .leads which doesn't have cost — validation should fail at run
    msgs = session.start("cardiology spend last quarter by campaign, non-brand only")
    show_msgs(msgs)
    msgs = session.handle_input("__run__")
    show_msgs(msgs)
    errors = [m for m in msgs if m["type"] == "builder:error"]
    assert errors, "Should have emitted a builder:error"
    print(f"\n  ✓ Validation error surfaced cleanly")
    assert not session.in_builder_mode

    banner("ALL BUILDER SESSION TESTS PASSED")


if __name__ == "__main__":
    main()
