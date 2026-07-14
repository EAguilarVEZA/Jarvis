"""
test_api.py
───────────
End-to-end verification of the Stage 2 HTTP layer over the semantic engine.

Run AFTER restarting jarvis so server.py picks up the new semantic_router:

    cd ~/jarvis
    source venv/bin/activate
    ./jarvis restart        # if the server isn't already running with the latest patch
    python -m semantic.test_api

What it tests, in order:
  1. Server is reachable (/api/health)
  2. GET  /api/semantic/tables           → 4 curated tables
  3. GET  /api/semantic/tables/ad_performance → full schema, calc metrics included
  4. GET  /api/semantic/tables/nonexistent    → 404 with useful error
  5. GET  /api/semantic/calculated       → 7 calculated metrics
  6. GET  /api/semantic/combined         → 2 combined dimensions
  7. POST /api/semantic/query            → real rows for spend-by-service-line
  8. POST /api/semantic/query (bad body) → 400 with useful error
  9. GET  /api/semantic/raw_tables       → all Gold tables, with curated flags
 10. GET  /api/semantic/raw_schema       → 30 columns for ad_dim_campaign
 11. POST /api/semantic/save             → round-trip the YAML, verify unchanged on disk

Exits 0 on full success, non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

import httpx


BASE_URL = os.environ.get("JARVIS_API_BASE", "https://127.0.0.1:8000")
# Server is HTTPS with self-signed cert; verify=False is fine for a local dev tool.
client = httpx.Client(base_url=BASE_URL, verify=False, timeout=60.0)


# ─── Test harness (matches test_engine.py style) ───────────────────
PASSED = 0
FAILED = 0
FAILURES: list[tuple[str, str]] = []


def test(name: str):
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


def _server_alive() -> bool:
    """Probe /api/health. Bail out cleanly if the server isn't running."""
    try:
        r = client.get("/api/health")
        return r.status_code == 200
    except Exception:
        return False


