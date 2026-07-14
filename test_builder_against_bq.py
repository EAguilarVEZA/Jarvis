"""
End-to-end test of the report builder against the real BigQuery warehouse.

Two queries:
  1. Cardiology spend last 7 days, no breakdown (simplest possible — single row)
  2. Cardiology spend last 7 days by campaign (adds GROUP BY + ORDER BY + LIMIT)

Both go through the full builder pipeline: parse intent → build state → confirm → run.
"""

from __future__ import annotations

from reporting.bq_client import BQClient
from reporting.catalog import WarehouseCatalog
from reporting.builder import ReportBuilder
from reporting.builder_types import BuilderPhase


def run_through_builder(builder, opening_msg):
    """Walk the builder from opening message to RUN, picking first option at each step."""
    print(f"\n  >>> Opening: {opening_msg!r}")
    r = builder.start(opening_msg)
    while r.state.phase not in (BuilderPhase.CONFIRMING, BuilderPhase.COMPLETE, BuilderPhase.IDLE):
        opts = r.state.pending.options if r.state.pending else []
        chosen = next((o.value for o in opts if o.is_recommended), opts[0].value if opts else "")
        print(f"  >>> picking: {chosen}")
        r = builder.answer({"value": chosen})

    if r.state.phase != BuilderPhase.CONFIRMING:
        print(f"  ERROR: did not reach confirmation. phase={r.state.phase.value}")
        return None

    # Show what we're about to run
    print(f"\n  Intent at confirmation:")
    print(f"    table:      {r.state.intent.table}")
    print(f"    metrics:    {r.state.intent.metrics}")
    print(f"    dimensions: {[d for d in r.state.intent.dimensions if d != '__none__']}")
    print(f"    filters:    {[(f.column, f.op) for f in r.state.intent.filters]}")
    print(f"    time:       {r.state.intent.time_window.start} → {r.state.intent.time_window.end}")

    # Run it
    print(f"\n  >>> Running...")
    r = builder.answer({"value": "__run__"})

    if r.state.error:
        print(f"  ERROR: {r.state.error}")
        return None

    return r


def main():
    print("Loading catalog + BigQuery client...")
    cat = WarehouseCatalog.load()
    bq = BQClient()

    def runner(sql, params):
        """Adapter: builder expects rows (list[dict]); BQClient returns QueryResult."""
        qr = bq.query(sql, params)
        print(f"  >>> BQ: {qr.bytes_billed:,} bytes billed, {qr.elapsed_ms} ms, cache={qr.cache_hit}")
        return qr.rows

    builder = ReportBuilder(cat, query_runner=runner)

    # ─── Test 1: simple — last 7 days, no breakdown ───
    print("\n" + "═" * 70)
    print("TEST 1 — Cardiology spend, last 7 days, no breakdown")
    print("═" * 70)
    r = run_through_builder(builder, "cardiology spend last 7 days")
    if r and r.state.last_result:
        print(f"\n  SQL:")
        for line in r.state.last_result["sql"].split("\n"):
            print(f"    {line}")
        print(f"\n  PARAMS: {r.state.last_result['params']}")
        rows = r.state.last_result.get("rows", [])
        print(f"\n  ROWS RETURNED: {len(rows)}")
        for row in rows[:5]:
            print(f"    {row}")

    # ─── Test 2: with GROUP BY ───
    print("\n" + "═" * 70)
    print("TEST 2 — Cardiology spend, last 7 days, by campaign")
    print("═" * 70)
    builder2 = ReportBuilder(cat, query_runner=runner)
    r = run_through_builder(builder2, "cardiology spend last 7 days by campaign")
    if r and r.state.last_result:
        print(f"\n  SQL:")
        for line in r.state.last_result["sql"].split("\n"):
            print(f"    {line}")
        print(f"\n  PARAMS: {r.state.last_result['params']}")
        rows = r.state.last_result.get("rows", [])
        print(f"\n  ROWS RETURNED: {len(rows)}")
        for row in rows[:5]:
            print(f"    {row}")

    print("\n" + "═" * 70)
    print("DONE.")
    print("═" * 70)


if __name__ == "__main__":
    main()
