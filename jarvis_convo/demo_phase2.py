"""
Phase 2 + artifact acceptance tests (blueprint §17, §6.2, §10, §13).
Run:  python3 -m jarvis_convo.demo_phase2
"""
from __future__ import annotations

from .tools import (ExecutionController, Perm, Status, ToolSpec, default_controller)
from .artifacts import ArtifactService, ArtifactStatus


def test_read_tool_provenance():
    c = default_controller()
    r = c.execute("web_search", {"query": "orlando health cardiology"})
    assert r.status == Status.SUCCEEDED and r.provenance, r.to_dict()
    r2 = c.execute("calculate", {"expression": "2 + 3 * 4"})
    assert r2.status == Status.SUCCEEDED and r2.output == 14, r2.to_dict()
    return "read tools succeed with provenance"


def test_write_permission_gate():
    c = default_controller()                              # no write grant
    denied = c.execute("send_message", {"to": "a@b.com", "body": "hi"})
    assert denied.status == Status.FAILED and denied.error == "permission_required"
    assert c.exec_count.get("send_message", 0) == 0, "write must not run without permission"
    ok = c.execute("send_message", {"to": "a@b.com", "body": "hi"}, confirm=True)
    assert ok.status == Status.SUCCEEDED and ok.output["message_id"]
    return "read/draft/write separation enforced"


def test_idempotent_write_no_duplicate():
    c = default_controller(grants={"write"})
    rid = "req-123"
    a = c.execute("send_message", {"to": "x@y.com", "body": "ping"}, request_id=rid)
    b = c.execute("send_message", {"to": "x@y.com", "body": "ping"}, request_id=rid)  # retry
    assert a.status == Status.SUCCEEDED and b.deduped is True
    assert c.exec_count["send_message"] == 1, "retry must not duplicate the external action"
    return "idempotent write — no duplicate on retry"


def test_safety_pre_execution():
    c = default_controller(grants={"write"})
    r = c.execute("send_message", {"to": "x@y.com", "body": "please wire transfer to 123"})
    assert r.status == Status.FAILED and r.error == "blocked_by_policy"
    return "safety policy blocks before execution"


def test_tool_failure_then_fallback():
    c = default_controller()
    c.register(ToolSpec("flaky_search", "always fails", Perm.READ,
                        {"query": {"type": "str", "required": True}},
                        lambda a: (_ for _ in ()).throw(RuntimeError("upstream 503")), max_retries=2))
    fail = c.execute("flaky_search", {"query": "x"})
    assert fail.status == Status.FAILED and "503" in fail.error and fail.attempts == 3
    fallback = c.execute("web_search", {"query": "x"})               # orchestrator offers fallback
    assert fallback.status == Status.SUCCEEDED
    return "tool failure reported accurately + fallback executes"


def test_artifact_completed_and_returned():
    svc = ArtifactService()
    a = svc.run("markdown", "brief.md", lambda cancel: "# Campaign Brief\n\nGoal: more heart-quiz leads.")
    assert a.status == ArtifactStatus.COMPLETED and a.filename == "brief.md" and len(a.content) > 0
    return "artifact created → validated → completed with filename"


def test_artifact_validation_failure_is_honest():
    svc = ArtifactService()
    a = svc.run("markdown", "empty.md", lambda cancel: "")           # builder produced nothing
    assert a.status == ArtifactStatus.FAILED and a.error == "artifact is empty"
    assert a.status != ArtifactStatus.COMPLETED, "must not claim completion"
    return "validation failure returns partial + exact error"


def test_requirements_change_mid_build():
    svc = ArtifactService()
    flag = {"cancel": True}
    v1 = svc.run("markdown", "plan.md", lambda cancel: "old plan for Q2", cancel=lambda: flag["cancel"])
    assert v1.status == ArtifactStatus.FAILED and v1.error == "cancelled"
    v2 = svc.regenerate(v1, lambda cancel: "# Plan\n\nNew plan for Q3 cardiology")   # regen from new reqs
    assert v2.status == ArtifactStatus.COMPLETED and v2.version == 2 and "Q3" in v2.content
    return "mid-build requirement change → cancel + regenerate"


def main():
    for fn in (test_read_tool_provenance, test_write_permission_gate,
               test_idempotent_write_no_duplicate, test_safety_pre_execution,
               test_tool_failure_then_fallback, test_artifact_completed_and_returned,
               test_artifact_validation_failure_is_honest, test_requirements_change_mid_build):
        print(f"  PASS  {fn()}")
    print("\nOK — tool registry (permissions, safety, idempotency, fallback) and artifact "
          "completion (validate-before-complete, honest failure, regenerate) all pass.")


if __name__ == "__main__":
    main()
