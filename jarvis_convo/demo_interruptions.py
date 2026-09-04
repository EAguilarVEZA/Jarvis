"""
Interruption acceptance tests (blueprint §17, §22) — simulated against the core state machine.
Proves the conversational behaviors WITHOUT audio hardware: barge-in stops speech and drops
unheard content, corrections dominate, backchannels don't cancel, emergency stop halts,
collaborative adds merge instead of restarting.

Run:  python3 -m jarvis_convo.demo_interruptions
"""
from __future__ import annotations

from .orchestrator import Orchestrator
from .turn_manager import Interruption, classify


def scenario_mid_sentence_stop():
    o = Orchestrator("c1")
    line = "Here is a detailed technical approach that uses a blocking request response loop."
    o.speak(line)
    o.tts.advance(25)                                   # user has heard ~25 chars
    o.on_user_speech_started()                          # barge-in
    kind = o.on_asr_partial("No, stop.")
    o.on_user_turn_committed("I want it to keep listening while it talks.", kind)
    heard = o.state.spoken_text()
    assert kind in (Interruption.EMERGENCY_STOP, Interruption.CORRECTION), kind
    assert o.tts.cancelled_chunks == 1, "TTS must be cancelled on barge-in"
    assert o.gen.cancelled, "generation must be cancelled"
    assert heard == line[:25], "only the spoken portion is retained"
    # the unheard remainder must NOT be in shared history
    hist = " ".join(m.text for m in o.memory.moves if m.role == "assistant")
    assert "blocking request response loop" not in hist, "unheard content leaked into history"
    return "mid-sentence stop"


def scenario_correction_dominates():
    o = Orchestrator("c2")
    o.memory.commit_user_turn("Set the report to Q2.")
    o.speak("Great, I'll build the Q2 report.")
    o.tts.advance(10)
    o.on_user_speech_started()
    kind = o.on_asr_partial("No, that's wrong — I meant Q3.")
    o.on_user_turn_committed("I meant Q3.", kind)
    ctx = o.memory.authoritative_context()
    assert kind == Interruption.CORRECTION
    # the correction is the highest-priority (last) context item; the assistant assumption is superseded
    assert ctx[-1].kind == "correction" and "Q3" in ctx[-1].text
    assert any(m.superseded for m in o.memory.moves if m.role == "assistant"), "assumption not superseded"
    return "correction dominates"


def scenario_backchannel_does_not_cancel():
    o = Orchestrator("c3")
    o.speak("So the plan has three phases, starting with the realtime core, then tools, then memory.")
    o.tts.advance(20)
    o.on_user_speech_started()
    kind = o.on_asr_partial("uh-huh")
    assert kind == Interruption.BACKCHANNEL
    assert o.tts.cancelled_chunks == 0, "backchannel must NOT cancel speech"
    assert o.tts.playing and not o.tts.ducked, "assistant keeps talking"
    return "backchannel ignored"


def scenario_emergency_stop():
    o = Orchestrator("c4")
    o.speak("Let me walk through the entire architecture in detail, starting from the top...")
    o.tts.advance(15)
    o.on_user_speech_started()
    kind = o.on_asr_partial("stop")
    assert kind == Interruption.EMERGENCY_STOP
    assert o.tts.cancelled_chunks == 1 and not o.tts.playing, "must halt immediately"
    return "emergency stop"


def scenario_collaborative_merge():
    o = Orchestrator("c5")
    o.on_user_turn_committed("Draft a campaign brief.")            # active task created
    o.speak("Okay, drafting the campaign brief now.")
    o.tts.advance(12)
    o.on_user_speech_started()
    kind = o.on_asr_partial("oh and make it about cardiology")
    o.on_user_turn_committed("make it about cardiology", kind)
    assert kind == Interruption.COLLABORATIVE
    assert o.state.active_task and "cardiology" in " ".join(o.state.active_task.get("additions", [])), \
        "collaborative info must merge into the active task, not restart it"
    assert o.state.active_task.get("request") == "Draft a campaign brief.", "task should not be discarded"
    return "collaborative merge"


def main():
    for fn in (scenario_mid_sentence_stop, scenario_correction_dominates,
               scenario_backchannel_does_not_cancel, scenario_emergency_stop,
               scenario_collaborative_merge):
        name = fn()
        print(f"  PASS  {name}")
    # a couple of unit checks on the classifier
    assert classify("what's the weather in Orlando", assistant_speaking=True) == Interruption.HARD
    assert classify("yeah", assistant_speaking=True) == Interruption.BACKCHANNEL
    print("\nOK — all §17 interruption acceptance tests pass on the conversational core.")


if __name__ == "__main__":
    main()
