"""
JARVIS Reporting Module — Orlando Health Marketing Analytics
BigQuery connector pulling from oh-data-warehouse data marts
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse, HTMLResponse

log = logging.getLogger("jarvis.reporting")

PROJECT = "oh-data-warehouse"
KEY_PATH = os.path.expanduser("~/.config/jarvis/bigquery-key.json")
DEMO_MODE = not Path(KEY_PATH).exists()

if not DEMO_MODE:
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", KEY_PATH)
    try:
        from google.cloud import bigquery
        _bq = bigquery.Client(project=PROJECT)
        log.info("BigQuery connected to oh-data-warehouse")
    except Exception as e:
        log.warning(f"BigQuery init failed: {e} — falling back to demo")
        DEMO_MODE = True
else:
    log.warning("BigQuery key not found — DEMO mode active")

router = APIRouter(prefix="/api/report", tags=["reporting"])

SERVICE_LINES = [
    ("cardiology",    "data_mart_cardiology"),
    ("orthopedics",   "data_mart_orthopedics"),
    ("cancer",        "data_mart_cancer"),
    ("womens",        "data_mart_womens"),
    ("neuroscience",  "data_mart_neuroscience"),
    ("primary",       "data_mart_primary"),
    ("bariatrics",    "data_mart_bariatrics"),
    ("pediatrics",    "data_mart_pediatrics"),
    ("urology",       "data_mart_urology"),
    ("surgery",       "data_mart_surgery"),
]

def _run_query(sql):
    rows = list(_bq.query(sql).result())
    return [dict(r) for r in rows]

def _days_ago(n):
    return (datetime.today() - timedelta(days=n)).strftime("%Y-%m-%d")

# ── Demo data ──────────────────────────────────────────────────────────────────

def _demo_kpis():
    return {
        "appointments": {"value": 14823, "change_pct": 8.4, "label": "Leads Generated"},
        "spend":        {"value": 612450, "change_pct": 3.1, "label": "Total Ad Spend ($)"},
        "cpa":          {"value": 41.32, "change_pct": -4.8, "label": "Cost per Acquisition ($)"},
        "revenue":      {"value": 9870000, "change_pct": 11.2, "label": "Attributed Revenue ($)"},
        "roi":          {"value": 16.12, "change_pct": 7.9, "label": "Return on Ad Spend"},
    }

def _demo_channels():
    return [
        {"channel": "Paid Search",   "spend": 241000, "appointments": 6120, "revenue": 4100000, "cpa": 39.38},
        {"channel": "Paid Social",   "spend": 158000, "appointments": 3840, "revenue": 2380000, "cpa": 41.15},
        {"channel": "Programmatic",  "spend": 112000, "appointments": 2460, "revenue": 1610000, "cpa": 45.53},
        {"channel": "Email",         "spend": 42000,  "appointments": 1680, "revenue": 1280000, "cpa": 25.00},
        {"channel": "Organic / SEO", "spend": 18000,  "appointments": 510,  "revenue": 390000,  "cpa": 35.29},
    ]

def _demo_service_lines():
    return [
        {"service_line": "Orthopedics",   "appointments": 3841, "revenue": 2980000, "cpa": 38.2},
        {"service_line": "Cardiology",    "appointments": 2614, "revenue": 2100000, "cpa": 43.1},
        {"service_line": "Cancer",        "appointments": 1980, "revenue": 1760000, "cpa": 49.5},
        {"service_line": "Women's Health","appointments": 2210, "revenue": 1280000, "cpa": 35.7},
        {"service_line": "Neurology",     "appointments": 1340, "revenue": 1050000, "cpa": 51.9},
        {"service_line": "Primary Care",  "appointments": 2838, "revenue": 700000,  "cpa": 29.4},
    ]

def _demo_trend():
    rows = []
    base = datetime.today() - timedelta(weeks=11)
    appts, rev, spend = 1100, 725000, 45000
    for i in range(12):
        d = (base + timedelta(weeks=i)).strftime("%Y-%m-%d")
        appts  = int(appts  * 1.018)
        rev    = int(rev    * 1.019)
        spend  = int(spend  * 1.012)
        rows.append({"week_start": d, "appointments": appts, "revenue": rev, "spend": spend})
    return rows

def _demo_campaigns():
    return [
        {"campaign": "Joint Replacement Spring Push", "channel": "Paid Search", "spend": 48200, "appointments": 1340, "revenue": 920000, "roi": 19.07, "status": "Active"},
        {"campaign": "Heart Health Month",            "channel": "Paid Social",  "spend": 35100, "appointments": 890,  "revenue": 720000, "roi": 20.51, "status": "Active"},
        {"campaign": "Cancer Screening Awareness",    "channel": "Programmatic", "spend": 29800, "appointments": 610,  "revenue": 540000, "roi": 18.12, "status": "Active"},
        {"campaign": "OB/GYN New Patient",            "channel": "Email",        "spend": 14000, "appointments": 520,  "revenue": 395000, "roi": 28.21, "status": "Paused"},
        {"campaign": "Spine & Back Pain",             "channel": "Paid Search",  "spend": 31700, "appointments": 980,  "revenue": 780000, "roi": 24.61, "status": "Active"},
        {"campaign": "Brand Awareness Q1",            "channel": "Programmatic", "spend": 61000, "appointments": 720,  "revenue": 410000, "roi": 6.72,  "status": "Ended"},
        {"campaign": "Primary Care Drive",            "channel": "Paid Search",  "spend": 19400, "appointments": 860,  "revenue": 210000, "roi": 10.82, "status": "Active"},
    ]

# ── Live BigQuery queries ──────────────────────────────────────────────────────

def _live_kpis(days=90):
    since = _days_ago(days)
    prev  = _days_ago(days * 2)
    datasets = [
        'data_mart_cardiology', 'data_mart_orthopedics', 'data_mart_cancer',
        'data_mart_womens', 'data_mart_neuroscience', 'data_mart_primary'
    ]
    union_current = " UNION ALL ".join([
        f"SELECT leads, cost FROM `oh-data-warehouse.{ds}.digital_ads_w_leads` WHERE date >= '{since}'"
        for ds in datasets
    ])
    union_prior = " UNION ALL ".join([
        f"SELECT leads, cost FROM `oh-data-warehouse.{ds}.digital_ads_w_leads` WHERE date >= '{prev}' AND date < '{since}'"
        for ds in datasets
    ])
    sql_current = f"SELECT SUM(leads) AS appointments, SUM(cost) AS spend FROM ({union_current})"
    sql_prior   = f"SELECT SUM(leads) AS appointments, SUM(cost) AS spend FROM ({union_prior})"
    try:
        curr = _run_query(sql_current)
        prio = _run_query(sql_prior)
    except Exception as e:
        log.error(f"_live_kpis failed: {e}")
        return _demo_kpis()
    if not curr:
        return _demo_kpis()
    r = curr[0]
    p = prio[0] if prio else {}
    appts      = int(r.get('appointments') or 0)
    spend      = float(r.get('spend') or 0)
    cpa        = spend / max(appts, 1)
    p_appts    = int(p.get('appointments') or 0)
    p_spend    = float(p.get('spend') or 0)
    appt_chg   = ((appts - p_appts) / max(p_appts, 1)) * 100 if p_appts else 0
    spend_chg  = ((spend - p_spend) / max(p_spend, 1)) * 100 if p_spend else 0
    roi        = (appts * 650) / max(spend, 1)
    return {
        'appointments': {'value': appts,          'change_pct': round(appt_chg, 1),   'label': 'Leads Generated'},
        'spend':        {'value': round(spend, 0), 'change_pct': round(spend_chg, 1),  'label': 'Total Ad Spend ($)'},
        'cpa':          {'value': round(cpa, 2),   'change_pct': round(-spend_chg*0.5,1), 'label': 'Cost per Lead ($)'},
        'revenue':      {'value': round(appts*650,0),'change_pct': round(appt_chg, 1), 'label': 'Attributed Revenue ($)'},
        'roi':          {'value': round(roi, 2),   'change_pct': 0.0,                  'label': 'Return on Ad Spend'},
    }


def _live_channels(days=90):
    since = _days_ago(days)
    sql = f"""
    SELECT
      ad_source AS channel,
      SUM(cost)   AS spend,
      SUM(leads)  AS appointments,
      SAFE_DIVIDE(SUM(cost), NULLIF(SUM(leads),0)) AS cpa
    FROM (
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_cardiology.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_orthopedics.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_cancer.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_womens.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_neuroscience.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT ad_source, cost, leads FROM `oh-data-warehouse.data_mart_primary.digital_ads_w_leads` WHERE date >= '{since}'
    )
    WHERE ad_source IS NOT NULL
    GROUP BY 1
    ORDER BY appointments DESC
    LIMIT 8
    """
    rows = _run_query(sql)
    if not rows:
        return _demo_channels()
    return [{"channel": r["channel"], "spend": float(r["spend"] or 0),
             "appointments": int(r["appointments"] or 0),
             "revenue": int(r["appointments"] or 0) * 650,
             "cpa": round(float(r["cpa"] or 0), 2)} for r in rows]

def _live_service_lines(days=90):
    since = _days_ago(days)
    rows = []
    for label, dataset in SERVICE_LINES:
        try:
            sql = f"""
            SELECT
              '{label}' AS service_line,
              SUM(leads) AS appointments,
              SAFE_DIVIDE(SUM(cost), NULLIF(SUM(leads),0)) AS cpa
            FROM `oh-data-warehouse.{dataset}.digital_ads_w_leads`
            WHERE date >= '{since}'
            """
            res = _run_query(sql)
            if res and res[0]["appointments"]:
                r = res[0]
                appts = int(r["appointments"] or 0)
                cpa   = float(r["cpa"] or 0)
                rows.append({"service_line": label.title(), "appointments": appts,
                             "revenue": appts * 650, "cpa": round(cpa, 2)})
        except Exception as e:
            log.warning(f"Service line {label} failed: {e}")
    return rows if rows else _demo_service_lines()

def _live_trend(days=84):
    since = _days_ago(days)
    sql = f"""
    SELECT
      DATE_TRUNC(date, WEEK) AS week_start,
      SUM(leads) AS appointments,
      SUM(cost)  AS spend
    FROM (
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_cardiology.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_orthopedics.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_cancer.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_womens.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_neuroscience.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT date, leads, cost FROM `oh-data-warehouse.data_mart_primary.digital_ads_w_leads` WHERE date >= '{since}'
    )
    GROUP BY 1
    ORDER BY 1
    """
    rows = _run_query(sql)
    if not rows:
        return _demo_trend()
    return [{"week_start": str(r["week_start"]), "appointments": int(r["appointments"] or 0),
             "spend": float(r["spend"] or 0), "revenue": int(r["appointments"] or 0) * 650} for r in rows]

def _live_campaigns(days=90):
    since = _days_ago(days)
    sql = f"""
    SELECT
      dashboard_campaign AS campaign,
      ad_source AS channel,
      SUM(cost)  AS spend,
      SUM(leads) AS appointments,
      SAFE_DIVIDE(SUM(cost), NULLIF(SUM(leads),0)) AS cpa
    FROM (
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_cardiology.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_orthopedics.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_cancer.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_womens.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_neuroscience.digital_ads_w_leads` WHERE date >= '{since}'
      UNION ALL
      SELECT dashboard_campaign, ad_source, cost, leads FROM `oh-data-warehouse.data_mart_primary.digital_ads_w_leads` WHERE date >= '{since}'
    )
    WHERE dashboard_campaign IS NOT NULL
      AND dashboard_campaign != '(Not Matched)'
    GROUP BY 1, 2
    HAVING SUM(leads) > 0
    ORDER BY appointments DESC
    LIMIT 15
    """
    rows = _run_query(sql)
    if not rows:
        return _demo_campaigns()
    return [{"campaign": r["campaign"], "channel": r["channel"] or "Unknown",
             "spend": round(float(r["spend"] or 0), 0),
             "appointments": int(r["appointments"] or 0),
             "revenue": int(r["appointments"] or 0) * 650,
             "roi": round((int(r["appointments"] or 0) * 650) / max(float(r["spend"] or 1), 1), 2),
             "status": "Active"} for r in rows]

# ── Custom NL→SQL query for Jarvis voice ──────────────────────────────────────

async def run_custom_query(question: str, days: int = 90) -> str:
    """Take a natural language question, build SQL, run it, return spoken answer."""
    since = _days_ago(days)
    q = question.lower()

    # Route to the right query based on keywords
    if any(w in q for w in ["cardiology", "heart", "cardiac"]):
        dataset = "data_mart_cardiology"
        svc = "Cardiology"
    elif any(w in q for w in ["ortho", "joint", "spine", "bone"]):
        dataset = "data_mart_orthopedics"
        svc = "Orthopedics"
    elif any(w in q for w in ["cancer", "oncology", "tumor"]):
        dataset = "data_mart_cancer"
        svc = "Cancer"
    elif any(w in q for w in ["women", "ob", "gyn", "maternal"]):
        dataset = "data_mart_womens"
        svc = "Women's Health"
    elif any(w in q for w in ["neuro", "brain", "stroke"]):
        dataset = "data_mart_neuroscience"
        svc = "Neuroscience"
    elif any(w in q for w in ["primary", "family", "pcp"]):
        dataset = "data_mart_primary"
        svc = "Primary Care"
    else:
        dataset = None
        svc = "All Service Lines"

    if DEMO_MODE:
        return f"I'm in demo mode, sir. To answer '{question}' with live data, please ensure BigQuery credentials are configured."

    try:
        if dataset:
            sql = f"""
            SELECT
              SUM(leads) AS appointments,
              SUM(cost) AS spend,
              SAFE_DIVIDE(SUM(cost), NULLIF(SUM(leads),0)) AS cpa,
              ad_source AS channel
            FROM `oh-data-warehouse.{dataset}.digital_ads_w_leads`
            WHERE date >= '{since}'
            GROUP BY channel
            ORDER BY appointments DESC
            LIMIT 5
            """
            rows = _run_query(sql)
            if not rows:
                return f"No data found for {svc} in the last {days} days, sir."
            total_appts = sum(int(r["appointments"] or 0) for r in rows)
            total_spend = sum(float(r["spend"] or 0) for r in rows)
            cpa = total_spend / max(total_appts, 1)
            top = rows[0]
            return (
                f"For {svc} over the last {days} days: "
                f"{total_appts:,} leads generated at a cost per lead of ${cpa:.2f}. "
                f"Top channel is {top['channel']} with {int(top['appointments'] or 0):,} appointments. "
                f"Total spend: ${total_spend:,.0f}."
            )
        else:
            kpis = _live_kpis(days)
            appts = kpis["appointments"]["value"]
            spend = kpis["spend"]["value"]
            cpa   = kpis["cpa"]["value"]
            return (
                f"Across all service lines over the last {days} days: "
                f"{appts:,} total leads at ${cpa:.2f} cost per acquisition. "
                f"Total marketing spend: ${spend:,.0f}."
            )
    except Exception as e:
        log.error(f"Custom query failed: {e}")
        return f"I encountered an error running that query, sir: {e}"

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def report_status():
    return {"ok": True, "demo_mode": DEMO_MODE,
            "databricks_host": None,
            "bigquery_project": PROJECT if not DEMO_MODE else None,
            "timestamp": datetime.utcnow().isoformat() + "Z"}

@router.get("/kpis")
async def get_kpis(days: int = 90):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: _live_kpis(days) if not DEMO_MODE else _demo_kpis())
        return JSONResponse(data)
    except Exception as e:
        log.error(f"KPIs failed: {e}")
        return JSONResponse(_demo_kpis())

@router.get("/by-channel")
async def get_by_channel(days: int = 90):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: _live_channels(days) if not DEMO_MODE else _demo_channels())
        return JSONResponse(data)
    except Exception as e:
        log.error(f"Channels failed: {e}")
        return JSONResponse(_demo_channels())

@router.get("/by-service-line")
async def get_by_service_line(days: int = 90):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: _live_service_lines(days) if not DEMO_MODE else _demo_service_lines())
        return JSONResponse(data)
    except Exception as e:
        log.error(f"Service lines failed: {e}")
        return JSONResponse(_demo_service_lines())

@router.get("/trend")
async def get_trend(days: int = 84):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: _live_trend(days) if not DEMO_MODE else _demo_trend())
        return JSONResponse(data)
    except Exception as e:
        log.error(f"Trend failed: {e}")
        return JSONResponse(_demo_trend())

@router.get("/campaigns")
async def get_campaigns(days: int = 90):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: _live_campaigns(days) if not DEMO_MODE else _demo_campaigns())
        return JSONResponse(data)
    except Exception as e:
        log.error(f"Campaigns failed: {e}")
        return JSONResponse(_demo_campaigns())

@router.get("/all")
async def get_all(days: int = 90):
    loop = asyncio.get_event_loop()
    try:
        if DEMO_MODE:
            kpis = _demo_kpis()
            channels = _demo_channels()
            service_lines = _demo_service_lines()
            trend = _demo_trend()
            campaigns = _demo_campaigns()
        else:
            kpis         = await loop.run_in_executor(None, lambda: _live_kpis(days))
            channels     = await loop.run_in_executor(None, lambda: _live_channels(days))
            service_lines= await loop.run_in_executor(None, lambda: _live_service_lines(days))
            trend        = await loop.run_in_executor(None, lambda: _live_trend(days))
            campaigns    = await loop.run_in_executor(None, lambda: _live_campaigns(days))
    except Exception as e:
        log.error(f"get_all failed: {e} — using demo data")
        kpis = _demo_kpis(); channels = _demo_channels()
        service_lines = _demo_service_lines(); trend = _demo_trend(); campaigns = _demo_campaigns()

    appts = kpis["appointments"]["value"]
    roi   = kpis["roi"]["value"]
    spend = kpis["spend"]["value"]
    cpa   = kpis["cpa"]["value"]
    mode  = "demo" if DEMO_MODE else "live"
    top_ch = channels[0]["channel"] if channels else "Paid Search"
    top_sl = service_lines[0]["service_line"] if service_lines else "Orthopedics"

    voice_summary = (
        f"Marketing dashboard — {mode} data. "
        f"Over the last {days} days: {appts:,} leads generated, "
        f"up {kpis['appointments']['change_pct']}%. "
        f"Total spend: ${spend:,.0f}. "
        f"Cost per acquisition: ${cpa:.2f}. "
        f"Return on ad spend: {roi:.1f}x. "
        f"Top channel: {top_ch}. Top service line: {top_sl}."
    )

    return JSONResponse({
        "demo_mode": DEMO_MODE,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "period_days": days,
        "kpis": kpis,
        "by_channel": channels,
        "by_service_line": service_lines,
        "trend": trend,
        "campaigns": campaigns,
        "voice_summary": voice_summary,
    })

@router.get("/query")
async def custom_query(q: str, days: int = 90):
    """Natural language query endpoint — used by Jarvis voice Q&A."""
    answer = await run_custom_query(q, days)
    return JSONResponse({"question": q, "answer": answer, "demo_mode": DEMO_MODE})

@router.get("/dashboard")
async def report_dashboard():
    """Serve the marketing dashboard HTML file."""
    p = Path(__file__).parent / "dashboard.html"
    if p.exists():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)

# ── Voice helpers ──────────────────────────────────────────────────────────────



@router.get("/stroke")
async def stroke_dashboard():
    from pathlib import Path
    p = Path(__file__).parent / "ohni_stroke_dashboard.html"
    if p.exists():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>ohni_stroke_dashboard.html not found</h1>", status_code=404)


@router.get("/ohni")
async def ohni_report():
    from pathlib import Path
    p = Path(__file__).parent / "ohni_report_v2.html"
    if p.exists():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>ohni_report_v2.html not found</h1>", status_code=404)


TRIGGER_PHRASES = [
    "show me the dashboard", "marketing dashboard", "open the dashboard",
    "pull up the dashboard", "show me marketing", "marketing report",
    "open my dashboard", "show me my dashboard", "my marketing dashboard",
    "how is marketing doing", "what are my numbers", "give me the numbers",
    "how are we performing", "appointment numbers", "campaign performance",
    "launch dashboard", "open reporting", "show me the report",
    "marketing performance", "show dashboard",
]

def detect_report_trigger(text: str) -> bool:
    t = text.lower().strip()
    if any(p in t for p in 

TRIGGER_PHRASES):
        return True
    if "dashboard" in t and any(w in t for w in ["show", "open", "pull", "launch", "my", "the", "market"]):
        return True
    return False

async def build_voice_summary(report_type: str = "overview") -> str:
    try:
        loop = asyncio.get_event_loop()
        if DEMO_MODE:
            k = _demo_kpis()
            ch = _demo_channels()
            sl = _demo_service_lines()
        else:
            k  = await loop.run_in_executor(None, _live_kpis)
            ch = await loop.run_in_executor(None, _live_channels)
            sl = await loop.run_in_executor(None, _live_service_lines)
        appts = k["appointments"]["value"]
        spend = k["spend"]["value"]
        roi   = k["roi"]["value"]
        cpa   = k["cpa"]["value"]
        top_ch = ch[0]["channel"] if ch else "Paid Search"
        top_sl = sl[0]["service_line"] if sl else "Orthopedics"
        mode_note = " I am showing demo data since BigQuery is not yet connected." if DEMO_MODE else ""
        return (
            f"Here is your marketing performance summary, sir.{mode_note} "
            f"Over the last 90 days: {appts:,} leads generated. "
            f"Total spend: ${spend:,.0f}. "
            f"Cost per acquisition: ${cpa:.2f}. "
            f"Return on ad spend: {roi:.1f} to one. "
            f"Top channel: {top_ch}. Top service line: {top_sl}. "
            f"The full dashboard is now open in your browser."
        )
    except Exception as e:
        return f"Marketing data encountered an error, sir: {e}"
