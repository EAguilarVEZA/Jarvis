"""
Anomaly + root-cause demo (spec §11.8-11.10, §2.4). build → NLP → detect issues →
investigate the top issue → print the structured FindingBundle the LLM would narrate.

Run:  python3 -m martin_experience.rc_demo
"""
from __future__ import annotations

import json

from .store import Store
from .demo_slice import build
from .services.nlp.pipeline import NlpPipeline
from .services.anomaly import engine as anomaly
from .services.investigation.engine import investigate_root_cause

START, END = "2026-08-01", "2026-08-31"


def main():
    store = Store(":memory:")
    build(store)
    NlpPipeline(store).run()

    issues = anomaly.detect_issues(store, "location", START, END)
    print(f"issue candidates detected: {len(issues)}")
    for i in issues:
        print(f"  [{i['severity']:6}] {i['entity_id']:14} {i['metric_id']:22} "
              f"value={i['current_value']} n={i['sample_n']} conf={i['confidence']}")

    top = issues[0]
    bundle = investigate_root_cause(
        store, top["entity_type"], top["entity_id"], START, END,
        question=f"Why is patient experience low at {top['entity_id']}?")

    print("\n=== FindingBundle (deterministic — the LLM only narrates this) ===")
    print("Q:", bundle["question"])
    print("Entity:", bundle["entity"]["name"], "| period", bundle["period"]["start"], "→", bundle["period"]["end"])
    print("\nFindings:")
    for f in bundle["findings"]:
        print(f"  {f['metric_id']:22} current={f['current']} (n={f['n_current']}) method={f['method']}")
    print("\nCandidate drivers (ranked by negative prevalence):")
    for d in bundle["candidate_drivers"]:
        print(f"  {d['topic_id']:28} {d['name']:24} neg_events={d['negative_events']} share={d['share']}")
    print("\nEvidence:")
    for e in bundle["feedback_evidence"]:
        print(f"  [{e['rating']}★ {e['source_record_id']}] {e['quote'][:70]}")
    print("\nLimitations:", "; ".join(bundle["limitations"]))
    print("Engine versions:", json.dumps(bundle["engine_versions"]))

    # verify structure + that a real driver surfaced with evidence
    assert issues, "expected at least one issue candidate"
    assert bundle["candidate_drivers"], "expected candidate drivers"
    assert bundle["feedback_evidence"], "expected evidence"
    driver_ids = {d["topic_id"] for d in bundle["candidate_drivers"]}
    assert driver_ids & {"ACCESS.SCHEDULING", "ACCESS.PHONE_ACCESS", "FIN.BILLING",
                         "WAIT.DELAY_COMMUNICATION"}, driver_ids
    # n-guard: no issue should be raised on a single-review window
    thin = anomaly.detect_issues(store, "location", "2026-08-20", "2026-08-20", persist=False)
    assert thin == [], "minimum-sample gate should suppress a 1-review window"
    print("\nOK — issue detected on adequate sample, root cause + evidence assembled, "
          "thin-sample windows suppressed.")


if __name__ == "__main__":
    main()
