"""
test_engine.py
──────────────
End-to-end verification of the semantic engine.

Run this after installing Stage 1B to confirm everything works:

    cd ~/jarvis
    source venv/bin/activate
    python -m semantic.test_engine

What it tests, in order:
  1. YAML loads and parses cleanly
  2. Resolver produces valid SQL fragments
  3. Query builder assembles full SELECT statements
  4. BigQuery connection works (small dry-run query)
  5. End-to-end query against real BigQuery returns rows
  6. Calculated metrics evaluate correctly
  7. Combined dimensions return expected labels
  8. Writer can round-trip the YAML

Each test prints a short result line. The script exits 0 on full success,
non-zero on any failure (so CI can use it).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


# ─── Test harness ──────────────────────────────────────────────────
PASSED = 0
FAILED = 0
FAILURES: list[tuple[str, str]] = []


def test(name: str):
    """Decorator: register a function as a test, run it, report."""
    def decorator(fn):
        global PASSED, FAILED
        try:
            fn()
            PASSED += 1
            print(f"  ✓ {name}")
        except Exception as e:
            FAILED += 1
            FAILURES.append((name, traceback.format_exc()))
            print(f"  ✗ {name}: {e}")
        return fn
    return decorator


def section(title: str):
    print()
    print(f"▶ {title}")


# ─── Main suite ─────────────────────────────────────────────────────
def run_all():
    print()
    print("═══════════════════════════════════════════════════════════════")
    print("  Semantic Engine — End-to-End Verification")
    print("═══════════════════════════════════════════════════════════════")

    # ─── 1. Loader ──
    section("1. YAML loading and parsing")

    from semantic import load_model

    @test("load_model() returns SemanticModel with version 1")
    def t():
        m = load_model()
        assert m.version == 1, f"Expected version 1, got {m.version}"

    @test("4 original core tables still present (campaigns, ad_performance, date, sessions)")
    def t():
        # The original 4 must still be there; additional tables curated later are fine.
        m = load_model()
        required = {"campaigns", "ad_performance", "date", "sessions"}
        actual = set(m.tables.keys())
        missing = required - actual
        assert not missing, f"Missing required tables: {missing} (have {len(actual)} total)"

    @test("7 calculated metrics present")
    def t():
        m = load_model()
        assert len(m.calculated_metrics) == 7, f"Expected 7 calc metrics, got {len(m.calculated_metrics)}"

    @test("2 combined dimensions present")
    def t():
        m = load_model()
        assert len(m.combined_dimensions) == 2, f"Expected 2 combined dims, got {len(m.combined_dimensions)}"

    @test("3 joins present, all with string 'on' keys")
    def t():
        m = load_model()
        assert len(m.joins) == 3, f"Expected 3 joins, got {len(m.joins)}"
        for j in m.joins:
            assert isinstance(j.on, str), f"Join 'on' should be string, got {type(j.on).__name__}"

    # ─── 2. Resolver ──
    section("2. SQL fragment resolution")

    from semantic import field_sql, table_sql

    @test("Resolve table to fully-qualified BigQuery reference")
    def t():
        m = load_model()
        result = table_sql("ad_performance", m)
        assert "oh-data-warehouse" in result and "gold" in result and "ad_fact_ads" in result
        assert "`" in result, "Identifiers should be backtick-quoted"

    @test("Dimension field returns bare column reference")
    def t():
        m = load_model()
        result = field_sql("campaigns", "service_line", m)
        assert "parsed_service_line" in result
        assert "SUM(" not in result, "Dimensions should not be aggregated"

    @test("Metric with micros transforms correctly")
    def t():
        m = load_model()
        result = field_sql("ad_performance", "spend", m)
        assert "SUM(`cost`)" in result
        assert "/ 1000000" in result

    @test("Calculated metric substitutes nested fields")
    def t():
        m = load_model()
        roas = field_sql("ad_performance", "roas", m)
        assert "SUM(`micro_conversions_value`)" in roas
        assert "SUM(`cost`)" in roas
        assert "NULLIF" in roas

    @test("Combined dimension produces CASE WHEN")
    def t():
        m = load_model()
        result = field_sql("campaigns", "service_line_group", m)
        assert "CASE" in result
        assert "WHEN" in result
        assert "Cardiology" in result
        assert "Heart & Vascular" in result
        assert "Other Specialties" in result

    @test("Aggregations SUM_BOOL and COUNT_DISTINCT work")
    def t():
        m = load_model()
        sessions = field_sql("sessions", "sessions", m)
        assert "COUNT(DISTINCT" in sessions
        engaged = field_sql("sessions", "engaged_sessions", m)
        assert "CAST(" in engaged and "AS INT64" in engaged

    # ─── 3. Query builder ──
    section("3. Full SQL assembly")

    from semantic import build_sql, StructuredQuery

    @test("Build SQL: spend by service line")
    def t():
        m = load_model()
        q = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
            "limit": 10,
        })
        sql = build_sql(q, m)
        assert "SELECT" in sql and "FROM" in sql
        assert "LEFT JOIN" in sql
        assert "GROUP BY 1" in sql
        assert "LIMIT 10" in sql

    @test("Build SQL: with date filter and ROAS")
    def t():
        m = load_model()
        q = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "campaign_name"}],
            "metrics": [
                {"table": "ad_performance", "field": "spend"},
                {"table": "ad_performance", "field": "roas"},
            ],
            "filters": [
                {"table": "ad_performance", "field": "date", "op": "between",
                 "value": ["2024-01-01", "2024-12-31"]}
            ],
        })
        sql = build_sql(q, m)
        assert "BETWEEN '2024-01-01' AND '2024-12-31'" in sql

    @test("Build SQL: combined dimension grouping")
    def t():
        m = load_model()
        q = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line_group"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
        })
        sql = build_sql(q, m)
        assert "CASE" in sql and "Heart & Vascular" in sql

    # ─── 4. BigQuery connection ──
    section("4. BigQuery connection")

    from semantic import estimate_cost, run_query
    from semantic.executor import ExecutorConfigError

    @test("Connection initializes (credentials load)")
    def t():
        # Try a tiny dry-run query
        try:
            estimate_cost("SELECT 1 AS test")
        except ExecutorConfigError as e:
            raise AssertionError(f"BigQuery connection failed: {e}")

    @test("Dry-run on real Gold table works")
    def t():
        bytes_processed = estimate_cost("SELECT 1 FROM `oh-data-warehouse.gold.dim_date` LIMIT 1")
        # Dry-run should succeed and return bytes that would be scanned
        # (small for dim_date, could be 0 for cached or full table scan)
        assert bytes_processed >= 0

    # ─── 5. Real query ──
    section("5. End-to-end real query")

    @test("Execute small live query against dim_date")
    def t():
        result = run_query(
            "SELECT calendar_year FROM `oh-data-warehouse.gold.dim_date` "
            "GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 5"
        )
        assert result.row_count > 0, "Expected at least 1 row from dim_date"
        assert "calendar_year" in result.columns
        print(f"      ↪ returned {result.row_count} rows, {result.bytes_processed:,} bytes processed, {result.elapsed_ms}ms")

    @test("Spend by service line query runs against real data")
    def t():
        m = load_model()
        q = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
            "order_by": [{"field": "spend", "direction": "desc"}],
            "limit": 5,
        })
        sql = build_sql(q, m)
        result = run_query(sql)
        assert result.row_count > 0, "Expected at least 1 row of spend by service line"
        print(f"      ↪ Top {result.row_count} service lines by spend:")
        for row in result.rows[:5]:
            sl, spend = row
            print(f"          {sl}: ${spend:,.2f}" if spend else f"          {sl}: null")

    # ─── 6. Schema introspection ──
    section("6. Schema introspection")

    from semantic import introspect_curated_table, list_dataset_tables

    @test("Introspect ad_dim_campaign returns real columns")
    def t():
        m = load_model()
        schema = introspect_curated_table("campaigns", m)
        assert len(schema.columns) > 5, f"Expected many columns, got {len(schema.columns)}"
        col_names = {c.name for c in schema.columns}
        # Must include the columns we map in semantic.yaml
        for needed in ["campaign_id", "campaign_name", "parsed_service_line"]:
            assert needed in col_names, f"Expected column {needed} in raw schema, got {col_names}"
        print(f"      ↪ {len(schema.columns)} columns in raw ad_dim_campaign")

    @test("List Gold dataset tables")
    def t():
        tables = list_dataset_tables("oh-data-warehouse", "gold")
        assert len(tables) > 10, f"Expected many Gold tables, got {len(tables)}"
        # Must include our curated tables
        for needed in ["ad_dim_campaign", "ad_fact_ads", "dim_date", "ga4_dim_sessions"]:
            assert needed in tables, f"Expected {needed} in Gold dataset, got {tables[:5]}..."
        print(f"      ↪ {len(tables)} tables in Gold")

    # ─── 7. Writer ──
    section("7. Writer round-trip")

    from semantic import save_model, load_model, invalidate_cache
    from semantic.loader import SEMANTIC_YAML
    import shutil

    @test("Round-trip: save model, reload, verify same structure")
    def t():
        # Back up the current YAML so we don't actually mutate state
        backup = SEMANTIC_YAML.with_suffix(".test_backup")
        shutil.copy(SEMANTIC_YAML, backup)
        try:
            m1 = load_model()
            save_model(m1, make_backup=False)
            invalidate_cache()
            m2 = load_model()
            # Verify key invariants
            assert m1.version == m2.version
            assert set(m1.tables.keys()) == set(m2.tables.keys())
            assert set(m1.calculated_metrics.keys()) == set(m2.calculated_metrics.keys())
            # Verify joins still parse with string 'on' (not boolean True)
            for j in m2.joins:
                assert isinstance(j.on, str), f"After save+reload, join 'on' is {type(j.on).__name__}"
        finally:
            # Restore original
            shutil.copy(backup, SEMANTIC_YAML)
            backup.unlink()
            invalidate_cache()

    # ─── Summary ──
    print()
    print("═══════════════════════════════════════════════════════════════")
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    print("═══════════════════════════════════════════════════════════════")

    if FAILED:
        print()
        print("  Failure details:")
        for name, tb in FAILURES:
            print()
            print(f"  ─── {name} ───")
            print(tb)
        sys.exit(1)
    else:
        print()
        print("  All tests passed. Stage 1B engine is verified.")
        sys.exit(0)


if __name__ == "__main__":
    run_all()
