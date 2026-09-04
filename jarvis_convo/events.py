"""
Canonical conversation event schema (blueprint §15). An append-only event stream per
conversation is the single source of truth — no long-running LLM call owns the conversation
(§3). In production the bus is Redis Streams / NATS; here it's an in-process append-only log
with synchronous subscribers so the core state machine is deterministic and testable.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class E:
    """Event type constants (blueprint §15)."""
    AUDIO_USER_STARTED = "audio.user.started"
    AUDIO_USER_STOPPED = "audio.user.stopped"
    ASR_PARTIAL = "asr.partial"
    ASR_FINAL = "asr.final"
    TURN_USER_COMMITTED = "turn.user.committed"
    GEN_STARTED = "assistant.generation.started"
    GEN_DELTA = "assistant.generation.delta"
    GEN_CANCELLED = "assistant.generation.cancelled"
    GEN_COMPLETED = "assistant.generation.completed"
    TTS_QUEUED = "tts.chunk.queued"
    TTS_PLAYING = "tts.chunk.playing"
    TTS_CANCELLED = "tts.chunk.cancelled"
    TTS_COMPLETED = "tts.chunk.completed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_RESULT = "tool.result"
    TOOL_FAILED = "tool.failed"
    TOOL_CANCELLED = "tool.cancelled"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    ARTIFACT_VALIDATED = "artifact.validated"
    ARTIFACT_COMPLETED = "artifact.completed"
    MEMORY_RETRIEVED = "memory.retrieved"
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_WRITTEN = "memory.written"
    TOPIC_CHANGED = "conversation.topic_changed"
    ERROR_RECOVERABLE = "error.recoverable"
    ERROR_FATAL = "error.fatal"


@dataclass
class Event:
    type: str
    data: dict = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)


class EventLog:
    """Append-only per-conversation log + synchronous pub/sub."""
    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self._events: list[Event] = []
        self._seq = itertools.count(1)
        self._subs: list[Callable[[Event], Any]] = []

    def subscribe(self, fn: Callable[[Event], Any]):
        self._subs.append(fn)

    def publish(self, type: str, **data) -> Event:
        ev = Event(type=type, data=data, seq=next(self._seq))
        self._events.append(ev)
        for fn in list(self._subs):
            fn(ev)
        return ev

    def events(self, of_type: str | None = None) -> list[Event]:
        return [e for e in self._events if of_type is None or e.type == of_type]

    def count(self, of_type: str) -> int:
        return sum(1 for e in self._events if e.type == of_type)
