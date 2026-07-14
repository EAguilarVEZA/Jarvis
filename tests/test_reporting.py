"""
Tests for the reporting module using a fake BQ client.

We verify:
  - catalog: alias resolution, lookup robustness
  - queries: SQL builds and result objects compute the right derived metrics
  - voice: intent parsing and answer formatting
  - dashboard: HTML renders without errors and contains expected values

Run with:  python -m pytest tests/ -v
or just:   python tests/test_reporting.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# Make the package importable when running this file directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.bq_client import BQClient, QueryResult
from reporting.catalog import resolve, SERVICE_LINES, BY_KEY
from reporting.queries import Reports, Period, Summary
from reporting.voice import (
    parse_intent, answer_question, format_voice_answer,
    _find_service_line, _parse_period,
)
from reporting.dashboard import build_dashboard


# -------- fake BQ client --------

class FakeBQ(BQClient):
    """Pattern-matches SQL fragments to canned results.

    Picks the LONGEST matching fragment so callers can register a generic
    fragment like '.leads`' alongside more specific ones like 'FULL OUTER
    JOIN spend' without ordering surprises.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.responses: dict[str, list[dict]] = {}

    def respond(self, sql_fragment: str, rows: list[dict]):
        self.responses[sql_fragment] = rows
        return self

    def query(self, sql, params=None, use_cache=True, timeout_seconds=60):
        self.calls.append((sql, params))
        # Longest fragment wins so specific patterns beat generic ones
        matches = [(frag, rows) for frag, rows in self.responses.items() if frag in sql]
        if matches:
            frag, rows = max(matches, key=lambda x: len(x[0]))
            return QueryResult(rows=rows, bytes_billed=0, cache_hit=False, elapsed_ms=1)
        return QueryResult(rows=[], bytes_billed=0, cache_hit=False, elapsed_ms=1)


# -------- catalog --------

def test_catalog_resolves_keys():
    assert resolve("cardiology").key == "cardiology"
    assert resolve("CARDIOLOGY").key == "cardiology"
    assert resolve("all").key == "all"


def test_catalog_resolves_aliases():
    assert resolve("heart").key == "cardiology"
    assert resolve("oncology").key == "cancer"
    assert resolve("ortho").key == "orthopedics"
    assert resolve("primary care").key == "primary"
    assert resolve("weight loss").key == "bariatrics"
    assert resolve("stroke").key == "neuroscience"


def test_catalog_loose_match():
    # Question-style phrase should still find the service line
    assert resolve("the cardiology stuff").key == "cardiology"
    assert resolve("how is womens health doing").key == "womens"


def test_catalog_unknown_returns_none():
    assert resolve("blockchain") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_catalog_all_dataset_names_match_inventory():
    # Sanity check: every entry has a non-empty dataset string starting with data_mart_
    for sl in SERVICE_LINES:
        assert sl.dataset.startswith("data_mart_"), f"{sl.key} has bad dataset"


# -------- period helpers --------

def test_period_last_n_days_inclusive():
    p = Period.last_n_days(7)
    assert p.days == 7
    assert p.end == date.today()
    assert p.start == date.today() - timedelta(days=6)


def test_period_month_to_date():
    p = Period.month_to_date()
    assert p.start.day == 1
    assert p.end == date.today()


def test_period_previous_month_is_full_month():
    p = Period.previous_month()
    assert p.start.day == 1
    # End is the last day of the previous month — next day is the 1st
    assert (p.end + timedelta(days=1)).day == 1


# -------- summary computation --------

def test_summary_metrics_with_data():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 100, "paid_leads": 60, "organic_leads": 40}])
    bq.respond(".combined_campaign_stats`",
               [{"cost": 6000.0, "clicks": 1500, "impressions": 60000}])

    s = Reports(bq).summary("cardiology", Period.last_n_days(30))
    assert s.leads == 100
    assert s.paid_leads == 60
    assert s.cost == 6000.0
    assert s.cpa == 100.0   # 6000 / 60
    assert s.ctr == 2.5     # 1500 / 60000
    assert s.cpc == 4.0     # 6000 / 1500


def test_summary_handles_zero_division():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 0, "paid_leads": 0, "organic_leads": 0}])
    bq.respond(".combined_campaign_stats`", [{"cost": 0, "clicks": 0, "impressions": 0}])
    s = Reports(bq).summary("cardiology")
    assert s.cpa is None
    assert s.ctr is None
    assert s.cpc is None


