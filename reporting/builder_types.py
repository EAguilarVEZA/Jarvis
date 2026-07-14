"""
Report builder — data model.

Three core types:
  - ReportIntent: the user's eventual report definition (table, metrics, dimensions, filters, time)
  - BuilderState:  the conversation's current progress (what's known, what's missing, what state)
  - Question:     a single clarifying question Jarvis is about to ask

Everything is JSON-serializable so it can ride over the WebSocket to the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import Any, Optional


# ─── Builder lifecycle states ────────────────────────────────

class BuilderPhase(str, Enum):
    """
    Phases of the report-building conversation.

    The conversation is event-driven, not strictly sequential — we may
    skip phases that are already filled in from the user's initial message.
    """
    IDLE = "idle"                          # not in builder mode
    AWAITING_SOURCE = "awaiting_source"    # which dataset / service line?
    AWAITING_TIME = "awaiting_time"        # what date range?
    AWAITING_METRICS = "awaiting_metrics"  # which metrics?
    AWAITING_DIMENSIONS = "awaiting_dimensions"  # how to break down?
    AWAITING_FILTERS = "awaiting_filters"  # any filters?
    CONFIRMING = "confirming"              # showing summary card, awaiting "run"
    RUNNING = "running"                    # query is executing
    COMPLETE = "complete"                  # results ready


class QuestionKind(str, Enum):
    """What kind of input the question is asking for."""
    SOURCE_CHOICE = "source_choice"        # which service line or data source
    TIME_CHOICE = "time_choice"            # date range (chips or custom)
    METRIC_CHOICE = "metric_choice"        # which metrics from the table's offerings
    DIMENSION_CHOICE = "dimension_choice"  # how to break down
    FILTER_CHOICE = "filter_choice"        # which filters
    CONFIRM = "confirm"                    # confirmation card before running
    FREE_TEXT = "free_text"                # fallback for ambiguous cases


# ─── The report intent (what we're going to query) ───────────

@dataclass
class TimeWindow:
    """A date range. Either preset_name OR explicit start/end."""
    preset: Optional[str] = None           # "mtd", "last_30d", "last_quarter", "ytd", etc.
    start: Optional[str] = None            # ISO date "YYYY-MM-DD"
    end: Optional[str] = None              # ISO date "YYYY-MM-DD"
    label: Optional[str] = None            # human label for the chosen window

    def is_specified(self) -> bool:
        return bool(self.preset or (self.start and self.end))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FilterClause:
    """A single filter to apply. Format depends on type."""
    column: str
    op: str                                # "=", "!=", "IN", "NOT IN", "IS TRUE", "IS FALSE"
    value: Any = None                      # may be scalar, list, or None for IS TRUE/IS FALSE

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReportIntent:
    """
    The user's eventual report — what we're going to run against BigQuery.
    Fields are filled in over the course of the conversation.
    """
    table: Optional[str] = None            # 'data_mart_cardiology.combined_campaign_stats'
    service_line: Optional[str] = None     # 'cardiology' (cached for convenience; redundant with table)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[FilterClause] = field(default_factory=list)
    time_window: TimeWindow = field(default_factory=TimeWindow)
    limit: Optional[int] = None            # for top-N queries
    order_by: Optional[str] = None         # which metric to sort by
    order_desc: bool = True

    # Optional metadata for display
    title: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def is_complete(self) -> bool:
        """Have we collected enough to run?"""
        return bool(
            self.table
            and self.metrics
            and self.time_window.is_specified()
        )

    def missing_required(self) -> list[str]:
        """What's still needed?"""
        gaps = []
        if not self.table:
            gaps.append("source")
        if not self.metrics:
            gaps.append("metrics")
        if not self.time_window.is_specified():
            gaps.append("time")
        return gaps

    def to_dict(self) -> dict:
        d = asdict(self)
        # Make sure nested dataclasses serialize cleanly
        return d


# ─── Questions and options ─────────────────────────────────

@dataclass
class Option:
    """A tappable choice shown to the user. value is what gets stored if picked."""
    label: str
    value: str
    description: Optional[str] = None      # subtitle, e.g., "30 days, MTD"
    is_default: bool = False
    is_recommended: bool = False           # surface a 'recommended' tag


@dataclass
class Question:
    """A single clarifying question to put to the user."""
    kind: QuestionKind
    prompt: str                            # what Jarvis says / shows
    options: list[Option] = field(default_factory=list)  # tappable choices
    allow_free_text: bool = True           # always allow free-text fallback
    target_field: Optional[str] = None     # which intent field this question fills

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "prompt": self.prompt,
            "options": [asdict(o) for o in self.options],
            "allow_free_text": self.allow_free_text,
            "target_field": self.target_field,
        }


# ─── Builder state (the whole conversation) ─────────────────

@dataclass
class BuilderState:
    """
    The conversation's current progress. One instance per session.

    intent: what we know so far
    phase:  where we are in the flow
    history: ordered list of (question, answer) pairs for backtracking
    pending: the question Jarvis just asked, waiting for an answer
    """
    intent: ReportIntent = field(default_factory=ReportIntent)
    phase: BuilderPhase = BuilderPhase.IDLE
    pending: Optional[Question] = None
    history: list[dict] = field(default_factory=list)
    session_defaults: dict = field(default_factory=dict)  # carry-forward across builds
    last_result: Optional[dict] = None    # filled in when RUNNING → COMPLETE
    error: Optional[str] = None           # any validation/runtime error

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.to_dict(),
            "phase": self.phase.value,
            "pending": self.pending.to_dict() if self.pending else None,
            "history": self.history,
            "session_defaults": self.session_defaults,
            "last_result": self.last_result,
            "error": self.error,
        }


# ─── A response from the builder (what to send to the UI) ───

@dataclass
class BuilderResponse:
    """
    What the builder returns after processing user input.
    The frontend renders this — either a question (with chips) or a summary card or a result.
    """
    state: BuilderState
    spoken: Optional[str] = None           # what Jarvis says aloud
    ui_update: Optional[dict] = None       # structured UI hint for the panel
    done: bool = False                     # True when phase is COMPLETE or IDLE

    def to_dict(self) -> dict:
        return {
            "state": self.state.to_dict(),
            "spoken": self.spoken,
            "ui_update": self.ui_update,
            "done": self.done,
        }
