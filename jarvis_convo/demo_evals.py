"""
Run the §18 evaluation suite + verify durable memory survives a reload.
Run:  python3 -m jarvis_convo.demo_evals
"""
from __future__ import annotations

import os
import tempfile

from .evals import run_all
from .persistence import DurableMemory, AuditLog


def main():
    scores = run_all()
    print("Conversation quality scorecard (§18):")
    for k, v in scores.items():
        print(f"  {k:42} {v}")
    for k, v in scores.items():
        if k != "overall":
            assert v == 1.0, f"{k} regressed to {v}"
    assert scores["overall"] == 1.0

    # durable memory survives a restart (new store from the same file)
    d = tempfile.mkdtemp()
    path = os.path.join(d, "mem.json")
    m1 = DurableMemory(path)
    m1.add("project", "Cardiology launch owner is Maria Reyes.", scope="p", tags=["owner"])
    m2 = DurableMemory(path)                          # simulate a restart: reload from disk
    hit = m2.retrieve("who owns cardiology launch", scope="p", k=1)
    assert hit and "Maria Reyes" in hit[0].text, "durable memory did not persist across reload"

    # audit log appends with action ids
    a = AuditLog(os.path.join(d, "audit.jsonl"))
    aid = a.append("tool", "conv1", tool="web_search", status="succeeded")
    assert aid and a.tail(1)[0]["action_id"] == aid and a.tail(1)[0]["type"] == "tool"

    print("\nOK — all §18 metrics at 1.0; durable memory persists across restart; audit log records action IDs.")


if __name__ == "__main__":
    main()
