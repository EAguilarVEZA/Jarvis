"""
ktx_smoke.py — validate the Jarvis × ktx integration end to end.

Run from the Jarvis root inside your venv:

    cd ~/Jarvis
    python ktx_smoke.py

It checks, in order:
  1. ktx is reachable and executes SQL (ktx_client.healthcheck)
  2. semantic search / provenance works (ktx_client.search_context)
  3. the feature flag routes semantic.run_query through ktx
  4. ktx and BigQuery return the same rows for one identical query (parity)

Nothing here writes data; all queries are read-only.
"""
import os
import sys


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main():
    ok = True

    section("1. ktx reachable + executes SQL")
    try:
        import ktx_client
        hc = ktx_client.healthcheck()
        print(f"  ktx_bin     : {hc['ktx_bin']}")
        print(f"  project_dir : {hc['project_dir']}")
        print(f"  connection  : {hc['connection']}")
        print(f"  SELECT 1    : rows={hc['rows']}  ({hc['elapsed_ms']} ms)")
        assert hc["ok"], "healthcheck returned ok=False"
        print("  PASS")
    except Exception as e:
        ok = False
        print(f"  FAIL: {e}")

    section("2. semantic search / provenance")
    try:
        import ktx_client
        hits = ktx_client.search_context("cardiology spend", limit=3)
        for h in hits:
            print(f"  - {h['name']:<28} score={h['score']:.3f}  {h['description'][:60]}")
        assert hits, "no search results"
        print("  PASS")
    except Exception as e:
        ok = False
        print(f"  FAIL: {e}")

    section("3. feature flag routes run_query through ktx")
    try:
        os.environ["JARVIS_SQL_ENGINE"] = "ktx"
        # import AFTER setting the flag so the dispatch sees it
        import importlib
        import semantic
        importlib.reload(semantic)  # ensure fresh binding of the wrapped run_query
        res = semantic.run_query("SELECT 1 AS one, 'jarvis' AS who")
        print(f"  columns={res.columns}  rows={res.rows}")
        assert res.rows and res.rows[0][0] == 1, "unexpected rows via ktx path"
        print("  PASS")
    except Exception as e:
        ok = False
        print(f"  FAIL: {e}")

    section("4. ktx vs BigQuery parity (same query, both engines)")
    try:
        sql = "SELECT COUNT(*) AS n FROM `oh-data-warehouse.gold.dim_date`"
        from semantic.executor import run_query as bq_run
        import ktx_client
        bq = bq_run(sql)
        kx = ktx_client.execute_sql(sql)
        print(f"  bigquery: {bq.rows}")
        print(f"  ktx     : {kx.rows}")
        assert bq.rows == kx.rows, "row mismatch between engines"
        print("  PASS")
    except Exception as e:
        ok = False
        print(f"  FAIL: {e}")

    section("RESULT")
    print("  ALL PASS ✓" if ok else "  SOME CHECKS FAILED ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