def test_summary_handles_null_rows():
    """BQ can return NULLs for empty windows; we should treat them as zeros."""
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": None, "paid_leads": None, "organic_leads": None}])
    bq.respond(".combined_campaign_stats`", [{"cost": None, "clicks": None, "impressions": None}])
    s = Reports(bq).summary("cardiology")
    assert s.leads == 0
    assert s.cost == 0.0


def test_summary_unknown_service_line_raises():
    bq = FakeBQ()
    try:
        Reports(bq).summary("blockchain")
        assert False, "should have raised"
    except ValueError as e:
        assert "blockchain" in str(e)


def test_summary_uses_correct_dataset():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 10, "paid_leads": 5, "organic_leads": 5}])
    bq.respond(".combined_campaign_stats`", [{"cost": 100, "clicks": 10, "impressions": 1000}])

    Reports(bq).summary("cardiology")
    sqls = " ".join(c[0] for c in bq.calls)
    assert "data_mart_cardiology" in sqls
    assert "data_mart_cancer" not in sqls


def test_summary_all_uses_count_star_not_sum_leads():
    """data_mart_all.leads is event-log (no `leads` column) — must use COUNT(*).

    This catches the bug where querying the cross-service rollup with
    SUM(leads) raised 'Unrecognized name: leads' from BigQuery.
    """
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 1000, "paid_leads": 600, "organic_leads": 400}])
    bq.respond(".combined_campaign_stats`",
               [{"cost": 50000, "clicks": 5000, "impressions": 200000}])

    Reports(bq).summary("all")
    leads_sqls = [c[0] for c in bq.calls if "FROM `oh-data-warehouse.data_mart_all.leads`" in c[0]]
    assert leads_sqls, "summary should query data_mart_all.leads"
    assert "COUNT(*)" in leads_sqls[0], (
        "data_mart_all.leads must be queried with COUNT(*), not SUM(leads), "
        "because the table has no `leads` column (it's one row per lead)."
    )
    assert "SUM(leads)" not in leads_sqls[0]
    # Also check is_brand_lead is NOT referenced (doesn't exist in data_mart_all)
    assert "is_brand_lead" not in leads_sqls[0]


def test_summary_per_service_line_uses_sum_leads():
    """Per-service-line marts ARE pre-aggregated — must use SUM(leads)."""
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 10, "paid_leads": 5, "organic_leads": 5}])
    bq.respond(".combined_campaign_stats`", [{"cost": 100, "clicks": 10, "impressions": 1000}])

    Reports(bq).summary("cardiology")
    leads_sqls = [c[0] for c in bq.calls if ".leads`" in c[0]]
    assert "SUM(leads)" in leads_sqls[0]


def test_top_campaigns_all_omits_dashboard_campaign():
    """data_mart_all.combined_campaign_stats has no dashboard_campaign column."""
    bq = FakeBQ()
    bq.respond(".combined_campaign_stats`",
               [{"campaign_name": "X", "cost": 100, "clicks": 10,
                 "impressions": 1000, "leads": 5,
                 "ad_source": "google", "dashboard_campaign": None}])

    Reports(bq).top_campaigns("all")
    sql = bq.calls[0][0]
    # ANY_VALUE(dashboard_campaign) would fail in real BQ — must be NULL placeholder
    assert "ANY_VALUE(dashboard_campaign)" not in sql
    assert "CAST(NULL AS STRING) AS dashboard_campaign" in sql


def test_top_campaigns_per_service_keeps_dashboard_campaign():
    bq = FakeBQ()
    bq.respond(".combined_campaign_stats`",
               [{"campaign_name": "X", "cost": 100, "clicks": 10,
                 "impressions": 1000, "leads": 5,
                 "ad_source": "google", "dashboard_campaign": "Brand Awareness"}])

    Reports(bq).top_campaigns("cardiology")
    sql = bq.calls[0][0]
    assert "ANY_VALUE(dashboard_campaign)" in sql


# -------- comparison --------

def test_summary_with_comparison_calculates_deltas():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 120, "paid_leads": 80, "organic_leads": 40}])
    bq.respond(".combined_campaign_stats`",
               [{"cost": 8000.0, "clicks": 2000, "impressions": 80000}])
    out = Reports(bq).summary_with_comparison("cardiology", Period.last_n_days(7))
    # Same fake answers for both periods → 0% deltas
    assert out["deltas"]["leads_pct"] == 0.0
    assert out["deltas"]["cost_pct"] == 0.0


# -------- voice intent parsing --------

