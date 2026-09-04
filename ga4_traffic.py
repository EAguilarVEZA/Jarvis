"""
ga4_traffic.py — full website-traffic dataset for a brand, from the GA4 export in
BigQuery. One marketing property (analytics_352423547) holds every brand site;
pick the brand by HOSTNAME (e.g. www.baptisthealthal.com).

Pulls every metric the 3-page traffic report needs, to GA4-standard definitions:
  Page 1: YoY totals (sessions/users/page_views), sessions by source, top movers.
  Page 2: engagement rate + avg engagement time + engaged sessions (YoY),
          most-visited pages, key conversion events.
  Page 3: paid vs organic share, paid totals, sessions by campaign type, top campaigns.

Cheap + safe: every query DRY-RUNS first and prints bytes/cost; hard-capped.

Library:   ds = ga4_traffic.build_dataset("www.baptisthealthal.com", verbose=True)
Shell:     cd ~/Jarvis && python ga4_traffic.py www.baptisthealthal.com
"""
import os
import re
import datetime


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
PROPERTY = "analytics_352423547"
MAX_GB = 150.0


def _yago(d, days=365):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _windows(timeframe="fiscal ytd", today=None):
    """Return (cs,ce,ps,pe, csd,ced,psd,ped, label) for a friendly timeframe string.
    Current window vs the same window one year earlier."""
    today = today or datetime.date.today()
    tf = (timeframe or "").lower().strip()
    # Custom explicit range: two ISO dates anywhere in the string (YYYY-MM-DD ... YYYY-MM-DD).
    m = re.search(r"(\d{4}-\d{2}-\d{2}).{0,6}?(\d{4}-\d{2}-\d{2})", tf)
    if m:
        try:
            cs = datetime.date.fromisoformat(m.group(1)); ce = datetime.date.fromisoformat(m.group(2))
            if ce < cs:
                cs, ce = ce, cs
            ps, pe = _yago(cs), _yago(ce)
            f = lambda d: d.strftime("%Y%m%d")
            return f(cs), f(ce), f(ps), f(pe), cs, ce, ps, pe, f"Custom range ({cs.isoformat()} to {ce.isoformat()})"
        except ValueError:
            pass  # bad date -> fall through to presets
    if tf in ("", "fiscal ytd", "fiscal year to date", "fytd", "fiscal"):
        fy0 = today.year if today.month >= 10 else today.year - 1
        cs, ce = datetime.date(fy0, 10, 1), today
        label = "Fiscal year to date (Oct 1 – today)"
    elif "7" in tf or "week" in tf:
        cs, ce = today - datetime.timedelta(days=7), today; label = "Last 7 days"
    elif "30" in tf or ("month" in tf and "12" not in tf):
        cs, ce = today - datetime.timedelta(days=30), today; label = "Last 30 days"
    elif "90" in tf or "quarter" in tf:
        cs, ce = today - datetime.timedelta(days=90), today; label = "Last 90 days"
    elif "12" in tf or "trailing" in tf or "365" in tf:
        cs, ce = today - datetime.timedelta(days=365), today; label = "Last 12 months"
    elif "ytd" in tf or "year to date" in tf or "calendar" in tf:
        cs, ce = datetime.date(today.year, 1, 1), today; label = "Calendar year to date (Jan 1 – today)"
    else:  # default fiscal ytd
        fy0 = today.year if today.month >= 10 else today.year - 1
        cs, ce = datetime.date(fy0, 10, 1), today
        label = "Fiscal year to date (Oct 1 – today)"
    ps, pe = _yago(cs), _yago(ce)
    f = lambda d: d.strftime("%Y%m%d")
    return f(cs), f(ce), f(ps), f(pe), cs, ce, ps, pe, label


def _fiscal_windows(today=None):
    cs, ce, ps, pe, csd, ced, psd, ped, _ = _windows("fiscal ytd", today)
    return cs, ce, ps, pe, csd, ced, psd, ped


def _client():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def _run(client, label, sql, verbose):
    from google.cloud import bigquery
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    gb = dry.total_bytes_processed / 1e9
    if verbose:
        print(f"    [{label}] scan ~{gb:.1f} GB (~${gb/1000*5:.3f})")
    if gb > MAX_GB:
        raise RuntimeError(f"{label}: {gb:.0f} GB exceeds cap {MAX_GB}")
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(MAX_GB * 1e9))
    return list(client.query(sql, job_config=cfg).result())


