"""
Explicit session state + working memory (blueprint §4). State is maintained explicitly, NOT
left to the model's context window (§4, Non-Goals). The decisive rule: corrections outrank
earlier assertions, and the assistant's *cancelled remainder* is never treated as heard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"
    ASSISTANT_SPEAKING = "assistant_speaking"
    TOOL_RUNNING = "tool_running"
    ARTIFACT_BUILDING = "artifact_building"


@dataclass
class SessionState:
    conversation_id: str
    session_id: str = ""
    active_topic: str = ""
    active_task: dict | None = None
    phase: Phase = Phase.IDLE

    user_partial: str = ""                 # current partial transcript
    assistant_generated_text: str = ""     # full text the model produced this turn
    spoken_boundary: int = 0               # chars actually voiced to the user
    cancelled_remainder: str = ""          # produced-but-never-heard text (must NOT enter history)

    pending_tools: list[str] = field(default_factory=list)
    cancellation_tokens: dict[str, bool] = field(default_factory=dict)   # tool_id -> cancelled
    unresolved_refs: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)

    def spoken_text(self) -> str:
        return self.assistant_generated_text[: self.spoken_boundary]

    def begin_assistant_turn(self):
        self.assistant_generated_text = ""
        self.spoken_boundary = 0
        self.cancelled_remainder = ""
        self.phase = Phase.ASSISTANT_SPEAKING

    def record_spoken(self, boundary_chars: int):
        """Advance the spoken cursor as TTS plays (client reports the playback boundary)."""
        self.spoken_boundary = min(max(boundary_chars, self.spoken_boundary),
                                   len(self.assistant_generated_text))

    def cut_at_boundary(self):
        """Barge-in: keep only what was actually spoken; stash the rest as cancelled."""
        self.cancelled_remainder = self.assistant_generated_text[self.spoken_boundary:]
        self.assistant_generated_text = self.spoken_text()


@dataclass
class Move:
    role: str            # user | assistant
    text: str
    kind: str = "turn"   # turn | correction
    superseded: bool = False


class WorkingMemory:
    """Compact rolling record of recent moves (§4.2). Corrections get priority; a stale
    summary never overrides verbatim recent turns."""
    def __init__(self, max_moves: int = 20):
        self.moves: list[Move] = []
        self.max_moves = max_moves

    def commit_user_turn(self, text: str):
        self.moves.append(Move("user", text, "turn"))
        self._trim()

    def commit_assistant_spoken(self, spoken_text: str):
        # ONLY what was actually heard enters shared history (§8.2, §20)
        if spoken_text.strip():
            self.moves.append(Move("assistant", spoken_text, "turn"))
        self._trim()

    def add_correction(self, text: str):
        # a correction supersedes the most recent assistant assumption
        for m in reversed(self.moves):
            if m.role == "assistant" and not m.superseded:
                m.superseded = True
                break
        self.moves.append(Move("user", text, "correction"))
        self._trim()

    def authoritative_context(self) -> list[Move]:
        """Recent verbatim turns with corrections ranked above superseded assertions."""
        live = [m for m in self.moves if not m.superseded]
        corrections = [m for m in live if m.kind == "correction"]
        others = [m for m in live if m.kind != "correction"]
        return others + corrections   # corrections last = highest priority for the planner

    def _trim(self):
        if len(self.moves) > self.max_moves:
            self.moves = self.moves[-self.max_moves:]
