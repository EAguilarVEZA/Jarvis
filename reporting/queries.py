"""
Reporting queries against oh-data-warehouse data marts.

Every function here:
- takes a ServiceLine (or service_line key) plus a date range
- returns a dataclass with structured numbers, NOT a string
- never builds dataset names from user input (uses catalog.resolve)
- uses parameterized SQL (no f-string injection of user data)

The dashboard layer formats these results as HTML; the voice layer
formats them as spoken sentences. Same data either way.

Schema reference (verified against bq_inventory.txt, Apr 30 2026):

    {data_mart_X}.leads
        date DATE, leads INTEGER, lead_source STRING, lead_medium STRING,
        is_paid_lead BOOL, is_brand_lead BOOL, dashboard_campaign STRING,
        subject STRING, lead_type STRING

    {data_mart_X}.combined_campaign_stats
        date DATE, ad_source STRING, ad_medium STRING, is_digital BOOL,
        dashboard_campaign STRING, campaign_name STRING,
        clicks INTEGER, impressions INTEGER, cost FLOAT, leads INTEGER

    {data_mart_X}.digital_ads_w_leads
        date DATE, ad_source, ad_medium, campaign_name, ad_content,
        clicks, impressions, cost (NUMERIC), engagements, video_views, leads

    {data_mart_X}.paid_keywords_w_leads
        date, ad_source, ad_medium, campaign_name, ad_group_name,
        keyword_match_type, keyword_text, clicks, impressions,
        cost (NUMERIC), leads

    {data_mart_X}.efficiency_media_campaigns
        date, budget_owner, dashboard_campaign, campaign_name,
        ad_source_medium, name_of_patient_testimonials,
        impressions, clicks, cost (NUMERIC)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any

from .bq_client import BQClient
from .catalog import ServiceLine, resolve, BY_KEY


# -------- result types --------

@dataclass
class Period:
    start: date
    end: date

    @classmethod
    def last_n_days(cls, n: int) -> "Period":
        end = date.today()
        return cls(start=end - timedelta(days=n - 1), end=end)

    @classmethod
    def month_to_date(cls) -> "Period":
        today = date.today()
        return cls(start=today.replace(day=1), end=today)

    @classmethod
    def previous_month(cls) -> "Period":
        today = date.today()
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return cls(start=start, end=end)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class Summary:
    """Top-line numbers for a service line over a period."""
    service_line: str
    label: str
    period: Period
    leads: int = 0
    paid_leads: int = 0
    organic_leads: int = 0
    cost: float = 0.0
    clicks: int = 0
    impressions: int = 0

    @property
    def cpa(self) -> float | None:
        """Cost per acquisition (paid leads only)."""
        return round(self.cost / self.paid_leads, 2) if self.paid_leads else None

    @property
    def ctr(self) -> float | None:
        """Click-through rate as a percentage."""
        return round(100 * self.clicks / self.impressions, 2) if self.impressions else None

    @property
    def cpc(self) -> float | None:
        """Cost per click."""
        return round(self.cost / self.clicks, 2) if self.clicks else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["period"] = {"start": str(self.period.start), "end": str(self.period.end)}
        d["cpa"] = self.cpa
        d["ctr"] = self.ctr
        d["cpc"] = self.cpc
        return d


@dataclass
class CampaignRow:
    campaign_name: str
    dashboard_campaign: str | None
    ad_source: str | None
    cost: float
    clicks: int
    impressions: int
    leads: int

    @property
    def cpa(self) -> float | None:
        return round(self.cost / self.leads, 2) if self.leads else None


@dataclass
class CampaignBreakdown:
    service_line: str
    period: Period
    campaigns: list[CampaignRow] = field(default_factory=list)


@dataclass
class TimeSeriesPoint:
    date: date
    leads: int
    cost: float


@dataclass
class TimeSeries:
    service_line: str
    period: Period
    points: list[TimeSeriesPoint] = field(default_factory=list)


# -------- queries --------

class Reports:
    """All reporting queries hang off this object so they share a BQ client."""

    def __init__(self, bq: BQClient):
        self.bq = bq

    # ---- summary ----
    def summary(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
    ) -> Summary:
        sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
        if not sl:
            raise ValueError(f"unknown service line: {service_line}")
        period = period or Period.last_n_days(30)

        # data_mart_all.leads is one row per lead (event log) — use COUNT(*).
        # Per-service-line leads tables are pre-aggregated with a `leads` SUM column.
        # is_brand_lead only exists in per-service-line tables, so skip it for "all".
        if sl.dataset == "data_mart_all":
            leads_sql = f"""
                SELECT
                  COUNT(*) AS leads,
                  SUM(IF(is_paid_lead, 1, 0)) AS paid_leads,
                  SUM(IF(NOT is_paid_lead, 1, 0)) AS organic_leads
                FROM `oh-data-warehouse.{sl.dataset}.leads`
                WHERE date BETWEEN @start_date AND @end_date
            """
        else:
            leads_sql = f"""
                SELECT
                  SUM(leads) AS leads,
                  SUM(IF(is_paid_lead, leads, 0)) AS paid_leads,
                  SUM(IF(NOT is_paid_lead, leads, 0)) AS organic_leads
                FROM `oh-data-warehouse.{sl.dataset}.leads`
                WHERE date BETWEEN @start_date AND @end_date
            """

        # combined_campaign_stats schema is consistent across all data marts
        # (verified against bq_inventory.txt — all have leads/cost/clicks/impressions/is_digital).
        spend_sql = f"""
            SELECT
              SUM(cost) AS cost,
              SUM(clicks) AS clicks,
              SUM(impressions) AS impressions
            FROM `oh-data-warehouse.{sl.dataset}.combined_campaign_stats`
            WHERE date BETWEEN @start_date AND @end_date
              AND is_digital
        """
        params = {"start_date": period.start, "end_date": period.end}

        leads_row = self.bq.query(leads_sql, params).first() or {}
        spend_row = self.bq.query(spend_sql, params).first() or {}

        return Summary(
            service_line=sl.key,
            label=sl.label,
            period=period,
            leads=int(leads_row.get("leads") or 0),
            paid_leads=int(leads_row.get("paid_leads") or 0),
            organic_leads=int(leads_row.get("organic_leads") or 0),
            cost=float(spend_row.get("cost") or 0.0),
            clicks=int(spend_row.get("clicks") or 0),
            impressions=int(spend_row.get("impressions") or 0),
        )

    # ---- compare to prior period ----
    def summary_with_comparison(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
    ) -> dict:
        """Returns this period's summary + same-length prior period for delta math."""
        period = period or Period.last_n_days(30)
        prior_end = period.start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=period.days - 1)
        prior = Period(start=prior_start, end=prior_end)

        current = self.summary(service_line, period)
        previous = self.summary(service_line, prior)

        def delta(a: float, b: float) -> float | None:
            if not b:
                return None
            return round(100 * (a - b) / b, 1)

        return {
            "current": current.to_dict(),
            "previous": previous.to_dict(),
            "deltas": {
                "leads_pct": delta(current.leads, previous.leads),
                "cost_pct": delta(current.cost, previous.cost),
                "cpa_pct": delta(current.cpa or 0, previous.cpa or 0)
                if current.cpa and previous.cpa else None,
            },
        }

    # ---- campaign breakdown ----
    def top_campaigns(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
        limit: int = 20,
        order_by: str = "cost",
    ) -> CampaignBreakdown:
        sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
        if not sl:
            raise ValueError(f"unknown service line: {service_line}")
        if order_by not in {"cost", "leads", "clicks"}:
            raise ValueError(f"order_by must be cost|leads|clicks, got {order_by}")
        period = period or Period.last_n_days(30)

        # dashboard_campaign exists in per-service-line marts but NOT in data_mart_all.
        # Use NULL placeholder for the rollup case.
        dashboard_col = (
            "ANY_VALUE(dashboard_campaign) AS dashboard_campaign"
            if sl.dataset != "data_mart_all"
            else "CAST(NULL AS STRING) AS dashboard_campaign"
        )

        sql = f"""
            SELECT
              campaign_name,
              {dashboard_col},
              ANY_VALUE(ad_source) AS ad_source,
              SUM(cost) AS cost,
              SUM(clicks) AS clicks,
              SUM(impressions) AS impressions,
              SUM(leads) AS leads
            FROM `oh-data-warehouse.{sl.dataset}.combined_campaign_stats`
            WHERE date BETWEEN @start_date AND @end_date
              AND is_digital
              AND campaign_name IS NOT NULL
            GROUP BY campaign_name
            ORDER BY {order_by} DESC
            LIMIT @row_limit
        """
        rows = self.bq.query(
            sql,
            {"start_date": period.start, "end_date": period.end, "row_limit": limit},
        ).rows

        return CampaignBreakdown(
            service_line=sl.key,
            period=period,
            campaigns=[
                CampaignRow(
                    campaign_name=r["campaign_name"],
                    dashboard_campaign=r.get("dashboard_campaign"),
                    ad_source=r.get("ad_source"),
                    cost=float(r.get("cost") or 0),
                    clicks=int(r.get("clicks") or 0),
                    impressions=int(r.get("impressions") or 0),
                    leads=int(r.get("leads") or 0),
                )
                for r in rows
            ],
        )

    # ---- time series for charts ----
    def daily_timeseries(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
    ) -> TimeSeries:
        sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
        if not sl:
            raise ValueError(f"unknown service line: {service_line}")
        period = period or Period.last_n_days(90)

        # data_mart_all.leads is event-log style (row per lead) — use COUNT(*)
        leads_agg = (
            "COUNT(*) AS leads" if sl.dataset == "data_mart_all" else "SUM(leads) AS leads"
        )

        # Outer join leads and spend on date so we don't drop days where one side is zero.
        sql = f"""
            WITH leads AS (
              SELECT date, {leads_agg}
              FROM `oh-data-warehouse.{sl.dataset}.leads`
              WHERE date BETWEEN @start_date AND @end_date
              GROUP BY date
            ),
            spend AS (
              SELECT date, SUM(cost) AS cost
              FROM `oh-data-warehouse.{sl.dataset}.combined_campaign_stats`
              WHERE date BETWEEN @start_date AND @end_date AND is_digital
              GROUP BY date
            )
            SELECT
              COALESCE(l.date, s.date) AS date,
              COALESCE(l.leads, 0) AS leads,
              COALESCE(s.cost, 0) AS cost
            FROM leads l
            FULL OUTER JOIN spend s USING (date)
            ORDER BY date
        """
        rows = self.bq.query(
            sql,
            {"start_date": period.start, "end_date": period.end},
        ).rows
        return TimeSeries(
            service_line=sl.key,
            period=period,
            points=[
                TimeSeriesPoint(
                    date=r["date"],
                    leads=int(r.get("leads") or 0),
                    cost=float(r.get("cost") or 0.0),
                )
                for r in rows
            ],
        )

    # ---- lead source mix ----
    def lead_source_mix(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
    ) -> list[dict]:
        sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
        if not sl:
            raise ValueError(f"unknown service line: {service_line}")
        period = period or Period.last_n_days(30)

        leads_agg = (
            "COUNT(*) AS leads" if sl.dataset == "data_mart_all" else "SUM(leads) AS leads"
        )

        sql = f"""
            SELECT
              COALESCE(lead_source, '(none)') AS source,
              COALESCE(lead_medium, '(none)') AS medium,
              {leads_agg},
              ANY_VALUE(is_paid_lead) AS is_paid
            FROM `oh-data-warehouse.{sl.dataset}.leads`
            WHERE date BETWEEN @start_date AND @end_date
            GROUP BY source, medium
            ORDER BY leads DESC
            LIMIT 50
        """
        return self.bq.query(
            sql,
            {"start_date": period.start, "end_date": period.end},
        ).rows

    # ---- top keywords (paid search only) ----
    def top_keywords(
        self,
        service_line: str | ServiceLine,
        period: Period | None = None,
        limit: int = 25,
    ) -> list[dict]:
        sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
        if not sl:
            raise ValueError(f"unknown service line: {service_line}")
        period = period or Period.last_n_days(30)

        sql = f"""
            SELECT
              keyword_text,
              ANY_VALUE(keyword_match_type) AS match_type,
              ANY_VALUE(ad_source) AS source,
              SUM(clicks) AS clicks,
              SUM(impressions) AS impressions,
              SUM(cost) AS cost,
              SUM(leads) AS leads
            FROM `oh-data-warehouse.{sl.dataset}.paid_keywords_w_leads`
            WHERE date BETWEEN @start_date AND @end_date
              AND keyword_text IS NOT NULL
            GROUP BY keyword_text
            ORDER BY leads DESC, cost DESC
            LIMIT @row_limit
        """
        return self.bq.query(
            sql,
            {"start_date": period.start, "end_date": period.end, "row_limit": limit},
        ).rows