# ── GA4-standard building blocks ───────────────────────────────────────────────
# Session grain with last-non-direct source/medium, engagement, and host. One CTE
# reused by most queries. Channel = GA4-style friendly label from source+medium.
_PAGE = None   # optional page-path filter (e.g. "/careers"), set by build_dataset


def _pagepred():
    return f" AND page LIKE '{_PAGE}%'" if _PAGE else ""


def _pvpage():
    # page-path filter for raw page_view subqueries (page_location present there)
    return (f" AND REGEXP_EXTRACT((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'), r'https?://[^/]+(/[^?#]*)') LIKE '{_PAGE}%'"
            if _PAGE else "")


def _sessions_cte(host, s, e):
    return f"""
    WITH ev AS (
      SELECT
        CONCAT(user_pseudo_id,'-',CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING)) AS sess,
        user_pseudo_id, event_name, event_timestamp,
        LOWER((SELECT value.string_value FROM UNNEST(event_params) WHERE key='source')) AS source,
        LOWER((SELECT value.string_value FROM UNNEST(event_params) WHERE key='medium')) AS medium,
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key='campaign') AS campaign,
        (SELECT value.int_value FROM UNNEST(event_params) WHERE key='session_engaged') AS engaged,
        (SELECT value.int_value FROM UNNEST(event_params) WHERE key='engagement_time_msec') AS eng_ms,
        NET.HOST((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location')) AS host,
        REGEXP_EXTRACT((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'), r'https?://[^/]+(/[^?#]*)') AS page
      FROM `{PROJECT}.{PROPERTY}.events_*`
      WHERE _TABLE_SUFFIX BETWEEN '{s}' AND '{e}'
    ),
    host_sess AS (SELECT DISTINCT sess FROM ev WHERE host='{host}'{_pagepred()}),
    sagg AS (
      SELECT
        e.sess,
        MAX(e.user_pseudo_id) AS uid,
        MAX(IF(e.engaged=1,1,0)) AS engaged,
        SUM(IFNULL(e.eng_ms,0)) AS eng_ms,
        ARRAY_AGG(IF(e.source IS NOT NULL AND e.source NOT IN ('(direct)','direct',''), STRUCT(e.source,e.medium), NULL) IGNORE NULLS ORDER BY e.event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] AS lc,
        ARRAY_AGG(e.campaign IGNORE NULLS ORDER BY e.event_timestamp LIMIT 1)[SAFE_OFFSET(0)] AS campaign
      FROM ev e JOIN host_sess h USING(sess)
      GROUP BY e.sess
    ),
    labeled AS (
      SELECT sess, uid, engaged, eng_ms, campaign,
        COALESCE(lc.source,'(direct)') AS source, COALESCE(lc.medium,'(none)') AS medium,
        CASE
          WHEN lc.source IS NULL THEN 'Direct'
          WHEN lc.medium='organic' THEN INITCAP(lc.source)||' organic'
          WHEN lc.medium IN ('cpc','ppc','paid','paidsearch') AND lc.source IN ('bing','microsoft') THEN 'Bing paid search'
          WHEN lc.medium IN ('cpc','ppc','paid','paidsearch') AND lc.source='google' THEN 'Google paid search'
          WHEN lc.medium='display' OR lc.medium='banner' OR lc.medium='cpm' THEN INITCAP(lc.source)||' display'
          WHEN lc.medium IN ('paid_social','paidsocial','social_paid') THEN INITCAP(lc.source)||' paid social'
          WHEN lc.source IN ('facebook','instagram','fb','ig') AND lc.medium IN ('cpc','paid') THEN INITCAP(lc.source)||' paid social'
          WHEN lc.medium IN ('social','organic_social') THEN INITCAP(lc.source)||' social'
          WHEN lc.medium='referral' THEN INITCAP(lc.source)||' referral'
          ELSE INITCAP(lc.source)
        END AS channel,
        CASE
          WHEN lc.source IS NULL THEN 'organic'
          WHEN lc.medium IN ('cpc','ppc','paid','paidsearch','display','banner','cpm','paid_social','paidsocial') THEN 'paid'
          WHEN lc.medium IN ('organic') THEN 'organic'
          WHEN lc.medium IN ('referral') THEN 'referral'
          WHEN lc.medium IN ('social','organic_social') THEN 'organic'
          ELSE 'organic'
        END AS bucket
      FROM sagg
    )"""


