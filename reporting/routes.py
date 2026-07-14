"""
FastAPI routes for Jarvis reporting.

Drop this into server.py with:

    from reporting.routes import register_reporting_routes
    register_reporting_routes(app, bq_credentials_path="~/.config/jarvis/bigquery-key.json")

The routes:
  GET  /api/report/health                  → BQ healthcheck (used at startup)
  GET  /api/report/service-lines           → catalog for the frontend dropdown
  GET  /api/report/{service_line}/summary  → numbers JSON
  GET  /api/report/{service_line}/dashboard → full HTML dashboard
  POST /api/report/voice                   → {"question": "..."} → spoken answer
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

from . import (
    BQClient,
    Reports,
    Period,
    SERVICE_LINES,
    answer_question,
    build_dashboard,
    resolve,
)

log = logging.getLogger("jarvis.reporting.routes")


def register_reporting_routes(app, bq_credentials_path: str | None = None):
    """Attach reporting routes to a FastAPI app instance.

    Imports FastAPI lazily so this module is importable in a unit-test
    environment that doesn't have FastAPI installed.
    """
    from fastapi import HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    creds = (
        os.path.expanduser(bq_credentials_path)
        if bq_credentials_path
        else os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    bq = BQClient(credentials_path=creds)
    reports = Reports(bq)

    @app.get("/api/report/health")
    def health():
        try:
            return bq.healthcheck()
        except Exception as e:
            log.exception("BQ healthcheck failed")
            raise HTTPException(status_code=503, detail=str(e))

    @app.get("/api/report/service-lines")
    def service_lines():
        return [
            {
                "key": sl.key,
                "label": sl.label,
                "dataset": sl.dataset,
                "aliases": list(sl.aliases),
            }
            for sl in SERVICE_LINES
        ]

    @app.get("/api/report/{service_line}/summary")
    def summary(service_line: str, days: int = 30):
        sl = resolve(service_line)
        if not sl:
            raise HTTPException(404, f"unknown service line: {service_line}")
        period = Period.last_n_days(max(1, min(days, 365)))
        return reports.summary_with_comparison(sl, period)

    @app.get("/api/report/{service_line}/campaigns")
    def campaigns(service_line: str, days: int = 30, limit: int = 20, order_by: str = "cost"):
        sl = resolve(service_line)
        if not sl:
            raise HTTPException(404, f"unknown service line: {service_line}")
        period = Period.last_n_days(max(1, min(days, 365)))
        breakdown = reports.top_campaigns(sl, period, limit=limit, order_by=order_by)
        return {
            "service_line": breakdown.service_line,
            "period": {"start": str(period.start), "end": str(period.end)},
            "campaigns": [
                {
                    "campaign_name": c.campaign_name,
                    "dashboard_campaign": c.dashboard_campaign,
                    "ad_source": c.ad_source,
                    "cost": c.cost,
                    "clicks": c.clicks,
                    "impressions": c.impressions,
                    "leads": c.leads,
                    "cpa": c.cpa,
                }
                for c in breakdown.campaigns
            ],
        }

    @app.get("/api/report/{service_line}/dashboard", response_class=HTMLResponse)
    def dashboard(service_line: str, days: int = 30):
        sl = resolve(service_line)
        if not sl:
            raise HTTPException(404, f"unknown service line: {service_line}")
        period = Period.last_n_days(max(1, min(days, 365)))
        try:
            html = build_dashboard(reports, sl, period)
        except Exception:
            log.exception("dashboard build failed for %s", service_line)
            raise HTTPException(500, "report generation failed")
        return HTMLResponse(content=html)

    @app.post("/api/report/voice")
    async def voice(request: Request):
        body = await request.json()
        question = body.get("question") or ""
        try:
            answer = answer_question(reports, question)
        except Exception:
            log.exception("voice answer failed for: %s", question)
            return JSONResponse({"answer": None, "error": "query failed"}, status_code=500)
        if answer is None:
            return {"answer": None, "matched": False}
        return {"answer": answer, "matched": True}

    log.info("Reporting routes registered: 5 endpoints")
    return reports  # caller can hold a reference for direct calls (e.g. voice handler)
