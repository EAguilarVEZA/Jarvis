"""
traffic_report.py — deterministic 3-page website-traffic report (NO LLM).
Consumes the structured dataset from ga4_traffic.build_dataset() and renders the
exact Baptist/Orlando Health Corporate layout. Same fixed brand kit every time.
"""
import datetime

CSS = r""":root{
  --navy:#1f2b38; --navy2:#16212c; --ink:#26333c; --muted:#5f6b74;
  --orange:#e8641c; --peach:#fce3d4; --grey:#aab6bf; --line:#e4e8eb;
  --foot:#8aa0a8; --green:#2f7d34;
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{font-family:Arial,Helvetica,sans-serif;color:var(--ink)}
.page{width:816px;height:1056px;position:relative;background:#fff;overflow:hidden;page-break-after:always}
.page:last-child{page-break-after:auto}
.hdr{display:flex;height:60px}
.hdr .l{background:var(--navy);flex:0 0 60%;display:flex;align-items:center;padding-left:53px;color:#fff;letter-spacing:4px;font-weight:700;font-size:14px}
.hdr .r{background:var(--orange);flex:1;display:flex;align-items:center;justify-content:flex-end;padding-right:53px;color:#fff;letter-spacing:3px;font-weight:700;font-size:12px}
.body{padding:22px 53px 0}
.kick{color:var(--orange);font-size:9px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin:16px 0 7px}
.kick.top{margin-top:4px}
.subkick{color:var(--orange);font-size:10.5px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin:2px 0 9px}
h1{color:var(--navy);font-size:25px;line-height:1.14;font-weight:800;margin-bottom:8px}
.lead{font-size:11px;line-height:1.5;color:#41505a;margin-bottom:4px}
.lead b{color:var(--navy)}
.foot{position:absolute;bottom:26px;left:53px;right:53px;color:var(--foot);font-size:8px;letter-spacing:2px;font-weight:700;text-transform:uppercase;border-top:1px solid var(--line);padding-top:8px}
.src{font-size:8px;color:#8b959c;line-height:1.4;margin-top:6px}
.cap{font-size:8px;color:#8b959c;line-height:1.4;font-style:italic;margin-top:6px}

/* stat callout row */
.stats{display:flex;gap:10px;margin-bottom:4px}
.stat{flex:1;border-radius:3px;padding:12px 13px;min-height:66px}
.stat.navy{background:var(--navy);color:#fff}
.stat.peach{background:var(--peach)}
.stat .n{font-size:29px;font-weight:800;line-height:1;letter-spacing:-.5px}
.stat.navy .n{color:#fff}
.stat.peach .n{color:var(--orange)}
.stat .lab{font-size:8.5px;margin-top:6px;line-height:1.35;color:#3f4a52}
.stat.navy .lab{color:#cdd6dd}
.up{color:var(--orange);font-weight:700}

/* grouped yoy bars */
.yoy{display:flex;justify-content:space-around;border:1px solid var(--line);border-radius:4px;padding:16px 10px 8px;margin-top:4px}
.grp{flex:1;text-align:center}
.grp .bars{display:flex;align-items:flex-end;justify-content:center;gap:14px;height:130px}
.bar{width:46px;position:relative;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.bar .v{position:absolute;top:-15px;font-size:10px;font-weight:700;color:var(--navy);white-space:nowrap}
.bar .col{width:100%}
.col.g{background:var(--grey)}
.col.o{background:var(--orange)}
.grp .delta{font-size:10px;font-weight:700;color:var(--green);margin-bottom:2px}
.grp .xr{display:flex;justify-content:center;gap:14px;font-size:8.5px;color:var(--muted);margin-top:5px}
.grp .xr span{width:46px}
.grp .gname{font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--navy);margin-top:3px}

/* horizontal bars */
.hb{border:1px solid var(--line);border-radius:4px;padding:14px 16px 8px}
.hrow{display:flex;align-items:center;font-size:9.5px;margin-bottom:9px}
.hrow .lbl{width:130px;color:var(--navy);font-weight:600;flex:0 0 130px}
.hrow .track{flex:1;display:flex;align-items:center;gap:7px}
.hbar{height:15px;border-radius:2px}
.hbar.o{background:var(--orange)}
.hbar.n{background:var(--navy)}
.hrow .val{font-size:9px;font-weight:700;color:var(--navy)}
.anno{font-size:8px;color:var(--muted)}
.anno.g{color:var(--green);font-weight:700}
.anno.o{color:var(--orange);font-weight:700}

/* colored-top cards */
.cards3{display:flex;gap:12px;margin-top:2px}
.c3{flex:1;border:1px solid var(--line);border-top:4px solid var(--orange);border-radius:3px;padding:12px 13px}
.c3 .t{font-size:11px;font-weight:700;color:var(--navy);margin-bottom:3px}
.c3 .big{font-size:18px;font-weight:800;color:var(--orange);margin-bottom:5px}
.c3 .tx{font-size:9px;color:#4a565e;line-height:1.4}

/* generic two cards */
.two{display:flex;gap:14px;margin-top:2px}
.tc{flex:1;border:1px solid var(--line);border-radius:3px;padding:13px 14px}
.tc.o{border-top:4px solid var(--orange)}
.tc.n{border-top:4px solid var(--navy)}
.tc h4{font-size:10.5px;font-weight:700;letter-spacing:.5px;color:var(--navy);margin-bottom:8px}
.tc li{list-style:none;font-size:9px;line-height:1.45;color:#4a565e;margin-bottom:7px;padding-left:12px;position:relative}
.tc li:before{content:"›";position:absolute;left:0;color:var(--orange);font-weight:700}
.tc li b{color:var(--navy)}

/* callout */
.callout{background:#eef1f3;border-left:4px solid var(--orange);border-radius:2px;padding:11px 14px;margin:12px 0;font-size:9.5px;line-height:1.5;color:#3f4a52}
.callout b{color:var(--navy)}

/* table */
table{width:100%;border-collapse:collapse;font-size:9.5px;margin-top:2px}
th{background:var(--navy);color:#fff;text-align:left;padding:7px 12px;font-size:8.5px;letter-spacing:1px;font-weight:700}
th.r,td.r{text-align:right}
td{padding:7px 12px;border-bottom:1px solid var(--line);color:#3a464e}
tr:nth-child(even) td{background:#f5f7f8}
td.p{font-weight:600;color:var(--navy)}

/* bottom line band */
.bl{background:var(--navy);border-radius:4px;padding:15px 18px;margin-top:14px}
.bl .k{color:var(--orange);font-size:9px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:6px}
.bl p{color:#dfe5ea;font-size:9.5px;line-height:1.5}
.bl p b{color:#fff}

/* engagement bars card */
.eng{display:flex;gap:14px;margin-top:2px}
.engcard{flex:0 0 46%;border:1px solid var(--line);border-radius:4px;padding:14px 12px 8px}
.engbars{display:flex;align-items:flex-end;justify-content:center;gap:34px;height:150px}
.ebar{width:70px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;position:relative}
.ebar .v{position:absolute;top:-16px;font-size:12px;font-weight:800;color:var(--navy)}
.ebar .col{width:100%}
.engx{display:flex;justify-content:center;gap:34px;font-size:8.5px;color:var(--muted);margin-top:5px}
.engx span{width:70px;text-align:center}
.engrate{text-align:center;font-size:9px;font-weight:700;letter-spacing:1px;color:var(--navy);margin-top:3px}
.engright{flex:1;display:flex;flex-direction:column;gap:12px}
.engstat{background:var(--peach);border-radius:3px;padding:13px 14px}
.engstat .n{font-size:26px;font-weight:800;color:var(--orange);line-height:1}
.engstat .lab{font-size:8.5px;color:#4a3a30;margin-top:5px;line-height:1.35}

/* share stacked */
.share{border:1px solid var(--line);border-radius:4px;padding:14px 16px 8px}
.legend{display:flex;gap:18px;font-size:8.5px;color:var(--muted);margin-bottom:12px;align-items:center}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.srow{display:flex;align-items:center;margin-bottom:11px}
.srow .yr{width:44px;font-size:9px;font-weight:700;color:var(--navy)}
.sbar{flex:1;display:flex;height:22px;border-radius:2px;overflow:hidden;font-size:8.5px;color:#fff;font-weight:700}
.seg{display:flex;align-items:center;justify-content:center}
.seg.n{background:var(--navy)}.seg.o{background:var(--orange)}.seg.g{background:var(--grey);color:#4a565e}
"""


