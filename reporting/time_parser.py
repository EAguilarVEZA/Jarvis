"""
Time expression parser for the report builder.

Turns natural-language phrases into proper date ranges:
  "MTD"               → 2026-05-01 to today
  "last 30 days"      → today-30 to today
  "Q1"                → 2026-01-01 to 2026-03-31
  "last quarter"      → the previous quarter
  "April"             → 2026-04-01 to 2026-04-30
  "April 2025"        → 2025-04-01 to 2025-04-30
  "2025"              → 2025-01-01 to 2025-12-31

Returns None when nothing matches — caller will ask the user.

Test with:  python -m reporting.time_parser
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

from reporting.builder_types import TimeWindow


# ─── Preset definitions (used by the chip UI) ──────────────────

PRESETS = [
    ("mtd",          "Month to date",     "From the 1st of this month through today"),
    ("last_30d",     "Last 30 days",      "Rolling 30-day window"),
    ("last_7d",      "Last 7 days",       "Rolling 7-day window"),
    ("last_90d",     "Last 90 days",      "Rolling 90-day window"),
    ("ytd",          "Year to date",      "From January 1 through today"),
    ("last_month",   "Last month",        "Full previous calendar month"),
    ("last_quarter", "Last quarter",      "Full previous calendar quarter"),
    ("last_year",    "Last year",         "Full previous calendar year"),
    ("this_quarter", "This quarter",      "Current quarter to date"),
    ("custom",       "Custom range...",   "Pick your own start and end"),
]


MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _month_range(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) of the given month."""
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _quarter_range(year: int, quarter: int) -> tuple[date, date]:
    """Return (first_day, last_day) of the given Q1..Q4 in year."""
    start_month = 3 * (quarter - 1) + 1
    end_month = start_month + 2
    return date(year, start_month, 1), date(year, end_month, monthrange(year, end_month)[1])


# ─── Preset resolver (chip selection → dates) ─────────────────

def resolve_preset(preset: str, today: Optional[date] = None) -> Optional[TimeWindow]:
    """
    Resolve a preset key like 'mtd' to an actual TimeWindow with start/end dates.
    Returns None for unknown presets.
    """
    today = today or date.today()
    p = preset.lower()

    if p == "mtd":
        start = today.replace(day=1)
        return TimeWindow(preset=p, start=start.isoformat(), end=today.isoformat(),
                          label="Month to date")
    if p == "last_30d":
        start = today - timedelta(days=30)
        return TimeWindow(preset=p, start=start.isoformat(), end=today.isoformat(),
                          label="Last 30 days")
    if p == "last_7d":
        start = today - timedelta(days=7)
        return TimeWindow(preset=p, start=start.isoformat(), end=today.isoformat(),
                          label="Last 7 days")
    if p == "last_90d":
        start = today - timedelta(days=90)
        return TimeWindow(preset=p, start=start.isoformat(), end=today.isoformat(),
                          label="Last 90 days")
    if p == "ytd":
        start = date(today.year, 1, 1)
        return TimeWindow(preset=p, start=start.isoformat(), end=today.isoformat(),
                          label="Year to date")
    if p == "last_month":
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        s, e = _month_range(last_of_prev.year, last_of_prev.month)
        return TimeWindow(preset=p, start=s.isoformat(), end=e.isoformat(),
                          label=f"Last month ({s.strftime('%B %Y')})")
    if p == "last_quarter":
        q_this = (today.month - 1) // 3 + 1
        if q_this == 1:
            year, quarter = today.year - 1, 4
        else:
            year, quarter = today.year, q_this - 1
        s, e = _quarter_range(year, quarter)
        return TimeWindow(preset=p, start=s.isoformat(), end=e.isoformat(),
                          label=f"Q{quarter} {year}")
    if p == "this_quarter":
        q = (today.month - 1) // 3 + 1
        s, _ = _quarter_range(today.year, q)
        return TimeWindow(preset=p, start=s.isoformat(), end=today.isoformat(),
                          label=f"Q{q} {today.year} to date")
    if p == "last_year":
        year = today.year - 1
        return TimeWindow(preset=p, start=f"{year}-01-01", end=f"{year}-12-31",
                          label=f"{year}")
    if p == "custom":
        # Caller needs to populate start/end separately
        return TimeWindow(preset=p, label="Custom range")
    return None


# ─── Free-text parser (NL → TimeWindow) ─────────────────────────

