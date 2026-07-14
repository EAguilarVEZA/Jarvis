"""
BuilderSession — per-WebSocket wrapper for the ReportBuilder.

Handles the back-and-forth between server.py's voice dispatch and the
ReportBuilder state machine. One instance per active WebSocket.

Two responsibilities:
  1. Maintain the builder instance + know whether we're currently "in builder mode"
  2. Translate user_text → ReportBuilder calls → WebSocket-ready response dicts

The actual `ws.send_json(...)` calls happen in server.py. This module just
returns the dicts to send.

Message contract — what this module produces:

  {"type": "builder:question",  "prompt": "...",  "options": [...], "phase": "..."}
    Sent when the builder asks a question. Frontend renders chips.

  {"type": "builder:state",  "state": {...intent fields...}}
    Sent on every step so the frontend can update the state summary panel.

  {"type": "builder:result",  "rows": [...], "row_count": N, "sql": "...", ...}
    Sent when the builder runs the query successfully.

  {"type": "builder:error",  "message": "..."}
    Sent when validation or query fails.

  {"type": "builder:done"}
    Sent when builder mode ends (completed, cancelled, or error). Frontend hides panel.

The audio/captions still flow through the existing {"type": "audio", "data": ..., "text": ...}
path — BuilderSession returns a `spoken` string that server.py passes to the TTS pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from reporting.builder import ReportBuilder
from reporting.builder_types import BuilderPhase, BuilderResponse
from reporting.catalog import WarehouseCatalog
from reporting.intent_extractor import is_report_trigger

log = logging.getLogger("jarvis.reporting.session")


# Sentinel value the user can say to exit builder mode at any time
CANCEL_PHRASES = (
    "cancel", "cancel that", "never mind", "stop the report",
    "exit report", "exit builder", "quit builder", "forget it",
)


def is_cancel_phrase(text: str) -> bool:
    """Did the user just ask to abort builder mode?"""
    t = text.lower().strip()
    # Exact match or prefixed by short interjection ("um, cancel that")
    return any(t == p or t.endswith(" " + p) or t.startswith(p) for p in CANCEL_PHRASES)


class BuilderSession:
    """
    Per-WebSocket wrapper around ReportBuilder.

    Typical lifecycle inside one WebSocket connection:

        session = BuilderSession(catalog, query_runner)

        # User says "let's build a report on cardiology"
        responses = session.start("let's build a report on cardiology")
        # → [{"type": "builder:state", ...}, {"type": "builder:question", ...}]

        # User taps a chip OR speaks an answer
        responses = session.handle_input("last 30 days")
        # → [{"type": "builder:state", ...}, {"type": "builder:question", ...}]

        # ... eventually
        responses = session.handle_input("run it")
        # → [{"type": "builder:state", ...}, {"type": "builder:result", ...}, {"type": "builder:done"}]

    session.in_builder_mode tells the dispatcher whether subsequent
    transcripts should be routed here.
    """

    def __init__(self, catalog: WarehouseCatalog, query_runner=None):
        self.catalog = catalog
        self.query_runner = query_runner
        self.builder: Optional[ReportBuilder] = None

    # ── Status ──────────────────────────────────────────

    @property
    def in_builder_mode(self) -> bool:
        """True if there's an active builder waiting for user input."""
        if self.builder is None:
            return False
        phase = self.builder.state.phase
        return phase not in (BuilderPhase.IDLE, BuilderPhase.COMPLETE)

    # ── Entry points ────────────────────────────────────

    def start(self, text: str) -> list[dict]:
        """
        Begin a new builder conversation. Called when is_report_trigger() fires.

        Returns a list of WebSocket message dicts to send (state + question),
        plus a `_spoken` key on the last dict telling server.py what Jarvis says aloud.
        """
        log.info(f"Builder session START: {text!r}")
        if self.builder is None:
            self.builder = ReportBuilder(self.catalog, query_runner=self.query_runner)
        else:
            # Restart cleanly while preserving session defaults
            defaults = self.builder.state.session_defaults
            self.builder = ReportBuilder(self.catalog, query_runner=self.query_runner)
            self.builder.state.session_defaults = defaults

        response = self.builder.start(text)
        return self._render(response)

    def handle_input(self, text: str) -> list[dict]:
        """
        Process the user's next input while in builder mode.

        text can be:
          - a chip value sent back as transcript (e.g. "service_line:cardiology", "__run__")
          - free-text from voice ("last 30 days", "by campaign")
          - a cancel phrase

        Returns WebSocket message dicts plus a `_spoken` string on the last one.
        """
        log.info(f"Builder session ANSWER: {text!r}")

        # Cancel phrase from voice
        if is_cancel_phrase(text):
            return self.cancel()

        if self.builder is None:
            # Defensive — shouldn't happen if dispatcher checks in_builder_mode first
            return [{"type": "builder:error", "message": "No active builder.", "_spoken": None}]

        # Pass BOTH value and free_text. Per-phase handlers in builder.py decide:
        #   (a) If value matches a known chip prefix (e.g. "service_line:cardiology",
        #       "__run__", "last_30d"), use it directly.
        #   (b) Otherwise treat as voice — re-parse free_text through the intent
        #       extractor / time parser / catalog resolver.
        # This is what makes voice answers work without chips.
        wrapped: dict = {"value": text, "free_text": text}

        response = self.builder.answer(wrapped)
        return self._render(response)

    def cancel(self) -> list[dict]:
        """User asked to abort."""
        log.info("Builder session CANCEL")
        if self.builder:
            self.builder.cancel()
        self.builder = None
        return [
            {
                "type": "builder:done",
                "reason": "cancelled",
                "_spoken": "Cancelled, sir.",
            }
        ]

    # ── Internal: turn BuilderResponse → list of message dicts ──

    def _render(self, response: BuilderResponse) -> list[dict]:
        """
        Turn a single BuilderResponse into the sequence of dicts to ship over WS.

        We always send a `builder:state` (so the panel can show the running
        intent summary) and then either a question, a result, or a done marker.
        The last dict gets a `_spoken` key telling server.py what Jarvis says.
        """
        msgs: list[dict] = []

        # 1) Always send the current state so the panel can refresh
        msgs.append({
            "type": "builder:state",
            "state": response.state.to_dict(),
        })

        # 2) Decide what to send next based on phase
        phase = response.state.phase

        if phase == BuilderPhase.COMPLETE:
            # Completed run — emit the result + done
            result = response.state.last_result or {}
            msgs.append({
                "type": "builder:result",
                "rows": result.get("rows"),
                "row_count": result.get("row_count", 0),
                "sql": result.get("sql"),
                "params": result.get("params"),
            })
            row_count = result.get("row_count", 0)
            msgs.append({
                "type": "builder:done",
                "reason": "complete",
                "_spoken": f"Done. {row_count} row(s).",
            })

        elif phase == BuilderPhase.IDLE:
            # Hit an error or cancelled mid-flight
            if response.state.error:
                msgs.append({
                    "type": "builder:error",
                    "message": response.state.error,
                })
                msgs.append({
                    "type": "builder:done",
                    "reason": "error",
                    "_spoken": response.spoken or f"I can't run that. {response.state.error}",
                })
            else:
                msgs.append({
                    "type": "builder:done",
                    "reason": "cancelled",
                    "_spoken": response.spoken or "Cancelled, sir.",
                })
            # Drop the builder after a terminal state
            self.builder = None

        else:
            # Still asking questions — send the pending question with chips
            if response.state.pending:
                q = response.state.pending
                msgs.append({
                    "type": "builder:question",
                    "phase": phase.value,
                    "kind": q.kind.value,
                    "prompt": q.prompt,
                    "options": [
                        {
                            "label": o.label,
                            "value": o.value,
                            "description": o.description,
                            "is_recommended": o.is_recommended,
                            "is_default": o.is_default,
                        }
                        for o in q.options
                    ],
                    "_spoken": response.spoken or q.prompt,
                })

        return msgs
