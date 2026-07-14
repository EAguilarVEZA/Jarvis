"""Jarvis reporting module — BigQuery-backed answers for voice and dashboards."""

from .bq_client import BQClient, QueryResult
from .catalog import SERVICE_LINES, ServiceLine, resolve, list_keys
from .queries import (
    Reports,
    Period,
    Summary,
    CampaignBreakdown,
    CampaignRow,
    TimeSeries,
    TimeSeriesPoint,
)
from .voice import (
    parse_intent,
    answer_question,
    format_voice_answer,
    ReportingIntent,
)
from .dashboard import build_dashboard

__all__ = [
    "BQClient",
    "QueryResult",
    "SERVICE_LINES",
    "ServiceLine",
    "resolve",
    "list_keys",
    "Reports",
    "Period",
    "Summary",
    "CampaignBreakdown",
    "CampaignRow",
    "TimeSeries",
    "TimeSeriesPoint",
    "parse_intent",
    "answer_question",
    "format_voice_answer",
    "ReportingIntent",
    "build_dashboard",
]