def _esc(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _fmt(n):
    try: return f"{int(round(n)):,}"
    except Exception: return str(n)

def _abbr(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if n >= 1e6: return f"{n/1e6:.2f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K"
    return f"{int(n)}"

def _delta(p):
    if p is None: return ""
    return f"▲ +{p}%" if p >= 0 else f"▼ {p}%"

def _mmss(sec):
    sec = int(round(sec or 0)); return f"{sec//60}:{sec%60:02d}"

def _yoy_group(name, prior, cur, yoy):
    ch = 120; ph = int(120 * prior / cur) if cur else 0
    return (f'<div class="grp"><div class="delta">{_delta(yoy)}</div><div class="bars">'
            f'<div class="bar"><span class="v">{_fmt(prior)}</span><div class="col g" style="height:{ph}px"></div></div>'
            f'<div class="bar"><span class="v">{_fmt(cur)}</span><div class="col o" style="height:{ch}px"></div></div>'
            f'</div><div class="xr"><span>PRIOR</span><span>CURRENT</span></div><div class="gname">{name}</div></div>')

def _hdr(entity, n=""):
    tag = "WEBSITE TRAFFIC&nbsp;&nbsp;SUMMARY" + (f" · {n}" if n else "")
    return f'<div class="hdr"><div class="l">{_esc(entity).upper()}</div><div class="r">{tag}</div></div>'

def _foot(entity):
    return f'<div class="foot">Confidential&nbsp;&nbsp;|&nbsp;&nbsp;{_esc(entity)}&nbsp;&nbsp;|&nbsp;&nbsp;Marketing Technology</div>'


def render(entity, ds):
    host = ds.get("host", ""); t = ds["totals"]; en = ds["engagement"]
    win = ds["window"]; mon = datetime.date.today().strftime("%B %Y")
    src_line = (f'Source: GA4 export (property {_esc(ds.get("property"))}) via BigQuery, host {_esc(host)}. '
                f'Windows: {_esc(win["current"])} vs matched prior {_esc(win["prior_matched"])}. Data as of {_esc(ds.get("as_of"))}.')

    def yospan(m): return f' <span class="up">{_delta(m["yoy_pct"])}</span>' if m.get("yoy_pct") is not None else ""

    # ---- PAGE 1 ----
    chans = ds.get("by_channel", []); mx = max((c["sessions"] for c in chans), default=1) or 1
    sbars = ""
    for i, c in enumerate(chans):
        cls = "o" if i < 3 else "n"; w = round(c["sessions"] / mx * 100, 1)
        yo = f'<span class="anno g">▲ {_delta(c.get("yoy_pct")).replace("▲ ","")}</span>' if c.get("yoy_pct") and c["yoy_pct"] > 150 else ""
        sbars += (f'<div class="hrow"><div class="lbl">{_esc(c["channel"])}</div><div class="track">'
                  f'<div class="hbar {cls}" style="width:{w}%"></div><span class="val">{_fmt(c["sessions"])}</span>{yo}</div></div>')
    movers = ds.get("movers", [])
    mcards = ""
    for m in movers:
        mcards += (f'<div class="c3"><div class="t">{_esc(m["channel"])}</div><div class="big">{_delta(m.get("yoy_pct"))}</div>'
                   f'<div class="tx">Grew from {_abbr(m["prior"])} to {_abbr(m["current"])} sessions year over year.</div></div>')
    lead1 = (f'For the fiscal year to date ({_esc(win["current"])}), {_esc(host)} recorded <b>{_fmt(t["sessions"]["current"])} sessions</b>'
             + (f' ({_delta(t["sessions"]["yoy_pct"])} YoY)' if t["sessions"]["yoy_pct"] is not None else "")
             + f' and <b>{_fmt(t["users"]["current"])} users</b>'
             + (f' ({_delta(t["users"]["yoy_pct"])})' if t["users"]["yoy_pct"] is not None else "") + ".")
    p1 = f'''<section class="page">{_hdr(entity)}<div class="body">
      <div class="kick top">Website Traffic&nbsp;&nbsp;|&nbsp;&nbsp;{_esc(host)}&nbsp;&nbsp;|&nbsp;&nbsp;{mon}</div>
      <h1>{_esc(entity)} Website Traffic &mdash; Year-Over-Year</h1>
      <p class="lead">{lead1}</p>
      <div class="kick">Traffic Growth &mdash; Like-for-Like YoY</div>
      <div class="stats">
        <div class="stat navy"><div class="n">{_fmt(t["sessions"]["current"])}</div><div class="lab">Sessions{yospan(t["sessions"])}</div></div>
        <div class="stat peach"><div class="n">{_fmt(t["users"]["current"])}</div><div class="lab">Users{yospan(t["users"])}</div></div>
        <div class="stat peach"><div class="n">{_abbr(t["page_views"]["current"])}</div><div class="lab">Page Views{yospan(t["page_views"])}</div></div>
        <div class="stat peach"><div class="n">{_fmt(en["engaged_sessions"]["current"])}</div><div class="lab">Engaged Sessions{yospan(en["engaged_sessions"])}</div></div>
      </div>
      <div class="kick">Year-Over-Year by Metric</div>
      <div class="yoy">{_yoy_group("SESSIONS", t["sessions"]["prior"], t["sessions"]["current"], t["sessions"]["yoy_pct"])}{_yoy_group("USERS", t["users"]["prior"], t["users"]["current"], t["users"]["yoy_pct"])}{_yoy_group("PAGE VIEWS", t["page_views"]["prior"], t["page_views"]["current"], t["page_views"]["yoy_pct"])}</div>
      <div class="cap">Matched fiscal-year windows, each pair self-scaled. Grey = prior year, orange = current. {src_line}</div>
      <div class="kick">Where the Traffic Comes From &mdash; Sessions by Source</div>
      <div class="hb">{sbars}<div class="cap">Session source (last non-direct), fiscal year to date. Orange = top three sources.</div></div>
      <div class="kick">What Drove the Jump</div>
      <div class="cards3">{mcards}</div>
      </div>{_foot(entity)}</section>'''

    # ---- PAGE 2 ----
    er_c = en["rate"]["current"]; er_p = en["rate"]["prior"]
    ebar_h_p = 130; ebar_h_c = int(130 * er_c / er_p) if er_p else 130
    pages = ds.get("top_pages", [])
    prows = "".join(
        f'<tr><td class="p">{_esc(p.get("page") or "/")}</td><td class="r">{_fmt(p["sessions"])}</td>'
        f'<td class="r">{_fmt(p["engaged"])}</td><td class="r">{_mmss(p["avg_sec"])}</td></tr>' for p in pages)
    events = ds.get("events", [])[:6]
    erows = "".join(f'<tr><td class="p">{_esc(ev["event"])}</td><td class="r">{_fmt(ev["events"])}</td><td class="r">{_fmt(ev["sessions"])}</td></tr>' for ev in events)
    callout2 = (f'<b>Read this the right way:</b> engaged sessions grew to <b>{_fmt(en["engaged_sessions"]["current"])}</b>'
                + (f' ({_delta(en["engaged_sessions"]["yoy_pct"])} YoY)' if en["engaged_sessions"]["yoy_pct"] is not None else "")
                + f'. The engaged-session rate is {er_c}% (vs {er_p}% prior) as top-of-funnel volume scaled.')
    p2 = f'''<section class="page">{_hdr(entity, "2")}<div class="body">
      <div class="kick top">Engagement, Behavior &amp; Measurement Notes</div>
      <div class="subkick">The Engagement Trade-Off &mdash; Volume Up, Quality-per-Visit</div>
      <div class="eng">
        <div class="engcard"><div class="engbars">
          <div class="ebar"><span class="v">{er_p}%</span><div class="col g" style="height:{ebar_h_p}px;background:var(--grey)"></div></div>
          <div class="ebar"><span class="v">{er_c}%</span><div class="col o" style="height:{ebar_h_c}px;background:var(--orange)"></div></div>
        </div><div class="engx"><span>PRIOR</span><span>CURRENT</span></div>
        <div class="engrate">ENGAGED SESSION RATE</div>
        <div class="cap">Engaged-session rate, matched fiscal-year windows.</div></div>
        <div class="engright">
          <div class="engstat"><div class="n">{_mmss(en["avg_eng_sec"]["current"])}</div><div class="lab">Avg. engagement time / session (prior {_mmss(en["avg_eng_sec"]["prior"])})</div></div>
          <div class="engstat"><div class="n">{_fmt(en["engaged_sessions"]["current"])}</div><div class="lab">Engaged sessions{(" (" + _delta(en["engaged_sessions"]["yoy_pct"]) + " YoY)") if en["engaged_sessions"]["yoy_pct"] is not None else ""}</div></div>
        </div>
      </div>
      <div class="callout">{callout2}</div>
      <div class="kick">Most-Visited Pages &mdash; FY YTD</div>
      <table><tr><th>PAGE</th><th class="r">SESSIONS</th><th class="r">ENGAGED</th><th class="r">AVG. TIME</th></tr>{prows}</table>
      <div class="kick">Key Events</div>
      <table><tr><th>EVENT</th><th class="r">EVENTS</th><th class="r">SESSIONS</th></tr>{erows}</table>
      <div class="src">{src_line}</div>
      </div>{_foot(entity)}</section>'''

    # ---- PAGE 3 ----
    pd = ds.get("paid", {}); sh = ds.get("share", {"current": {}, "prior": {}})
    def seg(shdict):
        o = shdict.get("organic", 0); p = shdict.get("paid", 0); r = shdict.get("referral", 0)
        return (f'<div class="seg n" style="width:{o}%">{o}%</div><div class="seg o" style="width:{p}%">{p}%</div>'
                f'<div class="seg g" style="width:{max(r,0)}%"></div>')
    ct = ds.get("campaign_types", []); cmx = max((c["sessions"] for c in ct), default=1) or 1
    cbars = ""
    for c in ct:
        w = round(c["sessions"] / cmx * 100, 1); good = c["eng_rate"] >= 50
        cls = "o" if "Recruitment" in c["type"] else "n"
        ann = f'<span class="anno {"g" if good else "o"}">~{c["eng_rate"]}% engaged{" ✓" if good else ""}</span>'
        cbars += (f'<div class="hrow"><div class="lbl">{_esc(c["type"])}</div><div class="track">'
                  f'<div class="hbar {cls}" style="width:{w}%"></div><span class="val">{_fmt(c["sessions"])}</span>{ann}</div></div>')
    tc = ds.get("top_campaigns", [])
    crows = "".join(f'<tr><td class="p">{_esc(c["campaign"])}</td><td class="r">{_fmt(c["sessions"])}</td><td class="r">{c["eng_rate"]}%</td></tr>' for c in tc)
    lead3 = (f'Paid sessions reached <b>{_fmt(pd.get("sessions",{}).get("current",0))}</b>'
             + (f' ({_delta(pd["sessions"]["yoy_pct"])} YoY)' if pd.get("sessions",{}).get("yoy_pct") is not None else "")
             + f' and now make up <b>{pd.get("share_current",0)}% of all traffic</b> (up from {pd.get("share_prior",0)}%). '
             + f'Recruitment campaigns account for much of the paid growth.')
    _py = pd.get("sessions", {}).get("yoy_pct")
    paid_yo = f' <span class="up">{_delta(_py)}</span>' if _py is not None else ""
    p3 = f'''<section class="page">{_hdr(entity, "3")}<div class="body">
      <div class="kick top">Paid Media Overview&nbsp;&nbsp;|&nbsp;&nbsp;Campaign-Level Detail</div>
      <h1>Paid Traffic &mdash; Recruitment vs. Patient Acquisition</h1>
      <p class="lead">{lead3}</p>
      <div class="kick">Paid at a Glance</div>
      <div class="stats">
        <div class="stat navy"><div class="n">{_fmt(pd.get("sessions",{}).get("current",0))}</div><div class="lab">Paid sessions{paid_yo}</div></div>
        <div class="stat peach"><div class="n">{pd.get("share_current",0)}%</div><div class="lab">of all traffic is paid (up from {pd.get("share_prior",0)}%)</div></div>
        <div class="stat peach"><div class="n">{_abbr(pd.get("recruitment_current",0))}</div><div class="lab">recruitment-campaign sessions</div></div>
        <div class="stat peach"><div class="n">{("+" + str(pd.get("recruitment_yoy_pct")) + "%") if pd.get("recruitment_yoy_pct") is not None else "&mdash;"}</div><div class="lab">recruitment sessions YoY</div></div>
      </div>
      <div class="kick">Paid vs Organic &mdash; Share of Traffic</div>
      <div class="share"><div class="legend"><span><i style="background:var(--navy)"></i>Organic</span><span><i style="background:var(--orange)"></i>Paid</span><span><i style="background:var(--grey)"></i>Referral / other</span></div>
        <div class="srow"><div class="yr">PRIOR</div><div class="sbar">{seg(sh.get("prior",{}))}</div></div>
        <div class="srow"><div class="yr">CURRENT</div><div class="sbar">{seg(sh.get("current",{}))}</div></div>
        <div class="cap">Session share by bucket, matched fiscal-year windows.</div></div>
      <div class="kick">Where Paid Sessions Go &mdash; by Campaign Type</div>
      <div class="hb">{cbars}<div class="cap">Campaign-tagged paid sessions grouped by type. Green ✓ = high-engagement.</div></div>
      <div class="kick">Top Individual Campaigns</div>
      <table><tr><th>CAMPAIGN</th><th class="r">SESSIONS</th><th class="r">ENGAGED RATE</th></tr>{crows}</table>
      <div class="src">{src_line}</div>
      </div>{_foot(entity)}</section>'''

    return f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{_esc(entity)} — Website Traffic</title><style>{CSS}</style></head><body>{p1}{p2}{p3}</body></html>'