def _totals(host, s, e):
    return _sessions_cte(host, s, e) + f"""
    , pv AS (
      SELECT COUNT(*) AS page_views FROM `{PROJECT}.{PROPERTY}.events_*`
      WHERE _TABLE_SUFFIX BETWEEN '{s}' AND '{e}' AND event_name='page_view'
        AND NET.HOST((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'))='{host}'{_pvpage()})
    SELECT
      (SELECT COUNT(DISTINCT uid) FROM labeled) AS users,
      (SELECT COUNT(*) FROM labeled) AS sessions,
      (SELECT SUM(engaged) FROM labeled) AS engaged_sessions,
      (SELECT SAFE_DIVIDE(SUM(eng_ms), COUNT(*))/1000 FROM labeled) AS avg_eng_sec,
      (SELECT page_views FROM pv) AS page_views"""


def _by_channel(host, s, e):
    return _sessions_cte(host, s, e) + """
    SELECT channel, COUNT(*) AS sessions FROM labeled GROUP BY channel ORDER BY sessions DESC LIMIT 10"""


def _by_bucket(host, s, e):
    return _sessions_cte(host, s, e) + """
    SELECT bucket, COUNT(*) AS sessions FROM labeled GROUP BY bucket"""


def _paid_total(host, s, e):
    return _sessions_cte(host, s, e) + """
    SELECT
      COUNTIF(bucket='paid') AS paid, COUNT(*) AS total,
      COUNTIF(bucket='paid' AND (LOWER(IFNULL(campaign,'')) LIKE '%talent%' OR LOWER(IFNULL(campaign,'')) LIKE '%career%' OR LOWER(IFNULL(campaign,'')) LIKE '%recruit%' OR LOWER(IFNULL(campaign,'')) LIKE '%rn%')) AS recruitment
    FROM labeled"""


def _campaign_types(host, s, e):
    return _sessions_cte(host, s, e) + """
    SELECT
      CASE
        WHEN LOWER(IFNULL(campaign,'')) LIKE '%talent%' OR LOWER(IFNULL(campaign,'')) LIKE '%career%' OR LOWER(IFNULL(campaign,'')) LIKE '%recruit%' THEN 'Recruitment display (Talent Acq.)'
        WHEN medium IN ('cpc','ppc','paidsearch') AND (LOWER(IFNULL(campaign,'')) LIKE '%brand%') THEN 'Brand paid search'
        WHEN medium IN ('paid_social','paidsocial') OR (source IN ('facebook','instagram') AND medium IN ('cpc','paid')) THEN 'Paid social'
        WHEN LOWER(IFNULL(campaign,'')) LIKE '%gmb%' OR LOWER(IFNULL(campaign,'')) LIKE '%local%' OR LOWER(IFNULL(campaign,'')) LIKE '%business%' THEN 'Google Business / local'
        WHEN medium IN ('cpc','ppc','paidsearch') THEN 'Service-line paid search'
        WHEN medium IN ('display','cpm','banner') THEN 'Brand display'
        ELSE 'Other paid'
      END AS ctype,
      COUNT(*) AS sessions, SAFE_DIVIDE(SUM(engaged), COUNT(*)) AS eng_rate
    FROM labeled WHERE bucket='paid'
    GROUP BY ctype ORDER BY sessions DESC"""


def _top_campaigns(host, s, e):
    return _sessions_cte(host, s, e) + """
    SELECT campaign, COUNT(*) AS sessions, SAFE_DIVIDE(SUM(engaged), COUNT(*)) AS eng_rate
    FROM labeled WHERE bucket='paid' AND campaign IS NOT NULL AND campaign NOT IN ('(not set)','')
    GROUP BY campaign ORDER BY sessions DESC LIMIT 6"""


def _top_pages(host, s, e):
    return _sessions_cte(host, s, e) + f"""
    , pv AS (
      SELECT
        CONCAT(user_pseudo_id,'-',CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING)) AS sess,
        REGEXP_EXTRACT((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'), r'https?://[^/]+(/[^?#]*)') AS page,
        (SELECT value.int_value FROM UNNEST(event_params) WHERE key='engagement_time_msec') AS eng_ms
      FROM `{PROJECT}.{PROPERTY}.events_*`
      WHERE _TABLE_SUFFIX BETWEEN '{s}' AND '{e}' AND event_name='page_view'
        AND NET.HOST((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'))='{host}'{_pvpage()})
    SELECT p.page,
      COUNT(DISTINCT p.sess) AS sessions,
      COUNT(DISTINCT IF(l.engaged=1, p.sess, NULL)) AS engaged,
      SAFE_DIVIDE(SUM(IFNULL(p.eng_ms,0)), COUNT(DISTINCT p.sess))/1000 AS avg_sec
    FROM pv p JOIN labeled l USING(sess)
    GROUP BY p.page ORDER BY sessions DESC LIMIT 8"""


