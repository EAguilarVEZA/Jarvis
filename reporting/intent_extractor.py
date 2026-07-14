"""
Intent extractor — parse a user's report request into a ReportIntent.

Heuristic / regex-based fast path. Returns a ReportIntent + confidence score.
When confidence is low, the report builder will fall back to asking the user.

Examples (assumes catalog is loaded):

  "let's build a report on cardiology"
    → ReportIntent(service_line="cardiology", table=None, ...) — needs table
  
  "cardiology spend last quarter by campaign"
    → ReportIntent(service_line="cardiology", 
                   table="data_mart_cardiology.combined_campaign_stats",
                   metrics=["cost"], 
                   dimensions=["dashboard_campaign"],
                   time_window=<Q1 2026>)

  "MTD leads for cardiology, paid only"
    → ReportIntent(service_line="cardiology",
                   table="data_mart_cardiology.combined_campaign_stats",
                   metrics=["leads"],
                   filters=[FilterClause(column="is_paid_lead", op="IS TRUE")],
                   time_window=<MTD>)

This module is intentionally conservative: it only fills in fields where
it's reasonably sure. The builder always asks for confirmation before running,
so a "wrong guess" gets surfaced in the confirmation card, not silently run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from reporting.catalog import WarehouseCatalog, SERVICE_LINES, find_service_line
from reporting.builder_types import ReportIntent, FilterClause, TimeWindow
from reporting.time_parser import parse_time_expression


# ─── Trigger phrases ──────────────────────────────────────────

# ─── Trigger detection ─────────────────────────────────────────
#
# Trigger phrases must be *constructive* — explicit build/create/make/start verbs.
# This is deliberately narrower than the existing reporting_module.detect_report_trigger
# which catches consumption verbs (show, open, pull, launch). Zero overlap by design:
#   - "show me marketing report"   → existing one-shot path
#   - "let's build a marketing report" → THIS path (new builder)
#
# If a phrase matches BOTH (e.g. dispatch ambiguity), the build path wins because
# server.py checks it first.

TRIGGER_PATTERNS = [
    r"\blet'?s build (?:me |us )?(?:a |the )?(?:new )?report\b",
    r"\bbuild (?:me |us )?(?:a |the )?(?:new )?report\b",
    r"\bcreate (?:me |us )?(?:a |the )?(?:new )?report\b",
    r"\bmake (?:me |us )?(?:a |the )?(?:new )?report\b",
    r"\bstart (?:me |us )?(?:a |the )?(?:new )?report\b",
    r"\bi (?:want|need) to build (?:a )?report\b",
    r"\bi (?:want|need) to create (?:a )?report\b",
    r"\bnew report\b",                                  # short form
    r"\breport builder\b",                              # naming the tool itself
]

TRIGGER_REGEX = re.compile("|".join(TRIGGER_PATTERNS), re.IGNORECASE)


def is_report_trigger(text: str) -> bool:
    """Does this utterance start the report builder?

    Returns True for *constructive* phrasings only (build/create/make/start).
    Display/consumption phrasings ("show me the dashboard", "marketing report")
    fall through to the existing reporting_module.detect_report_trigger.
    """
    return bool(TRIGGER_REGEX.search(text))


# Alias used by some callers
is_build_trigger = is_report_trigger


# ─── Metric vocabulary (English → column name) ────────────────

METRIC_KEYWORDS = {
    # Cost / spend
    "spend":           "cost",
    "cost":            "cost",
    "budget":          "cost",
    "dollars":         "cost",
    "money":           "cost",
    # Leads
    "lead":            "leads",
    "leads":           "leads",
    "conversion":      "leads",
    "conversions":     "leads",
    # Clicks
    "click":           "clicks",
    "clicks":          "clicks",
    # Impressions
    "impression":      "impressions",
    "impressions":     "impressions",
    "views":           "impressions",
    # Derived
    "cpa":             "cpa",
    "cost per acquisition": "cpa",
    "cost per lead":   "cpa",
    "ctr":             "ctr",
    "click through":   "ctr",
    "clickthrough":    "ctr",
    "cpc":             "cpc",
    "cost per click":  "cpc",
    "conversion rate": "conversion_rate",
}


# ─── Dimension vocabulary (English → column name) ─────────────

DIMENSION_KEYWORDS = {
    "campaign":         "dashboard_campaign",
    "campaigns":        "dashboard_campaign",
    "by campaign":      "dashboard_campaign",
    "source":           "ad_source",
    "sources":          "ad_source",
    "channel":          "ad_source",
    "channels":         "ad_source",
    "platform":         "ad_source",
    "by source":        "ad_source",
    "medium":           "ad_medium",
    "by medium":        "ad_medium",
    "keyword":          "keyword_text",     # signals paid_keywords table
    "keywords":         "keyword_text",
    "ad":               "ad_name",
    "by ad":            "ad_name",
    "ads":              "ad_name",
    "ad group":         "ad_group_name",
    "ad groups":        "ad_group_name",
    "owner":            "budget_owner",     # signals efficiency_media_campaigns table
    "budget owner":     "budget_owner",
}


# ─── Filter vocabulary ────────────────────────────────────────

# Maps phrase → (column, op, value)
# IMPORTANT: order matters. More specific patterns come first.
# A naive `\bbrand only\b` matches inside `non-brand only` because the hyphen
# is a word boundary in Python regex. We check "non-brand" first AND use
# negative lookbehind on the positive pattern.
FILTER_PATTERNS = [
    # NON-brand FIRST (must be checked before "brand only")
    (r"\bnon[- ]?brand(?:ed)?\b|\bexclude brand(?:ed)?\b|\bno brand(?:ed)?\b",
        FilterClause("is_brand_lead", "IS FALSE")),

    # brand only — negative lookbehind ensures "non-brand only" doesn't match
    (r"(?<!non[- ])(?<!non)\bbrand(?:ed)? only\b|\bbrand searches?\b",
        FilterClause("is_brand_lead", "IS TRUE")),

    # paid / organic
    (r"\bpaid only\b|\bonly paid\b|\bjust paid\b",
        FilterClause("is_paid_lead", "IS TRUE")),
    (r"\borganic only\b|\bonly organic\b|\bjust organic\b",
        FilterClause("is_paid_lead", "IS FALSE")),

    # digital
    (r"\bdigital only\b",
        FilterClause("is_digital", "IS TRUE")),
]


# Filter patterns whose columns live ONLY on the leads table.
# When one of these matches, the table router should prefer .leads over .combined_campaign_stats.
LEADS_TABLE_FILTERS = {"is_paid_lead", "is_brand_lead"}


# ─── Table choice heuristic ────────────────────────────────────

def choose_table(catalog: WarehouseCatalog, service_line: Optional[str],
                  dimensions: list[str], metrics: list[str],
                  filters: Optional[list[FilterClause]] = None) -> Optional[str]:
    """
    Given known signals, pick the best table.

    Logic:
      - If a lead-only filter is requested → .leads (only table with is_brand_lead / is_paid_lead)
      - If "keyword" is mentioned → paid_keywords_w_leads
      - If "owner" / "budget owner" → efficiency_media_campaigns
      - If "ad name" or fine ad-level → digital_ads_w_leads (or digital_ads if no leads metric)
      - Default → combined_campaign_stats (the workhorse table)
    """
    if not service_line:
        return None

    # Find the dataset for this service line
    sl = next((s for s in SERVICE_LINES if s.key == service_line), None)
    if not sl:
        return None
    dataset = sl.dataset

    # Lead-event-level required? (is_paid_lead, is_brand_lead don't exist in combined_campaign_stats)
    if filters:
        for f in filters:
            if f.column in LEADS_TABLE_FILTERS:
                return f"{dataset}.leads"

    # Keyword-level granularity?
    if "keyword_text" in dimensions:
        return f"{dataset}.paid_keywords_w_leads"

    # Budget owner?
    if "budget_owner" in dimensions:
        return f"{dataset}.efficiency_media_campaigns"

    # Ad-level (ad_name, ad_group_name)?
    if "ad_name" in dimensions or "ad_group_name" in dimensions:
        if "leads" in metrics:
            return f"{dataset}.digital_ads_w_leads"
        return f"{dataset}.digital_ads"

    # Default: campaign-level performance
    return f"{dataset}.combined_campaign_stats"


# ─── Main entry point ─────────────────────────────────────────

@dataclass
class IntentExtractionResult:
    intent: ReportIntent
    confidence: float                      # 0.0 to 1.0
    notes: list[str]                       # human-readable hints/warnings about parsing


def extract_intent(text: str, catalog: WarehouseCatalog,
                   defaults: Optional[dict] = None) -> IntentExtractionResult:
    """
    Parse a free-text request into a ReportIntent.
    
    `defaults` carries session-level defaults from prior report builds in
    the same conversation (e.g. last service line, last filter set).
    """
    defaults = defaults or {}
    intent = ReportIntent()
    notes: list[str] = []
    signals = 0                            # how many fields we filled in

    lowered = text.lower()

    # 1) Service line
    sl = find_service_line(lowered)
    if sl:
        intent.service_line = sl.key
        signals += 1
        notes.append(f"Service line: {sl.label} (matched: {sl.aliases})")
    elif "service_line" in defaults:
        intent.service_line = defaults["service_line"]
        notes.append(f"Service line: {intent.service_line} (from session default)")

    # 2) Time window
    tw = parse_time_expression(text)
    if tw:
        intent.time_window = tw
        signals += 1
        notes.append(f"Time: {tw.label}")
    elif defaults.get("time_window"):
        intent.time_window = TimeWindow(**defaults["time_window"])
        notes.append(f"Time: {intent.time_window.label} (from session default)")

    # 3) Metrics — match longest keywords first
    metrics_found: list[str] = []
    for phrase in sorted(METRIC_KEYWORDS.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(phrase) + r"\b", lowered):
            col = METRIC_KEYWORDS[phrase]
            if col not in metrics_found:
                metrics_found.append(col)
    if metrics_found:
        intent.metrics = metrics_found
        signals += 1
        notes.append(f"Metrics: {metrics_found}")

    # 4) Dimensions — match longest keywords first
    dims_found: list[str] = []
    for phrase in sorted(DIMENSION_KEYWORDS.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(phrase) + r"\b", lowered):
            col = DIMENSION_KEYWORDS[phrase]
            if col not in dims_found:
                dims_found.append(col)
    if dims_found:
        intent.dimensions = dims_found
        signals += 1
        notes.append(f"Dimensions: {dims_found}")

    # 5) Filters — deduplicate by column (first match wins)
    filters_found: list[FilterClause] = []
    filter_columns_seen: set[str] = set()
    for pat, fc in FILTER_PATTERNS:
        if re.search(pat, lowered) and fc.column not in filter_columns_seen:
            filters_found.append(fc)
            filter_columns_seen.add(fc.column)
    if filters_found:
        intent.filters = filters_found
        signals += 1
        notes.append(f"Filters: {[(f.column, f.op) for f in filters_found]}")
    elif defaults.get("filters"):
        intent.filters = [FilterClause(**f) for f in defaults["filters"]]
        notes.append("Filters: (carried from session default)")

    # 6) Choose table based on signals (filters influence routing — lead-level filters need .leads table)
    if intent.service_line:
        chosen = choose_table(catalog, intent.service_line, intent.dimensions,
                              intent.metrics, intent.filters)
        if chosen:
            intent.table = chosen
            signals += 1
            notes.append(f"Table: {chosen}")

    # 7) Confidence — proportional to how many fields we filled
    # 6 max signals; we want >=0.6 to skip clarification on a field
    confidence = min(signals / 6.0, 1.0)

    return IntentExtractionResult(intent=intent, confidence=confidence, notes=notes)
