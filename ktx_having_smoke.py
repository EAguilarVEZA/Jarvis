"""
ktx_having_smoke.py — validate measure / HAVING filters (#18).

Run from the Jarvis root in your venv:

    cd ~/Jarvis && source venv/bin/activate
    JARVIS_SQL_ENGINE=ktx python ktx_having_smoke.py

A query with both a dimension filter (date → WHERE) and a metric filter
(spend → HAVING) should compile with a HAVING clause and execute. Read-only.
"""
import os, sys
os.environ.setdefault("JARVIS_SQL_ENGINE", "ktx")
from semantic import build_sql, run_query, load_model, StructuredQuery

model = load_model()
q = StructuredQuery.from_dict({
    "primary_table": "ad_performance",
    "dimensions": [{"table": "ad_performance", "field": "date"}],
    "metrics": [{"table": "ad_performance", "field": "spend"},
                {"table": "ad_performance", "field": "clicks"}],
    "filters": [
        {"table": "ad_performance", "field": "date", "op": "between", "value": ["2015-01-01", "2015-12-31"]},
        {"table": "ad_performance", "field": "spend", "op": "gt", "value": 2000},   # HAVING SUM(cost) > 2000
    ],
    "order_by": [{"field": "spend", "direction": "desc"}],
    "limit": 10,
})

print("=" * 64)
sql = build_sql(q, model)
print(sql)
print("=" * 64)
assert "HAVING" in sql.upper(), "expected a HAVING clause for the metric filter"
assert "WHERE" in sql.upper(), "expected a WHERE clause for the date filter"
print("PASS: metric filter compiled to HAVING, date filter to WHERE\n")

res = run_query(sql)
print("columns:", res.columns)
for r in res.rows[:10]:
    print("  ", r)
assert all((row[1] or 0) > 2000 for row in res.rows), "every returned day should have spend > 2000"
print("\nPASS: executed; every row has daily spend > $2000.")