def _events(host, s, e):
    return f"""
    SELECT event_name,
      COUNT(*) AS events,
      COUNT(DISTINCT CONCAT(user_pseudo_id,'-',CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key='ga_session_id') AS STRING))) AS sessions
    FROM `{PROJECT}.{PROPERTY}.events_*`
    WHERE _TABLE_SUFFIX BETWEEN '{s}' AND '{e}'
      AND NET.HOST((SELECT value.string_value FROM UNNEST(event_params) WHERE key='page_location'))='{host}'
      AND event_name NOT IN ('page_view','session_start','first_visit','user_engagement','scroll')
    GROUP BY event_name ORDER BY events DESC LIMIT 15"""


def _pct(a, b):
    return None if not b else round((a - b) / b * 100, 1)


def build_dataset(host, timeframe="fiscal ytd", page=None, verbose=False):
    global _PAGE
    _PAGE = (page or "").strip() or None
    cs, ce, ps, pe, csd, ced, psd, ped, tf_label = _windows(timeframe)
    c = _client()
    ct = _run(c, "totals (cur)", _totals(host, cs, ce), verbose)[0]
    pt = _run(c, "totals (prior)", _totals(host, ps, pe), verbose)[0]
    chan = _run(c, "by channel (cur)", _by_channel(host, cs, ce), verbose)
    chan_p = {r["channel"]: r["sessions"] for r in _run(c, "by channel (prior)", _by_channel(host, ps, pe), verbose)}
    buck_c = {r["bucket"]: r["sessions"] for r in _run(c, "paid/organic (cur)", _by_bucket(host, cs, ce), verbose)}
    buck_p = {r["bucket"]: r["sessions"] for r in _run(c, "paid/organic (prior)", _by_bucket(host, ps, pe), verbose)}
    paid_c = _run(c, "paid totals (cur)", _paid_total(host, cs, ce), verbose)[0]
    paid_p = _run(c, "paid totals (prior)", _paid_total(host, ps, pe), verbose)[0]
    ctypes = _run(c, "campaign types", _campaign_types(host, cs, ce), verbose)
    tcamp = _run(c, "top campaigns", _top_campaigns(host, cs, ce), verbose)
    pages = _run(c, "top pages", _top_pages(host, cs, ce), verbose)
    events = _run(c, "key events", _events(host, cs, ce), verbose)

    def share(b):
        tot = sum(b.values()) or 1
        return {k: round(v / tot * 100, 1) for k, v in b.items()}
    ds = {
        "host": host + (_PAGE or ""), "property": PROPERTY,
        "window": {"current": f"{tf_label}: {csd.strftime('%b %d, %Y')} – {ced.strftime('%b %d, %Y')}",
                   "prior_matched": f"{psd.strftime('%b %d, %Y')} – {ped.strftime('%b %d, %Y')}"},
        "as_of": datetime.date.today().isoformat(),
        "totals": {
            "sessions": {"current": ct["sessions"], "prior": pt["sessions"], "yoy_pct": _pct(ct["sessions"], pt["sessions"])},
            "users": {"current": ct["users"], "prior": pt["users"], "yoy_pct": _pct(ct["users"], pt["users"])},
            "page_views": {"current": ct["page_views"], "prior": pt["page_views"], "yoy_pct": _pct(ct["page_views"], pt["page_views"])},
        },
        "engagement": {
            "engaged_sessions": {"current": ct["engaged_sessions"], "prior": pt["engaged_sessions"], "yoy_pct": _pct(ct["engaged_sessions"], pt["engaged_sessions"])},
            "rate": {"current": round((ct["engaged_sessions"] or 0) / (ct["sessions"] or 1) * 100, 1),
                     "prior": round((pt["engaged_sessions"] or 0) / (pt["sessions"] or 1) * 100, 1)},
            "avg_eng_sec": {"current": round(ct["avg_eng_sec"] or 0, 1), "prior": round(pt["avg_eng_sec"] or 0, 1)},
        },
        "by_channel": [{"channel": r["channel"], "sessions": r["sessions"], "yoy_pct": _pct(r["sessions"], chan_p.get(r["channel"], 0))} for r in chan],
        "movers": sorted(
            [{"channel": r["channel"], "current": r["sessions"], "prior": chan_p.get(r["channel"], 0),
              "gain": r["sessions"] - chan_p.get(r["channel"], 0), "yoy_pct": _pct(r["sessions"], chan_p.get(r["channel"], 0))}
             for r in chan],
            key=lambda m: m["gain"], reverse=True)[:3],
        "share": {"current": share(buck_c), "prior": share(buck_p)},
        "paid": {
            "sessions": {"current": paid_c["paid"], "prior": paid_p["paid"], "yoy_pct": _pct(paid_c["paid"], paid_p["paid"])},
            "share_current": round((paid_c["paid"] or 0) / (paid_c["total"] or 1) * 100, 1),
            "share_prior": round((paid_p["paid"] or 0) / (paid_p["total"] or 1) * 100, 1),
            "recruitment_current": paid_c["recruitment"], "recruitment_yoy_pct": _pct(paid_c["recruitment"], paid_p["recruitment"]),
        },
        "campaign_types": [{"type": r["ctype"], "sessions": r["sessions"], "eng_rate": round((r["eng_rate"] or 0) * 100, 1)} for r in ctypes],
        "top_campaigns": [{"campaign": r["campaign"], "sessions": r["sessions"], "eng_rate": round((r["eng_rate"] or 0) * 100, 1)} for r in tcamp],
        "top_pages": [{"page": r["page"], "sessions": r["sessions"], "engaged": r["engaged"], "avg_sec": round(r["avg_sec"] or 0, 0)} for r in pages],
        "events": [{"event": r["event_name"], "events": r["events"], "sessions": r["sessions"]} for r in events],
    }
    return ds


