"""
probe_ga4.py — one-off proof that we can pull real GA4 sessions-by-source from BigQuery.
Cheap by design: short 7-day window, and it DRY-RUNS first to show bytes/cost and
refuses to run anything that would scan more than MAX_GB.

Run:  cd ~/Jarvis && python probe_ga4.py
"""
import os

# ---- load creds/project from .env (same ones the server uses) ----
def _load_env():
    try:
        for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass
_load_env()

PROJECT = os.environ.get("BIGQUERY_PROJECT_ID", "oh-data-warehouse")
PROPERTIES = ["analytics_352423547", "analytics_352444842"]
START, END = "20260718", "20260724"   # short 7-day window = tiny scan
MAX_GB = 3.0                          # refuse anything bigger

from google.cloud import bigquery
client = bigquery.Client(project=PROJECT)


def _dry_gb(sql):
    cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client.query(sql, job_config=cfg)
    return job.total_bytes_processed / 1e9


def run(label, sql):
    gb = _dry_gb(sql)
    cost = gb / 1000 * 5  # ~$5 per TB scanned
    print(f"    [{label}] scan ~{gb:.3f} GB  (~${cost:.4f})")
    if gb > MAX_GB:
        print(f"    SKIPPED — would scan more than {MAX_GB} GB")
        return None
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(MAX_GB * 1e9))
    return list(client.query(sql, job_config=cfg).result())


for prop in PROPERTIES:
    tbl = f"`{PROJECT}.{prop}.events_*`"
    where = f"_TABLE_SUFFIX BETWEEN '{START}' AND '{END}'"
    print(f"\n=== {prop}  ({START}–{END}) ===")

    totals = run("totals", f"""
        SELECT
          COUNT(DISTINCT user_pseudo_id) AS users,
          COUNT(DISTINCT CONCAT(user_pseudo_id,'-',CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING))) AS sessions,
          COUNTIF(event_name='page_view') AS page_views
        FROM {tbl} WHERE {where}
    """)
    if totals:
        r = totals[0]
        print(f"    users={r['users']:,}  sessions={r['sessions']:,}  page_views={r['page_views']:,}")

    hosts = run("top hostnames", f"""
        SELECT host, COUNT(*) AS events FROM (
          SELECT NET.HOST((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location')) AS host
          FROM {tbl} WHERE {where} AND event_name='page_view'
        ) GROUP BY host ORDER BY events DESC LIMIT 5
    """)
    if hosts:
        print("    top hostnames:")
        for h in hosts:
            print(f"      {h['host']}: {h['events']:,} page_views")

    src = run("sessions by source", f"""
        WITH s AS (
          SELECT
            CONCAT(user_pseudo_id,'-',CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING)) AS sess,
            ARRAY_AGG((SELECT value.string_value FROM UNNEST(event_params) WHERE key='source') IGNORE NULLS ORDER BY event_timestamp)[SAFE_OFFSET(0)] AS source
          FROM {tbl} WHERE {where}
          GROUP BY sess
        )
        SELECT COALESCE(NULLIF(source,''),'(direct)') AS source, COUNT(*) AS sessions
        FROM s GROUP BY source ORDER BY sessions DESC LIMIT 10
    """)
    if src:
        print("    sessions by source:")
        for row in src:
            print(f"      {row['source']}: {row['sessions']:,}")

print("\nDone.")
