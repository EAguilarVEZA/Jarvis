# Jarvis Reporting Module

BigQuery-backed reporting that powers both Jarvis's voice answers and HTML dashboards from a single source of truth.

## What's in here

```
reporting/
├── catalog.py        # All 14 service lines + aliases ("heart" → cardiology)
├── bq_client.py      # BigQuery wrapper with on-disk caching (6h TTL)
├── queries.py        # One function per business question, returns structured data
├── voice.py          # Intent parsing + spoken-answer formatting
├── dashboard.py      # Single-file HTML report generator
├── routes.py         # FastAPI routes — drop into server.py
└── smoke_test.py     # Verifies it works against the real warehouse
```

## Install on your Mac

```bash
# 1. Copy the package into your jarvis folder
cp -r reporting ~/jarvis/

# 2. Install the BigQuery client (if you don't already have it)
cd ~/jarvis && source venv/bin/activate
pip install google-cloud-bigquery

# 3. Run the smoke test FIRST — confirms it works before touching server.py
python -m reporting.smoke_test
```

If the smoke test prints `✅ All smoke tests passed`, you're clear to wire it in. If it fails, fix that error before going further.

## Wire into server.py

Add these two lines near the top of `server.py`, after `app = FastAPI(...)`:

```python
from reporting.routes import register_reporting_routes
reports = register_reporting_routes(app)
```

That's it. You now have:

- `GET  /api/report/health` — BQ healthcheck for the startup banner
- `GET  /api/report/service-lines` — list of all 14 service lines
- `GET  /api/report/{service_line}/summary?days=30` — KPIs JSON
- `GET  /api/report/{service_line}/campaigns?days=30&order_by=cost` — top campaigns
- `GET  /api/report/{service_line}/dashboard?days=30` — full HTML report
- `POST /api/report/voice` — `{"question": "..."}` → spoken answer

## Wire into the voice loop

In `server.py`, find where you handle user transcripts (the `User: ...` log line).
Before sending the transcript to Claude, try the reporting matcher first:

```python
from reporting import answer_question

# inside your transcript handler:
spoken = answer_question(reports, transcript)
if spoken:
    await speak(spoken)            # your existing TTS function
    return                         # don't fall through to Claude
# else: fall through to Claude as you do today
```

This means questions like *"what's our CPA for cardiology this month?"* are answered directly from BigQuery in ~1 second instead of going through Claude. Anything that doesn't match a reporting intent (e.g. "what's the weather") falls through unchanged.

## Update the startup banner

Replace the BigQuery healthcheck in your startup sequence with:

```python
from reporting import BQClient

bq = BQClient(credentials_path=os.path.expanduser("~/.config/jarvis/bigquery-key.json"))
health = bq.healthcheck()
log.info(f"[OK] BigQuery — oh-data-warehouse connected")
log.info(f"[OK] BigQuery — {health['total_leads_90d']:,} total leads across all service lines (last 90 days)")
```

This is exactly what the banner already prints — but now driven by the same client the rest of the reporting uses.

## How caching works

Every query result is pickled to `~/.cache/jarvis/bq/` keyed by SHA-256 of the SQL + parameters. TTL is 6 hours (data marts only update overnight). To clear:

```bash
rm -rf ~/.cache/jarvis/bq/
```

Bump `CACHE_VERSION` in `bq_client.py` if you change a query's logic and need to invalidate everything programmatically.

## Adding a new business question

1. Add a method to `Reports` in `queries.py` that returns a dataclass.
2. Add a route in `routes.py` if you want it over HTTP.
3. (Optional) Add a regex in `METRIC_PATTERNS` in `voice.py` and a branch in `format_voice_answer` if you want voice support.
4. Write a test in `tests/test_reporting.py` using `FakeBQ`.

## Tests

```bash
python tests/test_reporting.py    # 26 tests, no external dependencies
```

The tests use a fake BQ client so they run instantly without hitting the real warehouse or needing credentials.

## What's NOT covered yet

These need follow-up work:

- **Salesforce CRM data** (`fivetran_salesforce.*`) — for the lead → contact → opportunity → revenue pipeline. The marts don't include downstream conversion, only top-of-funnel leads.
- **GA4 web analytics** (`gold.ga4_fact_events` — 340M rows) — for sessions, page views, conversions.
- **Search Console** (`gold.gsc_fact_site_impression` — 294M rows) — for organic keyword performance.
- **Epic / EHR data** (`epic.*`) — for actual patient appointments and revenue.

Each of those is a separate workstream — the data is there, the code just doesn't query it yet. Add them as new methods to `Reports` when needed.
