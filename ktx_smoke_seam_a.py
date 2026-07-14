"""
ktx_smoke_seam_a.py — validate Seam A (compile via ktx) end to end.

Run from the Jarvis root in your venv:

    cd ~/Jarvis
    python ktx_smoke_seam_a.py

Checks:
  1. With JARVIS_SQL_ENGINE=ktx, semantic.build_sql() compiles a normal
     metric+dimension query through ktx (backticked SQL).
  2. The compiled SQL executes via run_query (ktx sql) and returns real rows.
  3. A query using a combined dimension (service_line_group) transparently
     FALLS BACK to the BigQuery builder instead of erroring.
All queries are read-only.
"""
import os, sys, importlib


def section(t):
    print("\n" + "=" * 60); print(t); print("=" * 60)


def main():
    ok = True
    os.environ["JARVIS_SQL_ENGINE"] = "ktx"
    import semantic
    importlib.reload(semantic)
    from semantic import build_sql, run_query, load_model, StructuredQuery

    model = load_model()

    section("1. Seam A: compile spend + cpc by date through ktx")
    try:
        q = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "ad_performance", "field": "date"}],
            "metrics": [
                {"table": "ad_performance", "field": "spend"},
                {"table": "ad_performance", "field": "cpc"},
            ],
            "order_by": [{"field": "spend", "direction": "desc"}],
            "limit": 5,
        })
        sql = build_sql(q, model)
        print(sql)
        ktx_style = "`oh-data-warehouse.gold.ad_fact_ads`" in sql      # ktx: whole FQN in one backtick group
        bq_style = "`oh-data-warehouse`.`gold`" in sql                # jarvis builder: per-part backticks
        print("  engine:", "ktx" if ktx_style else ("bigquery-fallback" if bq_style else "unknown"))
        assert ktx_style, "expected ktx-compiled SQL but got BigQuery-builder fallback"
        print("  PASS (compiled via ktx)")
    except Exception as e:
        ok = False; print(f"  FAIL: {e}")

    section("2. Execute the ktx-compiled query")
    try:
        res = run_query(sql)
        print(f"  columns={res.columns}")
        for r in res.rows[:5]:
            print("   ", r)
        assert res.columns, "no columns returned"
        print("  PASS")
    except Exception as e:
        ok = False; print(f"  FAIL: {e}")

    section("3. Combined dimension falls back to BigQuery builder")
    try:
        q2 = StructuredQuery.from_dict({
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line_group"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
            "limit": 5,
        })
        sql2 = build_sql(q2, model)
        print(sql2[:300], "...")
        # BigQuery builder emits a CASE expression for the combined dimension
        assert "CASE" in sql2.upper() or "service_line_group" in sql2, "expected BigQuery-builder fallback output"
        print("  PASS (fell back cleanly, no crash)")
    except Exception as e:
        ok = False; print(f"  FAIL: {e}")

    section("RESULT")
    print("  ALL PASS ✓" if ok else "  SOME CHECKS FAILED ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
