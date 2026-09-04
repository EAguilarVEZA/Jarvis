"""
realtime_api — the low-latency conversation transport (blueprint §3, §15, §19 Phase 1 WebRTC).

Carries the canonical event schema (§15) over a WebSocket so the server is the authoritative
owner of session state, turn-taking, and barge-in, while the client is a thin audio I/O layer:

  Client → server:  audio.user.started · asr.partial{text,boundary} · asr.final{text} ·
                    tts.boundary{boundary} · tts.completed
  Server → client:  assistant.duck · tts.chunk.cancelled · assistant.generation.* ·
                    tts.chunk.queued · tool.* · artifact.* · interruption{kind}

RealtimeSession is pure, synchronous, testable logic (no I/O). The /ws/realtime endpoint wraps
it and calls the real model for generation. WebRTC/streaming-ASR adapter notes at the bottom.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jarvis_convo.state import WorkingMemory
from jarvis_convo.memory import MemoryStore
from jarvis_convo.orchestrator import Router
from jarvis_convo.turn_manager import classify, Interruption

router = APIRouter(tags=["realtime"])
log = logging.getLogger("realtime_api")


class RealtimeSession:
    """Server-authoritative turn/barge-in state machine that emits §15 events."""
    def __init__(self, conversation_id: str, memory: MemoryStore | None = None):
        self.cid = conversation_id
        self.working = WorkingMemory()
        self.memory = memory or MemoryStore()
        self.router = Router()
        self.speaking = False
        self.assistant_text = ""
        self.spoken_boundary = 0
        self._stop = 0

    def on_speech_started(self) -> list[dict]:
        # react to audio before we know the words: duck immediately (<150ms path)
        return [{"type": "assistant.duck"}] if self.speaking else []

    def on_partial(self, text: str, boundary: int | None = None) -> list[dict]:
        if not self.speaking:
            return []
        k = classify(text, True, self._stop)
        self._stop = self._stop + 1 if k == Interruption.EMERGENCY_STOP else 0
        if k in (Interruption.NONE, Interruption.BACKCHANNEL):
            return []                                     # echo or "uh-huh" → keep talking
        if boundary is not None:
            self.spoken_boundary = boundary
        # commit ONLY what was actually heard; the rest is cancelled, not remembered (§8.2)
        self.working.commit_assistant_spoken(self.assistant_text[: self.spoken_boundary])
        self.speaking = False
        if k == Interruption.CORRECTION:
            self.working.add_correction(text)            # correction invalidates the trajectory (§2.2)
        return [{"type": "tts.chunk.cancelled"},
                {"type": "assistant.generation.cancelled"},
                {"type": "interruption", "kind": k.value}]

    def commit_turn(self, text: str):
        """Returns (control_events, plan, context). The caller generates the answer then calls
        emit_response()."""
        text = (text or "").strip()
        if not text:
            return [], {"kind": "answer"}, {}
        k = classify(text, self.speaking)
        if k == Interruption.CORRECTION:
            self.working.add_correction(text)
        else:
            self.working.commit_user_turn(text)
        plan = self.router.decide(text)
        ev = [{"type": "turn.user.committed", "text": text}]
        if plan["kind"] == "tool":
            ev += [{"type": "tool.requested", "tool": plan.get("tool")},
                   {"type": "tool.result", "tool": plan.get("tool")}]
        if plan["kind"] == "artifact":
            ev += [{"type": "artifact.created"}, {"type": "artifact.completed"}]
        ctx = {"recent": [m.text for m in self.working.authoritative_context()][-6:]}
        return ev, plan, ctx

    def emit_response(self, text: str) -> list[dict]:
        self.assistant_text = text
        self.spoken_boundary = 0
        self.speaking = True
        return [{"type": "assistant.generation.started"},
                {"type": "assistant.generation.completed", "text": text},
                {"type": "tts.chunk.queued", "text": text}]

    def on_tts_boundary(self, boundary: int) -> list[dict]:
        self.spoken_boundary = boundary
        return []

    def on_tts_completed(self) -> list[dict]:
        self.working.commit_assistant_spoken(self.assistant_text)
        self.speaking = False
        return [{"type": "tts.chunk.completed"}]


async def _generate(text: str, ctx: dict, cid: str = "rt") -> str:
    # Voice generation goes through the SAME ConversationRuntime as text — same tools, memory,
    # provider routing (directive P0: no second simplified realtime brain).
    try:
        from conversation_runtime import RUNTIME
        r = await RUNTIME.run_turn(cid, text)
        return r.answer or f"Okay — {text}"
    except Exception as e:  # noqa
        log.warning("realtime generate failed: %s", e)
        return f"Okay — {text}"


@router.websocket("/ws/realtime")
async def realtime_ws(ws: WebSocket):
    await ws.accept()
    sess = RealtimeSession(conversation_id="rt")
    try:
        while True:
            ev = json.loads(await ws.receive_text())
            t = ev.get("type")
            if t == "audio.user.started":
                outs = sess.on_speech_started()
            elif t == "asr.partial":
                outs = sess.on_partial(ev.get("text", ""), ev.get("boundary"))
            elif t in ("asr.final", "turn.user.committed"):
                ctrl, plan, ctx = sess.commit_turn(ev.get("text", ""))
                for o in ctrl:
                    await ws.send_text(json.dumps(o))
                ans = await _generate(ev.get("text", ""), ctx, sess.cid)
                for o in sess.emit_response(ans):
                    await ws.send_text(json.dumps(o))
                continue
            elif t == "tts.boundary":
                outs = sess.on_tts_boundary(int(ev.get("boundary", 0)))
            elif t == "tts.completed":
                outs = sess.on_tts_completed()
            else:
                outs = []
            for o in outs:
                await ws.send_text(json.dumps(o))
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa
        log.warning("realtime ws error: %s", e)
        try:
            await ws.close()
        except Exception:
            pass


# ── WebRTC / streaming-ASR adapter (what remains for sub-150ms audio) ────────
# The event protocol above is transport-agnostic. To reach the blueprint's <150ms barge-in:
#   1. Media: browser opens a WebRTC PeerConnection (mic → server) via a TURN/STUN server;
#      audio frames are sent over the RTP path, control events over a WebRTC DataChannel that
#      speaks exactly the JSON events above (or this WebSocket for a prototype).
#   2. Streaming ASR: feed the inbound audio to a streaming recognizer (Deepgram/Whisper-
#      streaming/local) that emits asr.partial (~200-400ms) and asr.final into this session.
#   3. Streaming TTS: synthesize in small chunks; the client reports tts.boundary as it plays,
#      so on barge-in the server commits only the spoken prefix (already handled here).
# None of the session logic changes — only the audio ingress/egress adapters are added.
