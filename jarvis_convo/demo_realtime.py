"""
Realtime session acceptance tests (blueprint §3, §15, §17) — the server-authoritative event
loop, exercised without audio/WebSocket.
Run:  python3 -m jarvis_convo.demo_realtime
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from realtime_api import RealtimeSession


def _types(evs):
    return [e["type"] for e in evs]


def test_barge_in_emits_cancellation():
    s = RealtimeSession("c1")
    line = "Here is a detailed explanation of the blocking architecture we will avoid."
    s.emit_response(line)
    assert s.speaking
    duck = s.on_speech_started()
    assert _types(duck) == ["assistant.duck"]
    out = s.on_partial("no, stop that", boundary=20)
    assert "tts.chunk.cancelled" in _types(out) and "assistant.generation.cancelled" in _types(out)
    assert not s.speaking
    # only the spoken 20 chars are in history; the rest is not
    hist = " ".join(m.text for m in s.working.moves if m.role == "assistant")
    assert hist == line[:20] and "blocking architecture" not in hist
    return "barge-in emits cancellation + keeps only spoken content"


def test_backchannel_keeps_talking():
    s = RealtimeSession("c2")
    s.emit_response("The plan has three phases we will walk through.")
    out = s.on_partial("uh-huh")
    assert out == [] and s.speaking, "backchannel must not cancel"
    return "backchannel does not cancel the response"


def test_turn_routes_artifact_and_tool():
    s = RealtimeSession("c3")
    ctrl, plan, ctx = s.commit_turn("write me a campaign brief")
    assert plan["kind"] == "artifact" and "artifact.created" in _types(ctrl) and "artifact.completed" in _types(ctrl)
    ctrl2, plan2, _ = s.commit_turn("search the latest cardiology news")
    assert plan2["kind"] == "tool" and "tool.requested" in _types(ctrl2)
    return "turns route to artifact/tool with the right events"


def test_correction_recorded():
    s = RealtimeSession("c4")
    s.commit_turn("set the report to Q2")
    s.emit_response("Okay, building the Q2 report.")
    s.on_partial("no that's wrong", boundary=8)          # barge-in
    ctrl, plan, ctx = s.commit_turn("I meant Q3")
    assert any(m.kind == "correction" for m in s.working.moves), "correction not recorded"
    assert "Q3" in " ".join(ctx["recent"])
    return "correction recorded and dominates context"


def test_response_then_complete():
    s = RealtimeSession("c5")
    evs = s.emit_response("Short answer.")
    assert "tts.chunk.queued" in _types(evs)
    s.on_tts_boundary(len("Short answer."))
    done = s.on_tts_completed()
    assert "tts.chunk.completed" in _types(done) and not s.speaking
    return "full response → tts queued → completed commits full spoken text"


def main():
    for fn in (test_barge_in_emits_cancellation, test_backchannel_keeps_talking,
               test_turn_routes_artifact_and_tool, test_correction_recorded,
               test_response_then_complete):
        print(f"  PASS  {fn()}")
    print("\nOK — realtime event loop: barge-in cancellation, spoken-boundary, backchannel, "
          "tool/artifact routing, and corrections all pass server-side.")


if __name__ == "__main__":
    main()