def _mmss(sec):
    sec = int(sec or 0)
    return f"{sec//60}:{sec%60:02d}"


if __name__ == "__main__":
    import sys, json
    host = sys.argv[1] if len(sys.argv) > 1 else "www.baptisthealthal.com"
    tf = sys.argv[2] if len(sys.argv) > 2 else "fiscal ytd"
    pg = sys.argv[3] if len(sys.argv) > 3 else None
    print(f"Building FULL traffic dataset for {host}  (timeframe: {tf}{', page '+pg if pg else ''}) ...")
    ds = build_dataset(host, timeframe=tf, page=pg, verbose=True)
    t = ds["totals"]; en = ds["engagement"]
    print(f"\n== {host} — {ds['window']['current']} vs {ds['window']['prior_matched']} ==")
    for k in ("sessions", "users", "page_views"):
        m = t[k]; print(f"  {k}: {m['current']:,} vs {m['prior']:,}  (YoY {m['yoy_pct']}%)")
    print(f"  engaged sessions: {en['engaged_sessions']['current']:,} (YoY {en['engaged_sessions']['yoy_pct']}%)")
    print(f"  engagement rate: {en['rate']['current']}% vs {en['rate']['prior']}%   avg time: {_mmss(en['avg_eng_sec']['current'])} vs {_mmss(en['avg_eng_sec']['prior'])}")
    print("  sessions by source:"); [print(f"    {r['channel']}: {r['sessions']:,}") for r in ds["by_channel"]]
    print(f"  paid share: {ds['paid']['share_current']}% (prior {ds['paid']['share_prior']}%)  paid sessions {ds['paid']['sessions']['current']:,} (YoY {ds['paid']['sessions']['yoy_pct']}%)")
    print("  campaign types:"); [print(f"    {r['type']}: {r['sessions']:,} ({r['eng_rate']}% eng)") for r in ds["campaign_types"]]
    print("  top campaigns:"); [print(f"    {r['campaign']}: {r['sessions']:,} ({r['eng_rate']}%)") for r in ds["top_campaigns"]]
    print("  top pages:"); [print(f"    {r['page']}: {r['sessions']:,} / eng {r['engaged']:,} / {_mmss(r['avg_sec'])}") for r in ds["top_pages"]]
    print("  key events:"); [print(f"    {r['event']}: {r['events']:,} events, {r['sessions']:,} sessions") for r in ds["events"]]