# ─── Main suite ─────────────────────────────────────────────────────
def run_all():
    print()
    print("═══════════════════════════════════════════════════════════════")
    print(f"  Semantic API — End-to-End Verification ({BASE_URL})")
    print("═══════════════════════════════════════════════════════════════")

    if not _server_alive():
        print()
        print(f"  ✗ Could not reach {BASE_URL}/api/health.")
        print("    Start the server first:")
        print("      cd ~/jarvis && ./jarvis restart")
        print("    Or set JARVIS_API_BASE if the server runs elsewhere.")
        sys.exit(2)

    # ─── 1. List curated tables ──
    section("1. GET /api/semantic/tables")

    @test("Returns 4 curated tables (campaigns, ad_performance, date, sessions)")
    def t():
        r = client.get("/api/semantic/tables")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["count"] == 4, f"Expected 4 tables, got {body['count']}"
        keys = {t["key"] for t in body["tables"]}
        assert keys == {"campaigns", "ad_performance", "date", "sessions"}, f"Unexpected keys: {keys}"

    @test("Each table summary has expected shape (display_name, cluster, kind, field_count)")
    def t():
        r = client.get("/api/semantic/tables")
        body = r.json()
        for t in body["tables"]:
            for key in ["key", "display_name", "description", "cluster", "kind", "raw_table", "field_count"]:
                assert key in t, f"Table missing key '{key}': {t}"
            for fk in ["dimensions", "metrics", "dates"]:
                assert fk in t["field_count"], f"field_count missing '{fk}'"

    # ─── 2. Get a single table ──
    section("2. GET /api/semantic/tables/{key}")

    @test("ad_performance returns full schema with calc metrics")
    def t():
        r = client.get("/api/semantic/tables/ad_performance")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["key"] == "ad_performance"
        assert body["kind"] == "fact"
        assert len(body["metrics"]) > 0, "Expected at least one metric"
        # ROAS is defined as a calc metric available_in ad_performance
        calc_keys = {m["key"] for m in body["calculated_metrics"]}
        assert "roas" in calc_keys or len(calc_keys) > 0, f"Expected calc metrics, got {calc_keys}"
        print(f"      ↪ {len(body['dimensions'])} dimensions, {len(body['metrics'])} metrics, "
              f"{len(body['dates'])} dates, {len(body['calculated_metrics'])} calc metrics")

    @test("Each field has key, column, display_name, field_kind")
    def t():
        r = client.get("/api/semantic/tables/campaigns")
        body = r.json()
        for f in body["dimensions"] + body["metrics"]:
            for key in ["key", "column", "display_name", "field_kind"]:
                assert key in f, f"Field missing '{key}': {f}"

    @test("Unknown table returns 404 with useful error")
    def t():
        r = client.get("/api/semantic/tables/no_such_table_exists")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
        body = r.json()
        assert "error" in body and "detail" in body

    # ─── 3. Calculated metrics ──
    section("3. GET /api/semantic/calculated")

    @test("Returns 7 calculated metrics")
    def t():
        r = client.get("/api/semantic/calculated")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 7, f"Expected 7 calc metrics, got {body['count']}"
        keys = {m["key"] for m in body["calculated_metrics"]}
        for needed in ["ctr", "conversion_rate", "cpa", "roas"]:
            assert needed in keys, f"Missing expected calc metric '{needed}', got {keys}"

    # ─── 4. Combined dimensions ──
    section("4. GET /api/semantic/combined")

    @test("Returns 2 combined dimensions (service_line_group, channel_group)")
    def t():
        r = client.get("/api/semantic/combined")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2, f"Expected 2 combined dims, got {body['count']}"
        keys = {d["key"] for d in body["combined_dimensions"]}
        assert "service_line_group" in keys and "channel_group" in keys, f"Unexpected keys: {keys}"

    # ─── 5. Structured query ──
    section("5. POST /api/semantic/query")

    @test("Spend by service line returns rows with sql included")
    def t():
        r = client.post("/api/semantic/query", json={
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
            "order_by": [{"field": "spend", "direction": "desc"}],
            "limit": 5,
        })
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["row_count"] > 0, "Expected at least 1 row"
        assert body["columns"] == ["service_line", "spend"], f"Unexpected columns: {body['columns']}"
        assert "sql" in body and "SELECT" in body["sql"]
        print(f"      ↪ Top {body['row_count']} service lines, {body['bytes_processed']:,} bytes, {body['elapsed_ms']}ms")
        for row in body["rows"][:3]:
            sl, spend = row
            label = f"${spend:,.2f}" if isinstance(spend, (int, float)) else "null"
            print(f"          {sl}: {label}")

    @test("include_sql=false omits SQL from response")
    def t():
        r = client.post("/api/semantic/query?include_sql=false", json={
            "primary_table": "ad_performance",
            "dimensions": [{"table": "campaigns", "field": "service_line"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
            "limit": 1,
        })
        assert r.status_code == 200
        body = r.json()
        assert "sql" not in body, "SQL should be omitted when include_sql=false"

    @test("Malformed query (unknown table) returns 400 with useful error")
    def t():
        r = client.post("/api/semantic/query", json={
            "primary_table": "does_not_exist",
            "dimensions": [],
            "metrics": [{"table": "does_not_exist", "field": "spend"}],
        })
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
        body = r.json()
        assert "error" in body
        assert "detail" in body

    @test("Missing primary_table returns 4xx, not 500")
    def t():
        r = client.post("/api/semantic/query", json={
            "dimensions": [{"table": "campaigns", "field": "service_line"}],
            "metrics": [{"table": "ad_performance", "field": "spend"}],
        })
        # FastAPI/Pydantic returns 422 for missing required body fields
        assert 400 <= r.status_code < 500, f"Expected 4xx, got {r.status_code}: {r.text}"

    # ─── 6. Raw warehouse listing ──
    section("6. GET /api/semantic/raw_tables")

    @test("Lists Gold dataset tables with curated flags on the 4 curated tables")
    def t():
        r = client.get("/api/semantic/raw_tables")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["count"] > 10, f"Expected many Gold tables, got {body['count']}"
        by_name = {t["name"]: t for t in body["tables"]}

        # The 4 curated tables should be flagged
        expected_curated = {
            "ad_dim_campaign": "campaigns",
            "ad_fact_ads": "ad_performance",
            "dim_date": "date",
            "ga4_dim_sessions": "sessions",
        }
        for raw, curated_key in expected_curated.items():
            assert raw in by_name, f"Expected raw table '{raw}' in dataset, missing from response"
            assert by_name[raw]["curated"] is True, f"{raw} should be curated"
            assert by_name[raw]["curated_as"] == curated_key, \
                f"{raw} should map to {curated_key}, got {by_name[raw]['curated_as']}"

        # At least one uncurated table should exist
        uncurated = [t for t in body["tables"] if not t["curated"]]
        assert len(uncurated) > 0, "Expected at least one uncurated table"
        print(f"      ↪ {body['count']} total tables in {body['project']}.{body['dataset']}, "
              f"{sum(1 for t in body['tables'] if t['curated'])} curated")

    # ─── 7. Raw schema for one table ──
    section("7. GET /api/semantic/raw_schema")

    @test("ad_dim_campaign returns columns including campaign_id and parsed_service_line")
    def t():
        r = client.get("/api/semantic/raw_schema", params={"table": "ad_dim_campaign"})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["table"] == "ad_dim_campaign"
        assert len(body["columns"]) > 5
        col_names = {c["name"] for c in body["columns"]}
        for needed in ["campaign_id", "campaign_name", "parsed_service_line"]:
            assert needed in col_names, f"Expected column '{needed}', got {sorted(col_names)[:10]}..."
        print(f"      ↪ {len(body['columns'])} columns in ad_dim_campaign")

    # ─── 8. Save round-trip ──
    section("8. POST /api/semantic/save (round-trip)")

    @test("Read semantic.yaml, POST it back unchanged, verify disk content matches")
    def t():
        # Read the YAML from disk via the loader's known path
        from semantic.loader import SEMANTIC_YAML
        before = SEMANTIC_YAML.read_text()

        # Snapshot which backup files already exist so we can clean up after
        backup_glob_before = set(p.name for p in SEMANTIC_YAML.parent.glob("semantic.yaml.bak.*"))

        # POST back
        r = client.post("/api/semantic/save", json={"raw_yaml": before})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["ok"] is True
        assert body["bytes_written"] == len(before.encode("utf-8")), \
            f"bytes_written mismatch: {body['bytes_written']} vs {len(before.encode('utf-8'))}"
        assert body["backup_path"], "Expected a backup_path to be returned"
        assert Path(body["backup_path"]).exists(), f"Backup file not found at {body['backup_path']}"

        # Confirm disk content unchanged
        after = SEMANTIC_YAML.read_text()
        assert before == after, "Disk content changed after round-trip save"

        # Clean up: remove the backup file this test just created
        backup_glob_after = set(p.name for p in SEMANTIC_YAML.parent.glob("semantic.yaml.bak.*"))
        new_backups = backup_glob_after - backup_glob_before
        for name in new_backups:
            (SEMANTIC_YAML.parent / name).unlink()

    @test("Empty body returns 400")
    def t():
        r = client.post("/api/semantic/save", json={})
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    @test("Form B (patch) returns 501 not_implemented")
    def t():
        r = client.post("/api/semantic/save", json={"patch": {"tables": {}}})
        assert r.status_code == 501, f"Expected 501, got {r.status_code}: {r.text}"

    # ─── 9. Phase B write-path (self-cleaning) ──
    section("9. PHASE B — write-path mutations (create/verify/delete, rename/restore)")

    from semantic.loader import SEMANTIC_YAML
    # Snapshot backups so we can remove any this section creates.
    _backups_before = set(p.name for p in SEMANTIC_YAML.parent.glob("semantic.yaml.bak.*"))

    @test("Rename table round-trips, then restores")
    def t():
        # Capture original
        orig = client.get("/api/semantic/tables/campaigns").json()["display_name"]
        try:
            r = client.post("/api/semantic/tables/campaigns/rename",
                            json={"display_name": "Marketing Campaigns (test)"})
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            assert r.json()["ok"] is True
            check = client.get("/api/semantic/tables/campaigns").json()
            assert check["display_name"] == "Marketing Campaigns (test)", check["display_name"]
        finally:
            client.post("/api/semantic/tables/campaigns/rename", json={"display_name": orig})
        restored = client.get("/api/semantic/tables/campaigns").json()["display_name"]
        assert restored == orig, f"restore failed: {restored} != {orig}"

    @test("Rename field round-trips, then restores")
    def t():
        tbl = client.get("/api/semantic/tables/campaigns").json()
        fld = next(f for f in tbl["dimensions"] if f["key"] == "service_line")
        orig = fld["display_name"]
        try:
            r = client.post("/api/semantic/tables/campaigns/fields/service_line/rename",
                            json={"display_name": "Clinical Service Line (test)"})
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            tbl2 = client.get("/api/semantic/tables/campaigns").json()
            f2 = next(f for f in tbl2["dimensions"] if f["key"] == "service_line")
            assert f2["display_name"] == "Clinical Service Line (test)"
        finally:
            client.post("/api/semantic/tables/campaigns/fields/service_line/rename",
                        json={"display_name": orig})

    @test("Create calculated metric → appears in /calculated → delete")
    def t():
        try:
            r = client.post("/api/semantic/calculated", json={
                "key": "test_cost_per_session", "display_name": "Cost per Session (test)",
                "description": "test", "formula": "{spend} / NULLIF({sessions}, 0)",
                "format": "currency", "available_in": ["ad_performance"],
            })
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            keys = {m["key"] for m in client.get("/api/semantic/calculated").json()["calculated_metrics"]}
            assert "test_cost_per_session" in keys
        finally:
            d = client.delete("/api/semantic/calculated/test_cost_per_session")
            assert d.status_code == 200, f"cleanup failed: {d.status_code}: {d.text}"
        keys = {m["key"] for m in client.get("/api/semantic/calculated").json()["calculated_metrics"]}
        assert "test_cost_per_session" not in keys, "metric not cleaned up"

    @test("Create calc metric with bad available_in returns 400")
    def t():
        r = client.post("/api/semantic/calculated", json={
            "key": "test_bad", "display_name": "x", "formula": "{spend}",
            "available_in": ["no_such_table"],
        })
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    @test("Duplicate calc metric key returns 409")
    def t():
        r = client.post("/api/semantic/calculated", json={
            "key": "ctr", "display_name": "dup", "formula": "{clicks}", "available_in": ["ad_performance"],
        })
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"

    @test("Create combined dimension (rules + default) → verify → delete")
    def t():
        try:
            r = client.post("/api/semantic/combined", json={
                "key": "test_device_group", "display_name": "Device Group (test)",
                "description": "test", "base_field": "device", "available_in": ["ad_performance"],
                "rules": [
                    {"matches": ["MOBILE", "TABLET"], "label": "Mobile/Tablet"},
                    {"label": "Desktop & Other", "is_default": True},
                ],
            })
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            combined = client.get("/api/semantic/combined").json()["combined_dimensions"]
            dg = next((d for d in combined if d["key"] == "test_device_group"), None)
            assert dg is not None and dg["rule_count"] == 2, dg
        finally:
            d = client.delete("/api/semantic/combined/test_device_group")
            assert d.status_code == 200, f"cleanup failed: {d.text}"

    @test("Create business rule → appears in /rules → delete")
    def t():
        try:
            r = client.post("/api/semantic/rules", json={
                "key": "test_active_only", "display_name": "Active Campaigns Only (test)",
                "description": "test", "table": "campaigns", "field": "campaign_status",
                "op": "eq", "value": "ENABLED",
            })
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            rules = client.get("/api/semantic/rules").json()
            keys = {x["key"] for x in rules["business_rules"]}
            assert "test_active_only" in keys, keys
        finally:
            d = client.delete("/api/semantic/rules/test_active_only")
            assert d.status_code == 200, f"cleanup failed: {d.text}"
        keys = {x["key"] for x in client.get("/api/semantic/rules").json()["business_rules"]}
        assert "test_active_only" not in keys, "rule not cleaned up"

    @test("Business rule with invalid op returns 400")
    def t():
        r = client.post("/api/semantic/rules", json={
            "key": "test_badop", "display_name": "x", "table": "campaigns",
            "field": "campaign_status", "op": "frobnicate", "value": "x",
        })
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    @test("Business rule referencing unknown field returns 400")
    def t():
        r = client.post("/api/semantic/rules", json={
            "key": "test_badfield", "display_name": "x", "table": "campaigns",
            "field": "no_such_field", "op": "eq", "value": "x",
        })
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    @test("Write-path left semantic.yaml structurally unchanged")
    def t():
        # All create/delete and rename/restore should net to the original 4/7/2/0 shape.
        tabs = client.get("/api/semantic/tables").json()["count"]
        calc = client.get("/api/semantic/calculated").json()["count"]
        comb = client.get("/api/semantic/combined").json()["count"]
        rules = client.get("/api/semantic/rules").json()["count"]
        assert (tabs, calc, comb, rules) == (4, 7, 2, 0), \
            f"Expected (4,7,2,0), got {(tabs, calc, comb, rules)} — test left residue"

    # ─── 10. ERD graph + joins (self-cleaning) ──
    section("10. ERD graph + join CRUD")

    @test("GET /graph returns tables with fields and joins")
    def t():
        r = client.get("/api/semantic/graph")
        assert r.status_code == 200, f"{r.status_code}: {r.text}"
        g = r.json()
        assert g["table_count"] >= 4 and "joins" in g
        # each table carries field arrays
        any_t = g["tables"][0]
        for k in ["key", "dimensions", "metrics", "dates", "join_keys"]:
            assert k in any_t

    @test("Add a join, see it in /graph, then delete it (restores state)")
    def t():
        before = client.get("/api/semantic/graph").json()["join_count"]
        try:
            r = client.post("/api/semantic/joins", json={"left": "date", "right": "campaigns", "on": "campaign_id"})
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            mid = client.get("/api/semantic/graph").json()["join_count"]
            assert mid == before + 1, f"expected +1 join, got {mid} vs {before}"
        finally:
            d = client.post("/api/semantic/joins/delete", json={"left": "date", "right": "campaigns"})
            assert d.status_code == 200, f"cleanup failed: {d.text}"
        after = client.get("/api/semantic/graph").json()["join_count"]
        assert after == before, f"join count not restored: {after} vs {before}"

    # Clean up any backup files this section created.
    _backups_after = set(p.name for p in SEMANTIC_YAML.parent.glob("semantic.yaml.bak.*"))
    for name in (_backups_after - _backups_before):
        try:
            (SEMANTIC_YAML.parent / name).unlink()
        except OSError:
            pass

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
        print("  All tests passed. Stage 2 HTTP layer is verified.")
        sys.exit(0)


if __name__ == "__main__":
    run_all()
