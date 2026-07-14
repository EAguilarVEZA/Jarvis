"""
Question generator — given a partial ReportIntent, produce the next question.

Walks the intent fields in priority order:
  1. Source / service line / table     (without this, nothing else can be asked)
  2. Time window                       (always required)
  3. Metrics                           (at least one)
  4. Dimensions                        (optional but usually wanted)
  5. Filters                           (optional)
  6. Confirmation card                 (always shown before running)

At each step, only asks if the field is missing.
"""

from __future__ import annotations

from typing import Optional

from reporting.catalog import WarehouseCatalog, SERVICE_LINES
from reporting.builder_types import (
    BuilderPhase, Option, Question, QuestionKind, ReportIntent,
)
from reporting.time_parser import PRESETS


# ─── Top-level dispatcher ─────────────────────────────────────

def next_question(intent: ReportIntent, catalog: WarehouseCatalog) -> tuple[BuilderPhase, Optional[Question]]:
    """
    Look at the intent's state and pick the next question.
    
    Returns:
      (phase, question) where phase is the new BuilderPhase.
      If the intent is complete, returns (CONFIRMING, confirmation_question).
    """
    # 1. Service line / source first
    if not intent.service_line and not intent.table:
        return BuilderPhase.AWAITING_SOURCE, _ask_source()

    # 2. If service line is set but we couldn't auto-pick a table, ask which table
    if intent.service_line and not intent.table:
        return BuilderPhase.AWAITING_SOURCE, _ask_table(intent.service_line, catalog)

    # 3. Time
    if not intent.time_window.is_specified():
        return BuilderPhase.AWAITING_TIME, _ask_time()

    # 4. Metrics
    if not intent.metrics:
        return BuilderPhase.AWAITING_METRICS, _ask_metrics(intent.table, catalog)

    # 5. Dimensions (optional — but ask once)
    # We only ask once per build to avoid pestering. The "asked_dimensions"
    # flag lives in the intent notes; simplest approach is: ask if no dimensions
    # set AND we're not already past this phase. Builder.process_answer tracks phase.
    if not intent.dimensions:
        return BuilderPhase.AWAITING_DIMENSIONS, _ask_dimensions(intent.table, catalog)

    # 6. Filters — optional, ask once with "no filters" as a default
    # Same pattern: ask only if no filters AND not already past this phase.
    # (Driven by Builder.process_answer to avoid loops.)
    if not intent.filters:
        return BuilderPhase.AWAITING_FILTERS, _ask_filters(intent.table, catalog)

    # 7. Everything's filled — show confirmation
    return BuilderPhase.CONFIRMING, _ask_confirm(intent, catalog)


# ─── Question builders ────────────────────────────────────────

def _ask_source() -> Question:
    """Ask which service line / data source to query."""
    options = []
    # Service-line marts (the most common case)
    for sl in SERVICE_LINES:
        if sl.key == "all":
            continue  # we'll add this separately as a distinct option
        options.append(Option(label=sl.label, value=f"service_line:{sl.key}"))
    # Then the cross-cutting sources
    options.insert(0, Option(label="All Service Lines (enterprise)",
                              value="service_line:all",
                              description="Cross-channel rollup",
                              is_recommended=True))
    options.append(Option(label="GA4 web analytics",
                          value="source:ga4",
                          description="Sessions, events, page views"))
    options.append(Option(label="Search Console",
                          value="source:gsc",
                          description="Organic search — cross-domain"))
    options.append(Option(label="Cross-platform ads",
                          value="source:cross_platform_ads",
                          description="Granular ad-level data across platforms"))

    return Question(
        kind=QuestionKind.SOURCE_CHOICE,
        prompt="Which service line or area would you like to report on?",
        options=options,
        target_field="service_line",
    )


def _ask_table(service_line: str, catalog: WarehouseCatalog) -> Question:
    """Service line known, but multiple tables — let the user pick a granularity."""
    tables = [t for t in catalog.all_tables() if t.service_line_key == service_line]
    options = []
    # Order by row count ascending — smaller tables = higher level summaries first
    tables.sort(key=lambda t: t.row_count)
    for t in tables:
        opts_label = t.label.split(" — ", 1)[-1]  # drop "Cardiology — " prefix
        options.append(Option(
            label=opts_label,
            value=t.full_name,
            description=f"{t.row_count:,} rows",
            is_recommended=t.name == "combined_campaign_stats",
        ))
    return Question(
        kind=QuestionKind.SOURCE_CHOICE,
        prompt="Which table?",
        options=options,
        target_field="table",
    )


def _ask_time() -> Question:
    """Ask the date range."""
    options = [Option(label=label, value=key, description=desc, is_recommended=(key == "last_30d"))
               for key, label, desc in PRESETS]
    return Question(
        kind=QuestionKind.TIME_CHOICE,
        prompt="What time range?",
        options=options,
        target_field="time_window",
    )


