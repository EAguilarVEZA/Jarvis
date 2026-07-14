"""
ReportBuilder — the state machine that orchestrates the conversation.

Entry points:
    builder = ReportBuilder(catalog)
    response = builder.start("let's build a report on cardiology")
    response = builder.answer("Last 30 days")
    response = builder.answer("All key metrics")
    response = builder.answer("by campaign")
    response = builder.answer("No filters")
    response = builder.answer("Run it")
    # response.state.last_result has the data, response.done == True

Two principles:
    1. Smart asking — only ask for what's missing
    2. Always confirm — even when nothing is missing, show the summary card
"""

from __future__ import annotations

from typing import Any, Optional

from reporting.catalog import WarehouseCatalog
from reporting.builder_types import (
    BuilderPhase, BuilderResponse, BuilderState, FilterClause,
    Option, Question, QuestionKind, ReportIntent, TimeWindow,
)
from reporting.intent_extractor import extract_intent, is_report_trigger
from reporting.question_generator import next_question
from reporting.time_parser import resolve_preset, parse_time_expression
from reporting.sql_generator import generate_sql


class ReportBuilder:
    """Stateful coordinator. Hold one instance per conversation/session."""

    def __init__(self, catalog: WarehouseCatalog,
                 query_runner: Optional[callable] = None):
        """
        Args:
            catalog: the WarehouseCatalog
            query_runner: optional callable that takes (sql, params) and returns rows.
                          If None, the builder generates SQL but doesn't execute it
                          (useful for testing or for the UI to display the SQL).
        """
        self.catalog = catalog
        self.query_runner = query_runner
        self.state = BuilderState()

    # ─── Public API ───────────────────────────────────────

    def start(self, text: str, defaults: Optional[dict] = None) -> BuilderResponse:
        """
        Begin a new report-building conversation. Parses initial intent and
        produces the next question (or confirmation if everything's already specified).
        """
        # Reset state but preserve session defaults
        prior_defaults = self.state.session_defaults
        self.state = BuilderState(session_defaults=defaults or prior_defaults)

        # Extract whatever we can from the opening message
        result = extract_intent(text, self.catalog, self.state.session_defaults)
        self.state.intent = result.intent

        # Move to first phase
        return self._advance()

    def answer(self, user_input: str | dict) -> BuilderResponse:
        """
        Process the user's answer to the pending question.

        user_input can be:
          - a string (free text, e.g. from voice)
          - a dict with {value: "..."} (a tappable chip choice from the UI)
        """
        if self.state.phase == BuilderPhase.IDLE:
            # Treat answer-without-pending as a fresh start
            return self.start(user_input if isinstance(user_input, str) else "")

        # Extract the raw value
        if isinstance(user_input, dict):
            value = user_input.get("value", "")
            free_text = user_input.get("free_text", "")
        else:
            value = user_input
            free_text = user_input

        # Record in history
        self.state.history.append({
            "phase": self.state.phase.value,
            "question": self.state.pending.prompt if self.state.pending else None,
            "answer": str(user_input),
        })

        # Route by current phase
        if self.state.phase == BuilderPhase.AWAITING_SOURCE:
            self._handle_source(value, free_text)
        elif self.state.phase == BuilderPhase.AWAITING_TIME:
            self._handle_time(value, free_text)
        elif self.state.phase == BuilderPhase.AWAITING_METRICS:
            self._handle_metrics(value, free_text)
        elif self.state.phase == BuilderPhase.AWAITING_DIMENSIONS:
            self._handle_dimensions(value, free_text)
        elif self.state.phase == BuilderPhase.AWAITING_FILTERS:
            self._handle_filters(value, free_text)
        elif self.state.phase == BuilderPhase.CONFIRMING:
            return self._handle_confirmation(value)

        return self._advance()

    def cancel(self) -> BuilderResponse:
        """Abort the builder. State returns to IDLE."""
        self.state = BuilderState(session_defaults=self.state.session_defaults)
        return BuilderResponse(state=self.state, spoken="Cancelled.", done=True)

    # ─── Internal: per-phase answer handlers ──────────────

    def _handle_source(self, value: str, free_text: str) -> None:
        """User picked a source (service line or other dataset).

        Three input shapes accepted:
          1. Chip values: "service_line:cardiology", "source:ga4", "data_mart_x.tbl"
          2. Voice free-text: "cardiology", "the heart team", "let's do neuroscience"
          3. Anything else → leave intent empty, next_question() will re-ask
        """
        # Path 1: chip value
        if value.startswith("service_line:"):
            sl_key = value.split(":", 1)[1]
            self.state.intent.service_line = sl_key
            self.state.intent.table = f"data_mart_{sl_key}.combined_campaign_stats" \
                if sl_key != "all" else "data_mart_all.combined_campaign_stats"
            return
        if value.startswith("source:"):
            src_key = value.split(":", 1)[1]
            tables = self.catalog.tables_by_source(src_key)
            if tables:
                self.state.intent.table = tables[0].full_name
            return
        if "." in value and not " " in value:
            # Direct table pick (no spaces — distinguishes from "data warehouse")
            self.state.intent.table = value
            return

        # Path 2: voice free-text — use the catalog's resolver (has word-boundary matching)
        from reporting.catalog import resolve
        text = free_text or value
        sl = resolve(text)
        if sl:
            self.state.intent.service_line = sl.key
            self.state.intent.table = f"{sl.dataset}.combined_campaign_stats"
            return
        # Path 3: couldn't resolve — leave intent unchanged so question re-asks

    def _handle_time(self, value: str, free_text: str) -> None:
        """User picked a time range — chip preset OR voice phrase."""
        # Path 1: chip preset value ("last_30d", "mtd", etc.)
        tw = resolve_preset(value)
        if tw and tw.start and tw.end:
            self.state.intent.time_window = tw
            return
        # Path 2: voice/free-text expression ("last 30 days", "Q1 2024", "April")
        tw = parse_time_expression(free_text or value)
        if tw:
            self.state.intent.time_window = tw
            return
        # Path 3: couldn't parse — leave time window empty so question re-asks

    def _handle_metrics(self, value: str, free_text: str) -> None:
        """User picked metric(s) — chip OR voice phrase."""
        # Path 1: sentinel "all defaults"
        if value == "__all_defaults__":
            table = self.catalog.get(self.state.intent.table)
            if table:
                self.state.intent.metrics = list(table.default_metrics)
            return

        # Path 2: chip value matches a known metric on this table
        if self.state.intent.table:
            table = self.catalog.get(self.state.intent.table)
            if table:
                known_metrics = {c.name for c in table.metrics()} | {dm.name for dm in table.derived_metrics}
                if value in known_metrics:
                    self.state.intent.metrics = [value]
                    return

        # Path 3: voice / free-text — re-parse through the intent extractor
        # (handles "spend", "leads", "CPA", "cost per acquisition", etc.)
        text = free_text or value
        if text:
            from reporting.intent_extractor import extract_intent
            result = extract_intent(text, self.catalog)
            if result.intent.metrics:
                # Filter to metrics that actually exist on the current table
                if self.state.intent.table:
                    table = self.catalog.get(self.state.intent.table)
                    if table:
                        known = {c.name for c in table.metrics()} | {dm.name for dm in table.derived_metrics}
                        valid = [m for m in result.intent.metrics if m in known]
                        if valid:
                            self.state.intent.metrics = valid
                            return
                else:
                    self.state.intent.metrics = result.intent.metrics
                    return
        # Path 4: couldn't resolve — leave metrics empty, question re-asks

    def _handle_dimensions(self, value: str, free_text: str) -> None:
        """User picked dimension(s) — chip OR voice phrase."""
        # Path 1: explicit "no breakdown" sentinel
        if value == "__none__":
            self.state.intent.dimensions = ["__none__"]
            return

        # Voice "no breakdown" phrases
        if free_text:
            ft = free_text.lower()
            if any(p in ft for p in ["no breakdown", "no break down", "totals only",
                                       "just totals", "just the totals", "no grouping",
                                       "don't break", "skip", "none"]):
                self.state.intent.dimensions = ["__none__"]
                return

        # Path 2: chip value matches a known dimension on the table
        if self.state.intent.table:
            table = self.catalog.get(self.state.intent.table)
            if table:
                known_dims = {c.name for c in table.dimensions()}
                if value in known_dims:
                    self.state.intent.dimensions = [value]
                    return
                # synthetic "__time__:column_name" dimension
                if value.startswith("__time__:"):
                    self.state.intent.dimensions = [value]
                    return

        # Path 3: voice — re-parse via intent extractor for "by campaign", "by source", etc.
        text = free_text or value
        if text:
            from reporting.intent_extractor import extract_intent
            result = extract_intent(text, self.catalog)
            if result.intent.dimensions:
                # Filter to dims that exist on this table
                if self.state.intent.table:
                    table = self.catalog.get(self.state.intent.table)
                    if table:
                        known = {c.name for c in table.dimensions()}
                        valid = [d for d in result.intent.dimensions if d in known]
                        if valid:
                            self.state.intent.dimensions = valid
                            return
                else:
                    self.state.intent.dimensions = result.intent.dimensions
                    return
        # Path 4: couldn't resolve — leave dimensions empty, question re-asks

    def _handle_filters(self, value: str, free_text: str) -> None:
        """User picked filters — chip OR voice phrase."""
        # Path 1: explicit "no filters" sentinel
        if value == "__none__":
            self.state.intent.filters = []
            return

        # Voice "no filters" phrases
        if free_text:
            ft = free_text.lower()
            if any(p in ft for p in ["no filter", "no filters", "skip", "none",
                                       "don't filter", "dont filter", "no thanks", "skip filters"]):
                self.state.intent.filters = []
                return

        # Path 2: chip value like "is_paid_lead:true"
        if value and ":" in value and not " " in value:
            col, val = value.split(":", 1)
            if val == "true":
                self.state.intent.filters.append(FilterClause(column=col, op="IS TRUE"))
                return
            elif val == "false":
                self.state.intent.filters.append(FilterClause(column=col, op="IS FALSE"))
                return

        # Path 3: voice — re-parse via intent extractor for "paid only", "non-brand", etc.
        text = free_text or value
        if text:
            from reporting.intent_extractor import extract_intent
            result = extract_intent(text, self.catalog)
            if result.intent.filters:
                self.state.intent.filters = result.intent.filters
                return
        # Path 4: couldn't resolve — leave filters empty (treated same as "no filters")
        self.state.intent.filters = []

    def _handle_confirmation(self, value: str) -> BuilderResponse:
        """User responded to the confirmation card. Accepts chip values OR voice."""
        v = value.lower().strip() if value else ""

        # Run phrases — voice equivalents of "Run it" chip
        run_phrases = {"__run__", "run it", "run", "yes", "go", "go ahead",
                        "do it", "proceed", "ok", "okay", "do it", "execute",
                        "yep", "yeah", "sure", "looks good", "looks right"}
        cancel_phrases = {"__cancel__", "cancel", "stop", "abort", "never mind",
                           "no", "nope", "forget it"}
        edit_phrases = {"__edit__", "edit", "change", "modify", "go back",
                         "let me change", "let me edit", "change something",
                         "actually wait", "wait"}

        if v in run_phrases or any(v.startswith(p + " ") or v.endswith(" " + p) for p in run_phrases if p):
            return self._run()
        if v in cancel_phrases or any(v.startswith(p + " ") or v.endswith(" " + p) for p in cancel_phrases if p):
            return self.cancel()
        if v in edit_phrases or any(v.startswith(p + " ") or v.endswith(" " + p) for p in edit_phrases if p):
            # Clear routing fields so we re-enter at AWAITING_SOURCE
            self.state.intent.table = None
            self.state.intent.service_line = None
            self.state.phase = BuilderPhase.AWAITING_SOURCE
            self.state.pending = None
            self.state.history = [
                h for h in self.state.history
                if h.get("phase") not in (BuilderPhase.AWAITING_FILTERS.value,
                                           BuilderPhase.AWAITING_DIMENSIONS.value)
            ]
            return self._advance()

        # Unknown — re-show confirmation
        return self._advance()

    # ─── The phase advancer (called after every answer) ────

    def _advance(self) -> BuilderResponse:
        """Look at current intent, decide next question or run."""
        # If the user said "edit" we want to reset enough to re-ask. But for normal flow,
        # next_question() handles "ask what's missing."
        phase, question = next_question(self.state.intent, self.catalog)

        # Special case: dimensions and filters can be "answered" with "no"
        # which sets a sentinel. Detect that and skip the question.
        if phase == BuilderPhase.AWAITING_DIMENSIONS and self.state.intent.dimensions == ["__none__"]:
            # Already answered; advance past
            phase, question = next_question(
                _intent_with_skip(self.state.intent, skip="dimensions"),
                self.catalog,
            )
        if phase == BuilderPhase.AWAITING_FILTERS and self._filters_already_answered():
            phase, question = next_question(
                _intent_with_skip(self.state.intent, skip="filters"),
                self.catalog,
            )

        self.state.phase = phase
        self.state.pending = question

        spoken = question.prompt if question else None
        done = phase in (BuilderPhase.IDLE, BuilderPhase.COMPLETE)
        return BuilderResponse(state=self.state, spoken=spoken, done=done)

    def _filters_already_answered(self) -> bool:
        """Has the user already explicitly said 'no filters' in this build?"""
        for entry in self.state.history:
            if entry.get("phase") == BuilderPhase.AWAITING_FILTERS.value:
                return True
        return False

    # ─── Execute the report ──────────────────────────────

    def _run(self) -> BuilderResponse:
        """Generate SQL, run if a query_runner is provided, return results."""
        self.state.phase = BuilderPhase.RUNNING
        self.state.pending = None

        # Resolve any sentinel dimensions before generating SQL
        actual = ReportIntent(
            table=self.state.intent.table,
            service_line=self.state.intent.service_line,
            metrics=self.state.intent.metrics,
            dimensions=[d for d in self.state.intent.dimensions if d != "__none__"],
            filters=self.state.intent.filters,
            time_window=self.state.intent.time_window,
        )

        # Validate one more time before running
        intent_dict = {
            "table": actual.table,
            "metrics": actual.metrics,
            "dimensions": actual.dimensions,
            "filters": {f.column: f.value for f in actual.filters},
            "time_window": {
                "column": self.catalog.get(actual.table).time_column if actual.table else None,
                "start": actual.time_window.start,
                "end": actual.time_window.end,
            },
        }
        vr = self.catalog.validate(intent_dict)
        if not vr.ok:
            self.state.error = "; ".join(vr.errors)
            self.state.phase = BuilderPhase.IDLE
            return BuilderResponse(
                state=self.state,
                spoken=f"I can't run that: {self.state.error}",
                done=True,
            )

        # Generate SQL
        try:
            sql, params = generate_sql(actual, self.catalog)
        except ValueError as e:
            self.state.error = str(e)
            self.state.phase = BuilderPhase.IDLE
            return BuilderResponse(
                state=self.state,
                spoken=f"I can't generate that query: {e}",
                done=True,
            )

        # If no runner, just return the SQL
        if not self.query_runner:
            self.state.last_result = {"sql": sql, "params": params, "rows": None}
            self.state.phase = BuilderPhase.COMPLETE
            # Update session defaults from this successful build
            self._update_session_defaults(actual)
            return BuilderResponse(
                state=self.state,
                spoken="Generated query (no runner — preview mode).",
                done=True,
                ui_update={"sql": sql, "params": params},
            )

        # Execute
        try:
            rows = self.query_runner(sql, params)
            self.state.last_result = {
                "sql": sql,
                "params": params,
                "rows": rows,
                "row_count": len(rows) if rows is not None else 0,
            }
            self.state.phase = BuilderPhase.COMPLETE
            self._update_session_defaults(actual)
            row_count = len(rows) if rows is not None else 0
            return BuilderResponse(
                state=self.state,
                spoken=f"Done. {row_count} row(s).",
                done=True,
                ui_update={"sql": sql, "rows": rows},
            )
        except Exception as e:
            self.state.error = str(e)
            self.state.phase = BuilderPhase.IDLE
            return BuilderResponse(
                state=self.state,
                spoken=f"Query failed: {e}",
                done=True,
            )

    def _update_session_defaults(self, intent: ReportIntent) -> None:
        """Remember choices to use as defaults for the next report this session."""
        self.state.session_defaults = {
            "service_line": intent.service_line,
            "time_window": intent.time_window.to_dict(),
            "filters": [f.to_dict() for f in intent.filters],
        }


# ─── helpers ──────────────────────────────────────────────────

def _intent_with_skip(intent: ReportIntent, skip: str) -> ReportIntent:
    """
    Make a copy of intent that lies about the skipped field being filled,
    so next_question() doesn't loop back to ask it again. Used when user
    explicitly chose "no" for dimensions or filters.
    """
    import copy
    fake = copy.deepcopy(intent)
    if skip == "dimensions" and not fake.dimensions:
        fake.dimensions = ["__none__"]
    if skip == "filters":
        # Filters already empty is the answered state; we need an actual sentinel
        # The simplest: pretend a no-op filter is present
        fake.filters = [FilterClause(column="__placeholder__", op="IS TRUE")]
    return fake
