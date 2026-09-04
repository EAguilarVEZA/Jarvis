"""
Dialogue orchestrator (blueprint §5, §16). Event-driven traffic controller: it decides
whether to answer, use a tool, build an artifact, or stay silent — and it treats cancellation
as a first-class operation so the user can interrupt at any time (§20).

Generation and TTS are injected controllers (mockable) so this core is testable without audio.
A real deployment wires GenerationController → LLM gateway (Claude / local vLLM) and
TtsController → streaming Fish/local TTS with cancellable buffers; the logic here is identical.
"""
from __future__ import annotations

from .events import E, EventLog
from .state import Phase, SessionState, WorkingMemory
from . import turn_manager as tm
from .turn_manager import Interruption


class MockGeneration:
    """Stands in for a streaming LLM turn. Records cancellation; the real one streams deltas."""
    def __init__(self):
        self.text = ""
        self.cancelled = False
        self.active = False

    def start(self, text: str):
        self.text, self.cancelled, self.active = text, False, True

    def cancel(self):
        if self.active:
            self.cancelled = True
            self.active = False


class MockTts:
    def __init__(self):
        self.playing = False
        self.ducked = False
        self.cancelled_chunks = 0
        self.played_chars = 0

    def play(self, text: str):
        self.playing, self.ducked = True, False

    def advance(self, chars: int):
        self.played_chars = chars

    def duck(self):
        self.ducked = True

    def cancel(self):
        if self.playing:
            self.cancelled_chunks += 1
        self.playing = False


class Router:
    """Decision policy (§5.1). Minimal here; a real router consults the LLM + tool registry."""
    def decide(self, text: str) -> dict:
        t = text.lower()
        if any(w in t for w in ("write", "create", "make me", "draft", "build a", "document", "report")):
            return {"kind": "artifact", "spec": text}
        if any(w in t for w in ("search", "look up", "latest", "current", "who is", "what's the")):
            return {"kind": "tool", "tool": "web_search", "query": text}
        return {"kind": "answer"}


class Orchestrator:
    def __init__(self, conversation_id: str, gen=None, tts=None, router=None):
        self.log = EventLog(conversation_id)
        self.state = SessionState(conversation_id=conversation_id)
        self.memory = WorkingMemory()
        self.gen = gen or MockGeneration()
        self.tts = tts or MockTts()
        self.router = router or Router()
        self._stop_streak = 0

    # ── event entry (§16 on_event) ───────────────────────────────────────────
    def on_user_speech_started(self):
        self.log.publish(E.AUDIO_USER_STARTED)
        if self.state.phase == Phase.ASSISTANT_SPEAKING:
            # react to the *audio*, before we even know the words: duck immediately (<150ms)
            self.tts.duck()
            self.state.phase = Phase.USER_SPEAKING

    def on_asr_partial(self, text: str):
        self.state.user_partial = text
        self.log.publish(E.ASR_PARTIAL, text=text)
        assistant_was_speaking = self.tts.playing
        kind = tm.classify(text, assistant_was_speaking, self._stop_streak)
        self._stop_streak = self._stop_streak + 1 if kind == Interruption.EMERGENCY_STOP else 0
        if kind == Interruption.BACKCHANNEL:
            # not an interruption — un-duck and keep talking
            self.tts.ducked = False
            return kind
        if kind == Interruption.NONE:
            return kind
        # sync the spoken boundary from playback, then cut so unheard text is never "heard"
        self.state.record_spoken(self.tts.played_chars)
        if tm.should_cancel_tts(kind):
            self.tts.cancel()
            self.log.publish(E.TTS_CANCELLED)
        if tm.should_cancel_generation(kind):
            self.gen.cancel()
            self.log.publish(E.GEN_CANCELLED)
        self.state.cut_at_boundary()                      # keep only spoken text
        # commit ONLY the actually-spoken assistant text to history (§8.2, §20)
        self.memory.commit_assistant_spoken(self.state.spoken_text())
        if kind == Interruption.COLLABORATIVE:
            self.tts.duck()
        return kind

    def on_user_turn_committed(self, text: str, interruption: Interruption = Interruption.NONE):
        self.log.publish(E.TURN_USER_COMMITTED, text=text)
        if interruption == Interruption.CORRECTION:
            self.memory.add_correction(text)              # correction outranks prior assumptions
            self.state.active_task = None                  # invalidate conflicting assumptions
        elif interruption == Interruption.COLLABORATIVE and self.state.active_task:
            self.state.active_task.setdefault("additions", []).append(text)  # merge, don't restart
        else:
            self.memory.commit_user_turn(text)

        plan = self.router.decide(text)
        self.state.active_task = self.state.active_task or {"request": text}
        if plan["kind"] == "tool":
            self.state.phase = Phase.TOOL_RUNNING
            self.log.publish(E.TOOL_REQUESTED, tool=plan["tool"])
            # (execution controller runs the tool cancellably; omitted in the core mock)
            self.log.publish(E.TOOL_RESULT, tool=plan["tool"])
        if plan["kind"] == "artifact":
            self.state.phase = Phase.ARTIFACT_BUILDING
            aid = f"artifact-{self.log.count(E.ARTIFACT_CREATED)+1}"
            self.state.artifact_ids.append(aid)
            self.log.publish(E.ARTIFACT_CREATED, artifact_id=aid)
            # real path: build → validate → complete; never claim invisible background work (§10, §20)
            self.log.publish(E.ARTIFACT_COMPLETED, artifact_id=aid)
        return plan

    def speak(self, text: str):
        """Assistant begins an utterance (streamed in prod once a stable prefix exists)."""
        self.state.begin_assistant_turn()
        self.state.assistant_generated_text = text
        self.gen.start(text)
        self.log.publish(E.GEN_STARTED)
        self.tts.play(text)
        self.log.publish(E.TTS_PLAYING)

    def finish_speaking(self):
        self.state.record_spoken(len(self.state.assistant_generated_text))
        self.memory.commit_assistant_spoken(self.state.spoken_text())
        self.tts.playing = False
        self.state.phase = Phase.IDLE
        self.log.publish(E.TTS_COMPLETED)