def _ask_metrics(table_name: str, catalog: WarehouseCatalog) -> Question:
    """Ask which metrics from the chosen table."""
    table = catalog.get(table_name)
    if not table:
        # Shouldn't happen, but be defensive
        return Question(
            kind=QuestionKind.METRIC_CHOICE,
            prompt="What metric(s) do you want?",
            options=[],
            target_field="metrics",
        )
    options = []
    seen = set()
    # Default metrics first, recommended
    for m_name in table.default_metrics:
        col = next((c for c in table.metrics() if c.name == m_name), None)
        derived = next((dm for dm in table.derived_metrics if dm.name == m_name), None)
        if col and col.name not in seen:
            options.append(Option(label=col.label or col.name, value=col.name,
                                  description=col.unit, is_recommended=True))
            seen.add(col.name)
        elif derived and derived.name not in seen:
            options.append(Option(label=derived.label or derived.name, value=derived.name,
                                  description=derived.unit, is_recommended=True))
            seen.add(derived.name)
    # Then the rest
    for col in table.metrics():
        if col.name not in seen:
            options.append(Option(label=col.label or col.name, value=col.name,
                                  description=col.unit))
            seen.add(col.name)
    for dm in table.derived_metrics:
        if dm.name not in seen:
            options.append(Option(label=dm.label or dm.name, value=dm.name,
                                  description=dm.unit))
            seen.add(dm.name)
    # Always offer "all of them"
    options.append(Option(label="All key metrics", value="__all_defaults__",
                          description="The recommended set above"))

    return Question(
        kind=QuestionKind.METRIC_CHOICE,
        prompt="Which metric(s) do you want?",
        options=options,
        target_field="metrics",
    )


def _ask_dimensions(table_name: str, catalog: WarehouseCatalog) -> Question:
    """Ask how to break down."""
    table = catalog.get(table_name)
    if not table:
        return Question(
            kind=QuestionKind.DIMENSION_CHOICE,
            prompt="Break down by what?",
            options=[Option(label="No breakdown (totals only)", value="__none__", is_default=True)],
            target_field="dimensions",
        )
    options = [Option(label="No breakdown (totals only)", value="__none__",
                      description="Just the top-line numbers")]
    for col in table.dimensions():
        # Skip noise / internal columns
        if not col.role or col.role == "noise":
            continue
        options.append(Option(label=col.label or col.name, value=col.name,
                              description=col.note))
    # If time column exists, offer "daily trend" as a synthetic option
    tc = table.time_col()
    if tc:
        options.append(Option(label="Daily trend (by day)", value=f"__time__:{tc.name}",
                              description="Time series chart"))
    return Question(
        kind=QuestionKind.DIMENSION_CHOICE,
        prompt="Break down by what?",
        options=options,
        target_field="dimensions",
    )


def _ask_filters(table_name: str, catalog: WarehouseCatalog) -> Question:
    """Ask whether to apply any filters."""
    table = catalog.get(table_name)
    if not table:
        return Question(
            kind=QuestionKind.FILTER_CHOICE,
            prompt="Any filters?",
            options=[Option(label="No filters", value="__none__", is_default=True)],
            target_field="filters",
        )
    options = [Option(label="No filters", value="__none__", is_default=True)]
    for col in table.filters():
        # For booleans, offer both TRUE and FALSE options
        if col.name == "is_paid_lead":
            options.append(Option(label="Paid only", value=f"{col.name}:true"))
            options.append(Option(label="Organic only", value=f"{col.name}:false"))
        elif col.name == "is_brand_lead":
            options.append(Option(label="Brand only", value=f"{col.name}:true"))
            options.append(Option(label="Non-brand only", value=f"{col.name}:false"))
        elif col.name == "is_digital":
            options.append(Option(label="Digital only", value=f"{col.name}:true"))
        elif col.name == "is_anonymized_query":
            options.append(Option(label="Exclude anonymized queries", value=f"{col.name}:false"))
        else:
            # Generic boolean — both
            options.append(Option(label=f"{col.label or col.name} = TRUE", value=f"{col.name}:true"))
            options.append(Option(label=f"{col.label or col.name} = FALSE", value=f"{col.name}:false"))

    return Question(
        kind=QuestionKind.FILTER_CHOICE,
        prompt="Any filters?",
        options=options,
        target_field="filters",
    )


def _ask_confirm(intent: ReportIntent, catalog: WarehouseCatalog) -> Question:
    """Final confirmation. Always shown before running."""
    table = catalog.get(intent.table) if intent.table else None
    summary_lines = []
    if table:
        summary_lines.append(f"Source: {table.label}")
    summary_lines.append(f"Time: {intent.time_window.label or 'unspecified'}")
    summary_lines.append(f"Metrics: {', '.join(intent.metrics) or 'none'}")
    if intent.dimensions:
        summary_lines.append(f"Breakdown: {', '.join(intent.dimensions)}")
    else:
        summary_lines.append("Breakdown: (totals only)")
    if intent.filters:
        summary_lines.append("Filters: " + "; ".join(f"{f.column} {f.op}" + (f" {f.value}" if f.value is not None else "")
                                                      for f in intent.filters))
    if table:
        summary_lines.append(f"Estimated source size: {table.row_count:,} rows")

    options = [
        Option(label="✓ Run it", value="__run__", is_recommended=True),
        Option(label="← Edit", value="__edit__",
               description="Change the source, time, metrics, dimensions, or filters"),
        Option(label="✗ Cancel", value="__cancel__"),
    ]
    return Question(
        kind=QuestionKind.CONFIRM,
        prompt="Here's what I'm about to run:\n\n" + "\n".join(summary_lines),
        options=options,
        target_field=None,
    )
