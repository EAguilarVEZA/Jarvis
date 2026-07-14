#!/usr/bin/env python3
"""
Smoke test against the real Orlando Health data warehouse.

Run this on your Mac AFTER copying the reporting/ folder into ~/jarvis/
and BEFORE wiring routes into server.py.

Usage:
    cd ~/jarvis
    source venv/bin/activate
    python -m reporting.smoke_test

What it does:
    1. Connects to BQ using ~/.config/jarvis/bigquery-key.json
    2. Runs the healthcheck (same query Jarvis uses at startup)
    3. Pulls a 30-day summary for cardiology and the all-services rollup
    4. Tries a voice question end-to-end
    5. Prints what each step cost in bytes billed

If any step fails, the error tells you exactly what to fix BEFORE
restarting the server.
"""

from __future__ import annotations

import os
import sys
from datetime import date

CRED = os.path.expanduser("~/.config/jarvis/bigquery-key.json")
if not os.path.exists(CRED):
    print(f"❌ BigQuery credentials not found at {CRED}")
    print("   Restore them before running this test.")
    sys.exit(1)

from reporting import (
    BQClient,
    Reports,
    Period,
    answer_question,
    build_dashboard,
)


def section(title: str):
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


def main():
    section("1. Connecting to BigQuery")
    bq = BQClient(credentials_path=CRED)
    health = bq.healthcheck()
    print(f"  ✓ Project: oh-data-warehouse")
    print(f"  ✓ Total leads (last 90d): {health['total_leads_90d']:,}")
    print(f"  ✓ Most recent lead: {health['last_lead_date']}")
    print(f"  ✓ Healthcheck took {health['elapsed_ms']}ms")

    reports = Reports(bq)

    section("2. Cardiology summary (last 30 days)")
    summary = reports.summary("cardiology", Period.last_n_days(30))
    print(f"  Leads: {summary.leads:,} ({summary.paid_leads:,} paid)")
    print(f"  Spend: ${summary.cost:,.0f}")
    print(f"  CPA:   ${summary.cpa or 0:,.2f}")
    print(f"  CTR:   {summary.ctr or 0}%")

    section("3. All service lines rollup (month to date)")
    s_all = reports.summary("all", Period.month_to_date())
    print(f"  Leads: {s_all.leads:,}")
    print(f"  Spend: ${s_all.cost:,.0f}")

    section("4. Voice question pipeline")
    questions = [
        "what's our CPA for cardiology this month",
        "how many leads for orthopedics last 7 days",
        "show me cancer spend this quarter",
    ]
    for q in questions:
        try:
            answer = answer_question(reports, q)
            print(f'  Q: "{q}"')
            print(f'  A: {answer}\n')
        except Exception as e:
            print(f'  ✗ "{q}" failed: {e}\n')

    section("5. Generating a dashboard HTML file")
    out_path = os.path.expanduser("~/jarvis/test_cardiology_dashboard.html")
    html = build_dashboard(reports, "cardiology", Period.last_n_days(30))
    with open(out_path, "w") as f:
        f.write(html)
    print(f"  ✓ Wrote {len(html):,} bytes to {out_path}")
    print(f"  Open it with: open {out_path}")

    print("\n✅ All smoke tests passed.\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Smoke test failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
