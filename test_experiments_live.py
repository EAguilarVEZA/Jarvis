#!/usr/bin/env python3
"""
test_experiments_live.py
────────────────────────
End-to-end smoke test for the Test & Learn stats engine against a RUNNING
Jarvis server (validates the pieces the sandbox can't: HTTP wiring + live
BigQuery data-mode).

Usage:
    python3 test_experiments_live.py
    JARVIS_BASE=https://localhost:8000 python3 test_experiments_live.py

To also exercise the live data-mode against BigQuery, set (matching your model):
    EXP_TABLE, EXP_METRIC, EXP_UNIT, EXP_DATE, EXP_GROUP,
    EXP_TEST_VALUES (comma-sep), EXP_CONTROL_VALUES (comma-sep),
    EXP_PRE_START, EXP_PRE_END, EXP_POST_START, EXP_POST_END

No third-party deps — uses urllib. Self-signed certs are accepted.
"""
import json
import os
import ssl
import sys
import urllib.request

BASE = os.getenv("JARVIS_BASE", "https://localhost:8000").rstrip("/")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_passed = 0
_failed = 0


def _req(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    global _passed, _failed
    ok = bool(cond)
    _passed += ok
    _failed += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main():
    print(f"Testing Jarvis stats engine at {BASE}\n")

    print("· health")
    try:
        h = _req("GET", "/api/stats/health")
        check("health ok", h.get("ok"))
        check("t_p(2,10)≈0.0734", abs(h["checks"]["t_p_(2,10)"] - 0.0734) < 0.002, str(h["checks"]))
    except Exception as e:
        check("health reachable", False, str(e))
        print("\nServer not reachable — is it running? Aborting.")
        sys.exit(1)

    print("· diagnostics")
    try:
        d = _req("GET", "/api/stats/diagnostics")
        check("semantic model loads", d.get("semantic", {}).get("ok"),
              f"{d.get('semantic', {}).get('table_count')} tables")
        tabs = d.get("semantic", {}).get("tables", [])
        if tabs:
            print(f"    sample table: {tabs[0]['key']} — dims={tabs[0]['dimensions'][:3]} dates={tabs[0]['dates'][:2]} metrics={tabs[0]['metrics'][:3]}")
    except Exception as e:
        check("diagnostics", False, str(e))

    print("· pure-stats endpoints")
    try:
        r = _req("POST", "/api/stats/ttest", {"a": [5.1, 4.9, 5.3, 5.0, 5.2], "b": [4.6, 4.7, 4.5, 4.9, 4.4]})
        check("ttest significant", r["verdict"]["significant"] is True, f"p={r['verdict']['p_display']}")
        r = _req("POST", "/api/stats/proportions", {"successes_a": 120, "n_a": 1000, "successes_b": 90, "n_b": 1000})
        check("proportions lift", round(r["relative_lift_pct"], 0) == 33, f"lift={r['relative_lift_pct']:.1f}%")
        r = _req("POST", "/api/stats/did", {"test_pre": [10, 11, 9, 10], "test_post": [14, 15, 13, 14],
                                            "control_pre": [10, 9, 11, 10], "control_post": [11, 10, 12, 11]})
        check("DiD ≈ 3.0", abs(r["did_estimate"] - 3.0) < 0.01, f"did={r['did_estimate']:.2f}")
        r = _req("POST", "/api/stats/power", {"kind": "mean", "baseline": 100, "sd": 20, "mde_rel": 0.05})
        check("power sizing", r["n_per_group"] > 0, r["summary"])
    except Exception as e:
        check("pure-stats endpoints", False, str(e))

    # Optional: live data-mode against BigQuery
    tbl = os.getenv("EXP_TABLE")
    if tbl:
        print("· live data-mode (BigQuery)")
        body = {
            "primary_table": tbl, "metric_field": os.getenv("EXP_METRIC"),
            "unit_field": os.getenv("EXP_UNIT"), "date_field": os.getenv("EXP_DATE"),
            "group_field": os.getenv("EXP_GROUP"),
            "test_values": [x for x in os.getenv("EXP_TEST_VALUES", "").split(",") if x],
            "control_values": [x for x in os.getenv("EXP_CONTROL_VALUES", "").split(",") if x],
            "pre_start": os.getenv("EXP_PRE_START"), "pre_end": os.getenv("EXP_PRE_END"),
            "post_start": os.getenv("EXP_POST_START"), "post_end": os.getenv("EXP_POST_END"),
            "metric_name": os.getenv("EXP_METRIC"), "robust": True,
        }
        try:
            r = _req("POST", "/api/stats/experiment_from_data", body)
            if r.get("error"):
                check("experiment_from_data ran", False, r["error"])
            else:
                check("experiment_from_data ran", True,
                      f"DiD={r.get('did_estimate'):.2f}, n_test={r.get('n_test_units')}, verdict={r['verdict']['headline']}")
        except Exception as e:
            check("experiment_from_data", False, str(e))
    else:
        print("· live data-mode skipped (set EXP_TABLE + friends to enable)")

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
