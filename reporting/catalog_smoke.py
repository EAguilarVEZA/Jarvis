"""
Smoke test for the WarehouseCatalog.

Run from CLI:  python -m reporting.catalog_smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent to path so we can import as a module
sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.catalog import WarehouseCatalog, SERVICE_LINES, find_service_line


def banner(s: str) -> None:
    print()
    print("─" * 70)
    print(s)
    print("─" * 70)


def main() -> int:
    banner("Loading catalog...")
    cat = WarehouseCatalog.load()
    summary = cat.summarize()
    print(f"  Curated tables:            {summary['curated_tables']}")
    print(f"  Sources:                   {list(summary['sources'].keys())}")
    print(f"  Tables per source:")
    for src, n in summary["sources"].items():
        print(f"    {src:30}  {n}")
    print(f"  Total warehouse datasets:  {summary['total_warehouse_datasets']}")
    print(f"  Total warehouse tables:    {summary['total_warehouse_tables']:,}")

    banner("Test 1: Get a known service-line table")
    t = cat.get("data_mart_cardiology.combined_campaign_stats")
    assert t is not None, "Should find cardiology table"
    print(f"  Found: {t.full_name}")
    print(f"  Label:        {t.label}")
    print(f"  Purpose:      {t.purpose}")
    print(f"  Row count:    {t.row_count:,}")
    print(f"  Time column:  {t.time_column}")
    print(f"  Dimensions:   {[c.name for c in t.dimensions()]}")
    print(f"  Metrics:      {[c.name for c in t.metrics()]}")
    print(f"  Derived:      {[dm.name for dm in t.derived_metrics]}")
    print(f"  Filters:      {[c.name for c in t.filters()]}")

    banner("Test 2: Get the cross-channel event-level leads table")
    t = cat.get("data_mart_all.leads")
    assert t is not None
    print(f"  Found: {t.full_name}")
    print(f"  Purpose: {t.purpose}")
    print(f"  Warnings:")
    for w in t.warnings:
        print(f"    ! {w}")
    print(f"  Derived metrics: {[(dm.name, dm.expr) for dm in t.derived_metrics]}")

    banner("Test 3: Get GA4 events — should have date-filter requirement")
    t = cat.get("gold.ga4_fact_events")
    assert t is not None
    print(f"  Found: {t.full_name}")
    print(f"  Row count:           {t.row_count:,}")
    print(f"  require_date_filter: {t.require_date_filter}")
    print(f"  Warnings:")
    for w in t.warnings:
        print(f"    ! {w}")

    banner("Test 4: Get GSC — should expose `page_hostname` as dimension for cross-domain views")
    t = cat.get("gold.gsc_fact_site_impression")
    assert t is not None
    hostname_col = next((c for c in t.columns if c.name == "page_hostname"), None)
    print(f"  Found: {t.full_name}")
    print(f"  Row count: {t.row_count:,}")
    assert hostname_col, "`page_hostname` column should be curated for cross-domain views"
    print(f"  `page_hostname` column: role={hostname_col.role}, label={hostname_col.label}")
    assert hostname_col.role == "dimension"

    banner("Test 5: search() — find leads-related tables")
    hits = cat.search("cardiology leads", limit=5)
    print(f"  Query: 'cardiology leads'")
    for h in hits:
        print(f"    [{h.source:25}] {h.full_name}  ({h.row_count:,} rows)")

    banner("Test 6: search() — find budget owner reporting")
    hits = cat.search("budget owner", limit=3)
    print(f"  Query: 'budget owner'")
    for h in hits:
        print(f"    [{h.source:25}] {h.full_name}")

    banner("Test 7: validate() — good query")
    result = cat.validate({
        "table": "data_mart_cardiology.combined_campaign_stats",
        "metrics": ["leads", "cost", "cpa"],
        "dimensions": ["dashboard_campaign", "ad_source"],
        "time_window": {"column": "date", "start": "2026-04-01", "end": "2026-04-30"},
    })
    print(f"  OK: {result.ok}")
    print(f"  Errors:   {result.errors}")
    print(f"  Warnings: {result.warnings}")
    assert result.ok, "Good query should validate"

    banner("Test 8: validate() — unknown metric")
    result = cat.validate({
        "table": "data_mart_cardiology.combined_campaign_stats",
        "metrics": ["revenue"],  # not in this table
    })
    print(f"  OK: {result.ok}")
    print(f"  Errors:   {result.errors[0] if result.errors else 'none'}")
    assert not result.ok, "Should reject unknown metric"

    banner("Test 9: validate() — large table without date filter")
    result = cat.validate({
        "table": "gold.ga4_fact_events",
        "metrics": ["event_count"],
    })
    print(f"  OK: {result.ok}")
    print(f"  Errors:")
    for e in result.errors:
        print(f"    ! {e}")
    assert not result.ok, "Should reject GA4 query without date filter"

    banner("Test 10: validate() — unknown table with suggestion")
    result = cat.validate({
        "table": "data_mart_cardiology.campaign_stats",  # close but wrong
    })
    print(f"  OK: {result.ok}")
    for e in result.errors:
        print(f"    ! {e}")

    banner("Test 11: Original ServiceLine catalog still works")
    sl = find_service_line("how's our heart business doing")
    print(f"  Matched 'heart' to: {sl.label if sl else None}")
    assert sl and sl.key == "cardiology"

    banner("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