# Recognized natural-language phrases
_NL_PATTERNS = [
    # (regex, preset_key) — case insensitive
    (r"\b(?:mtd|month[ -]to[ -]date)\b", "mtd"),
    (r"\bytd\b|\byear[ -]to[ -]date\b", "ytd"),
    (r"\blast 7 days?\b|\bpast 7 days?\b|\blast week\b", "last_7d"),
    (r"\blast 30 days?\b|\bpast 30 days?\b|\blast month period\b", "last_30d"),
    (r"\blast 90 days?\b|\bpast 90 days?\b|\blast three months?\b|\blast quarter period\b", "last_90d"),
    (r"\blast month\b|\bprev(?:ious)? month\b", "last_month"),
    (r"\blast quarter\b|\bprev(?:ious)? quarter\b|\bprior quarter\b", "last_quarter"),
    (r"\bthis quarter\b|\bcurrent quarter\b|\bquarter to date\b|\bqtd\b", "this_quarter"),
    (r"\blast year\b|\bprev(?:ious)? year\b|\bprior year\b", "last_year"),
]


def parse_time_expression(text: str, today: Optional[date] = None) -> Optional[TimeWindow]:
    """
    Try to interpret a natural-language time expression.
    Returns None if nothing matched.
    """
    if not text:
        return None
    today = today or date.today()
    t = text.lower().strip()

    # Preset phrases first
    for pat, preset in _NL_PATTERNS:
        if re.search(pat, t):
            return resolve_preset(preset, today)

    # "Q1", "Q2 2024", "Q1 of 2025"
    m = re.search(r"\bq([1-4])(?:\s+(?:of\s+)?(\d{4}))?\b", t)
    if m:
        q = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else today.year
        s, e = _quarter_range(year, q)
        return TimeWindow(start=s.isoformat(), end=e.isoformat(), label=f"Q{q} {year}")

    # "April 2025", "april 2025"
    m = re.search(r"\b(" + "|".join(MONTH_NAMES.keys()) + r")\s+(\d{4})\b", t)
    if m:
        month = MONTH_NAMES[m.group(1)]
        year = int(m.group(2))
        s, e = _month_range(year, month)
        return TimeWindow(start=s.isoformat(), end=e.isoformat(),
                          label=f"{s.strftime('%B %Y')}")

    # Standalone month name → current year unless month is in the future, then prior year
    m = re.search(r"\b(" + "|".join(MONTH_NAMES.keys()) + r")\b", t)
    if m:
        month = MONTH_NAMES[m.group(1)]
        year = today.year if month <= today.month else today.year - 1
        s, e = _month_range(year, month)
        return TimeWindow(start=s.isoformat(), end=e.isoformat(),
                          label=f"{s.strftime('%B %Y')}")

    # Date range "from 2025-01-01 to 2025-03-31" or "2025-01-01 to 2025-03-31"
    # Check this BEFORE the year pattern, otherwise "2025" matches first.
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:to|through|-|–|—)\s*(\d{4}-\d{2}-\d{2})", t)
    if m:
        return TimeWindow(start=m.group(1), end=m.group(2),
                          label=f"{m.group(1)} to {m.group(2)}")

    # "last N days" with arbitrary N
    m = re.search(r"\blast (\d+) days?\b", t)
    if m:
        n = int(m.group(1))
        start = today - timedelta(days=n)
        return TimeWindow(start=start.isoformat(), end=today.isoformat(),
                          label=f"Last {n} days")

    # Year alone: "2025"
    m = re.search(r"\b(20\d{2})\b", t)
    if m:
        year = int(m.group(1))
        return TimeWindow(start=f"{year}-01-01", end=f"{year}-12-31", label=str(year))

    return None


# ─── Quick smoke test when run as script ─────────────────────

if __name__ == "__main__":
    fake_today = date(2026, 5, 11)
    tests = [
        ("MTD", "mtd"),
        ("last 30 days", "last_30d"),
        ("last quarter", None),  # resolves but no specific preset string
        ("Q1", None),
        ("Q1 2024", None),
        ("April", None),
        ("April 2025", None),
        ("2025", None),
        ("last 14 days", None),
        ("from 2025-01-01 to 2025-03-31", None),
        ("show me appointment data for cardiology MTD", "mtd"),  # hidden in a sentence
        ("garbage input that doesn't match", None),
    ]
    print("Time parser tests (assuming today = 2026-05-11):")
    print()
    for input_text, _ in tests:
        tw = parse_time_expression(input_text, fake_today)
        if tw:
            print(f"  {input_text:50}  →  {tw.start} to {tw.end}  ({tw.label})")
        else:
            print(f"  {input_text:50}  →  (no match)")
