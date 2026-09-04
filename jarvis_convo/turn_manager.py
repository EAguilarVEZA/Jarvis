"""
Turn + interruption manager (blueprint §2.2, §8.2). Classifies what a user interruption
MEANS and decides how the assistant reacts — the difference between a natural collaborator
and a chatbot that either ignores you or falls apart when you talk over it.

Classes (§2.2):
  EMERGENCY_STOP  repeated stop/cancel → halt speech before any more reasoning is voiced.
  CORRECTION      "no", "that's wrong", "not what I mean" → invalidate current trajectory.
  BACKCHANNEL     "yeah", "uh-huh", "right" → do NOT cancel; keep talking.
  COLLABORATIVE   user adds info while assistant speaks → merge into the active task.
  HARD            a clearly new request → cancel current turn, prioritize the new one.
"""
from __future__ import annotations

import re
from enum import Enum


class Interruption(str, Enum):
    NONE = "none"
    EMERGENCY_STOP = "emergency_stop"
    CORRECTION = "correction"
    BACKCHANNEL = "backchannel"
    COLLABORATIVE = "collaborative"
    HARD = "hard"


_STOP = re.compile(r"\b(stop|cancel|halt|quiet|shut up|enough)\b", re.I)
_CORRECTION = re.compile(
    r"^\s*(no+\b|nope\b|wait\b|actually\b|that'?s (not|wrong)|not what i mean|"
    r"you'?re wrong|that'?s incorrect|hold on)", re.I)
_BACKCHANNEL = {"yeah", "yes", "yep", "uh-huh", "uhhuh", "mm", "mmhmm", "mhm", "right",
                "ok", "okay", "sure", "gotcha", "got it", "cool", "nice", "haha", "true"}
_COLLAB_LEAD = re.compile(r"^\s*(and|also|oh,? and|plus|make (it|sure)|add|include|"
                          r"can you also|one more|by the way)\b", re.I)


def _words(t: str) -> list[str]:
    return re.findall(r"[a-z'\-]+", (t or "").lower())


def classify(user_text: str, assistant_speaking: bool, stop_streak: int = 0) -> Interruption:
    """stop_streak = how many consecutive stop-ish utterances have arrived (emergency gate)."""
    t = (user_text or "").strip()
    if not t:
        return Interruption.NONE
    words = _words(t)

    # emergency: explicit stop, or repeated stop language
    if _STOP.search(t) and (stop_streak >= 1 or len(words) <= 3):
        return Interruption.EMERGENCY_STOP
    # correction phrases dominate
    if _CORRECTION.search(t):
        return Interruption.CORRECTION
    # short affirmations while the assistant talks = backchannel (don't cancel)
    if assistant_speaking and len(words) <= 2 and set(words) <= _BACKCHANNEL:
        return Interruption.BACKCHANNEL
    # additive info while speaking = collaborative merge
    if assistant_speaking and _COLLAB_LEAD.search(t):
        return Interruption.COLLABORATIVE
    # otherwise a substantive utterance during speech is a new (hard) request
    if assistant_speaking:
        return Interruption.HARD
    return Interruption.NONE


def should_cancel_tts(kind: Interruption) -> bool:
    return kind in (Interruption.EMERGENCY_STOP, Interruption.CORRECTION, Interruption.HARD)


def should_cancel_generation(kind: Interruption) -> bool:
    # collaborative keeps the generation but replans; backchannel leaves it alone
    return kind in (Interruption.EMERGENCY_STOP, Interruption.CORRECTION, Interruption.HARD)


def should_duck(kind: Interruption) -> bool:
    # lower the volume (don't kill) while we work out a collaborative add
    return kind == Interruption.COLLABORATIVE
