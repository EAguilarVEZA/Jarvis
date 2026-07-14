"""
Voice answer formatting and intent routing.

When the user says "what's our CPA for cardiology this month?", we need to:
  1. detect intent (cpa)
  2. extract the service line (cardiology) and time window (month-to-date)
  3. run the right query
  4. format the result as a natural spoken sentence

The intent matcher below is deliberately rule-based (keywords + simple regex)
rather than another LLM call. It's fast, deterministic, and easy to debug
when Jarvis gets something wrong. If a phrase doesn't match any rule, we
return None and the caller can fall back to passing the question to Claude.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import resolve, ServiceLine
from .queries import Period, Reports, Summary


# -------- intent detection --------

@dataclass
class ReportingIntent:
    metric: str                  # "cpa" | "leads" | "spend" | "summary" | "ctr"
    service_line: ServiceLine
    period: Period
    raw_question: str


METRIC_PATTERNS = [
    # order matters — most specific first
    ("cpa",     re.compile(r"\b(cpa|cost per (?:lead|acquisition|conversion))\b", re.I)),
    ("ctr",     re.compile(r"\b(ctr|click[\- ]?through(?: rate)?)\b", re.I)),
    ("cpc",     re.compile(r"\b(cpc|cost per click)\b", re.I)),
    ("spend",   re.compile(r"\b(spend|spent|cost|budget|costs)\b", re.I)),
    ("leads",   re.compile(r"\b(leads?|conversions?|appointments?|sign[\- ]?ups?)\b", re.I)),
    ("clicks",  re.compile(r"\bclicks?\b", re.I)),
    ("summary", re.compile(r"\b(summary|overview|how(?:'s| is) .* (?:doing|performing)|recap)\b", re.I)),
]

PERIOD_PATTERNS = [
    (re.compile(r"\b(today)\b", re.I),                   lambda: Period(date.today(), date.today())),
    (re.compile(r"\byesterday\b", re.I),                 lambda: Period(date.today() - timedelta(days=1),
                                                                         date.today() - timedelta(days=1))),
    (re.compile(r"\b(?:this|the current) week\b", re.I), lambda: _this_week()),
    (re.compile(r"\b(last|past|previous) 7 days?\b", re.I), lambda: Period.last_n_days(7)),
    (re.compile(r"\b(?:this|the current) month\b|\bmonth[ \-]to[ \-]date\b|\bmtd\b", re.I),
                                                          lambda: Period.month_to_date()),
    (re.compile(r"\b(last|previous) month\b", re.I),     lambda: Period.previous_month()),
    (re.compile(r"\b(?:last|past|previous) 30 days?\b", re.I), lambda: Period.last_n_days(30)),
    (re.compile(r"\b(?:last|past|previous) 90 days?\b|\bquarter\b", re.I),
                                                          lambda: Period.last_n_days(90)),
    (re.compile(r"\b(?:last|past) (\d+) days?\b", re.I), None),  # handled inline
]


def _this_week() -> Period:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return Period(start=monday, end=today)


def parse_intent(question: str) -> ReportingIntent | None:
    """Extract metric + service line + period from a user utterance.

    Returns None if we can't confidently identify a service line — falling back
    to the LLM is better than guessing wrong on which dataset to query.
    """
    if not question or not question.strip():
        return None

    # service line — required
    sl = _find_service_line(question)
    if not sl:
        return None

    # metric — default to summary if none mentioned
    metric = "summary"
    for name, pattern in METRIC_PATTERNS:
        if pattern.search(question):
            metric = name
            break

    # period — default to last 30 days
    period = _parse_period(question) or Period.last_n_days(30)

    return ReportingIntent(
        metric=metric,
        service_line=sl,
        period=period,
        raw_question=question.strip(),
    )


def _find_service_line(question: str) -> ServiceLine | None:
    """Walk through the question and try resolve() on each phrase.

    We try increasingly broad chunks because aliases like "primary care"
    are two words, while "cardiology" is one.
    """
    q = question.lower()
    # whole-question match first (catches "primary care", "weight loss")
    sl = resolve(q)
    if sl:
        return sl
    # then word by word
    for word in re.findall(r"[a-zA-Z']+", q):
        sl = resolve(word)
        if sl:
            return sl
    return None


def _parse_period(question: str) -> Period | None:
    for pattern, builder in PERIOD_PATTERNS:
        m = pattern.search(question)
        if not m:
            continue
        if builder is None:
            # "last N days" with captured number
            n = int(m.group(1))
            return Period.last_n_days(max(1, min(n, 365)))
        return builder()
    return None


# -------- answer formatting --------

def _money(n: float | None) -> str:
    if n is None:
        return "no data"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f} million"
    if n >= 10_000:
        return f"${n/1000:.1f} thousand"
    return f"${n:,.0f}"


def _count(n: int | None) -> str:
    if n is None:
        return "no data"
    return f"{n:,}"


def _period_phrase(p: Period) -> str:
    days = p.days
    if days == 1:
        return "today" if p.end == date.today() else f"on {p.start.strftime('%B %d')}"
    if days == 7:
        return "in the past 7 days"
    if days == 30:
        return "in the past 30 days"
    if days == 90:
        return "this quarter"
    today = date.today()
    if p.start.day == 1 and p.end == today and p.start.month == today.month:
        return "month to date"
    return f"from {p.start.strftime('%b %d')} to {p.end.strftime('%b %d')}"


def format_voice_answer(intent: ReportingIntent, summary: Summary) -> str:
    """Single sentence Jarvis can speak. Always lead with the number."""
    sl = intent.service_line.label
    when = _period_phrase(intent.period)
    metric = intent.metric

    if metric == "leads":
        return (
            f"{_count(summary.leads)} leads for {sl} {when}, "
            f"of which {_count(summary.paid_leads)} were paid."
        )

    if metric == "spend":
        return f"You spent {_money(summary.cost)} on {sl} {when}."

    if metric == "cpa":
        if summary.cpa is None:
            return f"I can't compute CPA for {sl} {when} — no paid leads recorded."
        return (
            f"{sl} CPA is {_money(summary.cpa)} {when}, "
            f"on {_money(summary.cost)} spend and {_count(summary.paid_leads)} paid leads."
        )

    if metric == "ctr":
        if summary.ctr is None:
            return f"No impressions recorded for {sl} {when}, so CTR is unavailable."
        return f"{sl} CTR is {summary.ctr}% {when}."

    if metric == "cpc":
        if summary.cpc is None:
            return f"No clicks recorded for {sl} {when}, so CPC is unavailable."
        return f"{sl} CPC is {_money(summary.cpc)} {when}."

    if metric == "clicks":
        return f"{_count(summary.clicks)} clicks for {sl} {when}."

    # summary — the big-picture answer
    parts = [
        f"For {sl} {when}:",
        f"{_count(summary.leads)} total leads",
        f"({_count(summary.paid_leads)} paid)",
        f"on {_money(summary.cost)} spend",
    ]
    if summary.cpa is not None:
        parts.append(f"at a CPA of {_money(summary.cpa)}")
    return ", ".join(parts) + "."


# -------- one-call entry point --------

def answer_question(reports: Reports, question: str) -> str | None:
    """Full pipeline: parse → query → format. Returns None if we can't handle it."""
    intent = parse_intent(question)
    if not intent:
        return None
    summary = reports.summary(intent.service_line, intent.period)
    return format_voice_answer(intent, summary)