def test_intent_finds_metric():
    intent = parse_intent("what's our CPA for cardiology this month")
    assert intent is not None
    assert intent.metric == "cpa"
    assert intent.service_line.key == "cardiology"


def test_intent_finds_period():
    i1 = parse_intent("how many leads for ortho last 7 days")
    assert i1.period.days == 7
    i2 = parse_intent("cancer spend month to date")
    assert i2.period.start.day == 1
    i3 = parse_intent("orthopedics last 14 days")
    assert i3.period.days == 14


def test_intent_returns_none_without_service_line():
    assert parse_intent("how's our CPA looking") is None
    assert parse_intent("") is None


def test_intent_default_metric_is_summary():
    intent = parse_intent("how is cardiology doing")
    assert intent.metric == "summary"


def test_intent_aliases_work():
    assert parse_intent("heart leads today").service_line.key == "cardiology"
    assert parse_intent("show me oncology spend").service_line.key == "cancer"


# -------- voice formatting --------

def _fake_summary(**kw):
    defaults = dict(
        service_line="cardiology", label="Cardiology",
        period=Period.last_n_days(30),
        leads=100, paid_leads=60, organic_leads=40,
        cost=6000.0, clicks=1500, impressions=60000,
    )
    defaults.update(kw)
    return Summary(**defaults)


def test_voice_answer_cpa():
    intent = parse_intent("cardiology cpa last 30 days")
    text = format_voice_answer(intent, _fake_summary())
    assert "$100" in text
    assert "Cardiology" in text


def test_voice_answer_leads():
    intent = parse_intent("how many leads for cardiology last 30 days")
    text = format_voice_answer(intent, _fake_summary())
    assert "100" in text
    assert "60" in text  # paid leads


def test_voice_answer_no_paid_leads_handles_gracefully():
    intent = parse_intent("cardiology cpa last 30 days")
    s = _fake_summary(paid_leads=0, cost=0)
    text = format_voice_answer(intent, s)
    assert "can't compute" in text.lower() or "no paid leads" in text.lower()


def test_voice_answer_full_pipeline():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 50, "paid_leads": 30, "organic_leads": 20}])
    bq.respond(".combined_campaign_stats`",
               [{"cost": 3000, "clicks": 800, "impressions": 30000}])
    answer = answer_question(Reports(bq), "what's our CPA for cardiology this month")
    assert answer is not None
    assert "Cardiology" in answer


def test_voice_answer_unknown_question_returns_none():
    bq = FakeBQ()
    answer = answer_question(Reports(bq), "what's the weather")
    assert answer is None


# -------- dashboard --------

def test_dashboard_renders_html():
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 100, "paid_leads": 60, "organic_leads": 40,
                            "source": "google", "medium": "cpc", "is_paid": True}])
    bq.respond(".combined_campaign_stats`",
               [{"cost": 5000, "clicks": 1000, "impressions": 50000,
                 "campaign_name": "Test", "dashboard_campaign": "Test", "ad_source": "google",
                 "leads": 25}])
    # daily_timeseries uses a CTE with FULL OUTER JOIN — fragment match below
    bq.respond("FULL OUTER JOIN spend s USING (date)",
               [{"date": date.today(), "leads": 10, "cost": 100.0}])
    html = build_dashboard(Reports(bq), "cardiology", Period.last_n_days(30))
    assert "<!doctype html>" in html
    assert "Cardiology" in html
    assert "Marketing Report" in html
    assert "chart.js" in html.lower()


def test_dashboard_escapes_html_in_campaign_names():
    """A campaign name with HTML in it should not break the page."""
    bq = FakeBQ()
    bq.respond(".leads`", [{"leads": 10, "paid_leads": 5, "organic_leads": 5,
                            "source": "google", "medium": "cpc", "is_paid": True}])
    bq.respond(
        ".combined_campaign_stats`",
        [{"cost": 100, "clicks": 10, "impressions": 1000,
          "campaign_name": "<script>alert(1)</script>",
          "dashboard_campaign": None, "ad_source": "google", "leads": 1}],
    )
    bq.respond("FULL OUTER JOIN spend s USING (date)",
               [{"date": date.today(), "leads": 1, "cost": 10.0}])
    html = build_dashboard(Reports(bq), "cardiology")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# -------- runner --------

if __name__ == "__main__":
    import inspect
    tests = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    passed = 0
    failed = []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed.append((name, e))
            print(f"  ✗ {name}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        print(f"\n{len(failed)} failures:")
        for name, e in failed:
            print(f"  {name}: {type(e).__name__}: {e}")
        sys.exit(1)
