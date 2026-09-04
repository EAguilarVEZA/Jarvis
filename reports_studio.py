"""
reports_studio
──────────────
In-app "Board Reports": turn confirmed figures into a polished, on-brand
Orlando Health / Baptist Health PDF (the orlando-health-reports skill, brought
inside Martin). Martin (Sonnet) lays out the report using a FIXED brand CSS kit
(so it's always on-brand and never invents design), then we render a print-ready
PDF with headless Chromium — the same Playwright path the app already uses.

Data integrity: only figures the user supplied/confirmed are used. Nothing is
invented or back-filled; every report carries its source + as-of line.
"""
from __future__ import annotations

import os
import re
import json
import time
import datetime
import logging

log = logging.getLogger("reports_studio")

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# The brand CSS kit (Corporate / Baptist variant) — verbatim tokens, never improvised.
SHELL_CSS = """
:root{--navy:#1f2b38;--navy2:#16212c;--ink:#26333c;--muted:#5f6b74;--orange:#e8641c;
--peach:#fce3d4;--grey:#aab6bf;--line:#e4e8eb;--foot:#8aa0a8;--green:#2f7d34;}
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
h1{color:var(--navy);font-size:25px;line-height:1.14;font-weight:800;margin-bottom:8px}
.lead{font-size:11px;line-height:1.5;color:#41505a;margin-bottom:4px}
.lead b{color:var(--navy)}
.foot{position:absolute;bottom:26px;left:53px;right:53px;color:var(--foot);font-size:8px;letter-spacing:2px;font-weight:700;text-transform:uppercase;border-top:1px solid var(--line);padding-top:8px}
.src{font-size:8px;color:#8b959c;line-height:1.4;margin-top:6px}
.cap{font-size:8px;color:#8b959c;line-height:1.4;font-style:italic;margin-top:6px}
.stats{display:flex;gap:10px;margin-bottom:4px}
.stat{flex:1;border-radius:3px;padding:12px 13px;min-height:66px}
.stat.navy{background:var(--navy);color:#fff}.stat.peach{background:var(--peach)}
.stat .n{font-size:29px;font-weight:800;line-height:1;letter-spacing:-.5px}
.stat.navy .n{color:#fff}.stat.peach .n{color:var(--orange)}
.stat .lab{font-size:8.5px;margin-top:6px;line-height:1.35;color:#3f4a52}
.stat.navy .lab{color:#cdd6dd}.up{color:var(--orange);font-weight:700}
.hb{border:1px solid var(--line);border-radius:4px;padding:14px 16px 8px}
.hrow{display:flex;align-items:center;font-size:9.5px;margin-bottom:9px}
.hrow .lbl{width:150px;color:var(--navy);font-weight:600;flex:0 0 150px}
.hrow .track{flex:1;display:flex;align-items:center;gap:7px}
.hbar{height:15px;border-radius:2px}.hbar.o{background:var(--orange)}.hbar.n{background:var(--navy)}
.hrow .val{font-size:9px;font-weight:700;color:var(--navy)}
.anno{font-size:8px;color:var(--muted)}.anno.g{color:var(--green);font-weight:700}.anno.o{color:var(--orange);font-weight:700}
.cards3{display:flex;gap:12px;margin-top:2px}
.c3{flex:1;border:1px solid var(--line);border-top:4px solid var(--orange);border-radius:3px;padding:12px 13px}
.c3 .t{font-size:11px;font-weight:700;color:var(--navy);margin-bottom:3px}
.c3 .big{font-size:18px;font-weight:800;color:var(--orange);margin-bottom:5px}
.c3 .tx{font-size:9px;color:#4a565e;line-height:1.4}
.callout{background:#eef1f3;border-left:4px solid var(--orange);border-radius:2px;padding:11px 14px;margin:12px 0;font-size:9.5px;line-height:1.5;color:#3f4a52}
.callout b{color:var(--navy)}
table{width:100%;border-collapse:collapse;font-size:9.5px;margin-top:2px}
th{background:var(--navy);color:#fff;text-align:left;padding:7px 12px;font-size:8.5px;letter-spacing:1px;font-weight:700}
th.r,td.r{text-align:right}
td{padding:7px 12px;border-bottom:1px solid var(--line);color:#3a464e}
tr:nth-child(even) td{background:#f5f7f8}
td.p{font-weight:600;color:var(--navy)}
.bl{background:var(--navy);border-radius:4px;padding:15px 18px;margin-top:14px}
.bl .k{color:var(--orange);font-size:9px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:6px}
.bl p{color:#dfe5ea;font-size:9.5px;line-height:1.5}.bl p b{color:#fff}
"""

COMPONENT_GUIDE = """You lay out an on-brand executive report using ONLY these prebuilt CSS classes (do not invent styles, colors, or fonts):

PER PAGE, wrap content in:
<section class="page">
  <div class="hdr"><div class="l">{ENTITY_CAPS_SPACED}</div><div class="r">{REPORT NAME}&nbsp;&nbsp;SUMMARY[ · N]</div></div>
  <div class="body"> ...content... </div>
  <div class="foot">Confidential&nbsp;&nbsp;|&nbsp;&nbsp;{ENTITY}&nbsp;&nbsp;|&nbsp;&nbsp;Marketing Technology</div>
</section>

Content blocks (use as needed):
- Kicker label: <div class="kick top">Kicker | context | Month Year</div>  (later kickers: class="kick")
- Title: <h1>…</h1>   Lead paragraph: <p class="lead">… <b>bold key facts</b> …</p>
- Stat callout row (2-4): <div class="stats"><div class="stat navy"><div class="n">649,191</div><div class="lab">Label <span class="up">▲ +89%</span></div></div><div class="stat peach"><div class="n">477,125</div><div class="lab">Label</div></div>…</div>  (first card navy, rest peach)
- Horizontal bar chart: <div class="hb"><div class="hrow"><div class="lbl">Direct</div><div class="track"><div class="hbar o" style="width:100%"></div><span class="val">227,027</span></div></div>…<div class="cap">caption</div></div>  (orange bars = top items, navy = rest; width % scaled to the largest value)
- Colored-top cards (3): <div class="cards3"><div class="c3"><div class="t">Title</div><div class="big">+291%</div><div class="tx">text</div></div>…</div>
- Callout: <div class="callout"><b>Read this the right way:</b> …</div>
- Table: <table><tr><th>PAGE</th><th class="r">SESSIONS</th></tr><tr><td class="p">/careers</td><td class="r">199,377</td></tr>…</table>
- Bottom line band: <div class="bl"><div class="k">The Bottom Line</div><p>… <b>bold</b> …</p></div>
- Source line: <div class="src">Source: … Data as of …</div>   Caption under a chart: <div class="cap">…</div>

RULES: Use ONLY the numbers given below — never invent, estimate, or back-fill. If a value is missing, leave a labeled gap. Keep every caveat. Put a source + as-of line on each page. Voice: measured, credible, no hype. Lead with a chart wherever the point is a trend or comparison."""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "report").lower()).strip("-")[:48] or "report"


def load_skill() -> str:
    """Load the orlando-health-reports skill (and any reference files) from the
    Obsidian vault so reports follow the user's own written rules — update the
    skill, update the app's behavior, no code change. The fixed SHELL_CSS still
    guarantees the visual format matches the attached sample report."""
    try:
        import martin_core
        d = martin_core._vault() / "02-Knowledge" / "Reporting" / "orlando-health-reports"
        parts = []
        sk = d / "SKILL.md"
        if sk.exists():
            parts.append(sk.read_text(encoding="utf-8")[:6000])
        refs = d / "references"
        if refs.exists():
            for f in sorted(refs.glob("*.md")):
                parts.append("### reference: " + f.stem + "\n" + f.read_text(encoding="utf-8")[:2500])
        return "\n\n".join(parts)
    except Exception:
        return ""


async def generate_report(entity: str, template: str, brief: str, data: str,
                          source: str = "", asof: str = "") -> dict:
    """Ask the active model to lay out the branded report body from confirmed data.
    Returns {ok, html, id, path} — path is the rendered PDF, or html-only if render unavailable."""
    import system_llm, llm_router
    entity = entity or "Orlando Health"
    system_llm.ensure_seed()
    prov = system_llm.resolve(
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "") or None,
        anthropic_model=os.getenv("JARVIS_MARTIN_MODEL", "claude-sonnet-5"), name="Claude")
    if not prov:
        return {"ok": False, "error": "No model available."}
    skill = load_skill()
    skill_block = ("\n\n## Your report skill (from the vault — follow it)\n" + skill) if skill else ""
    sysmsg = (f"You are Martin, building a print-ready {entity} executive report (Corporate template, portrait), "
              "in the SAME format as the reference Baptist Health Website Traffic Summary. "
              f"{COMPONENT_GUIDE}{skill_block}\n\nENTITY = {entity}. Match the reference report's structure: a navy/orange "
              "header bar per page, an orange kicker line, a bold navy title, a lead paragraph, a stat-callout row (first card "
              "navy, rest peach), at least one chart where the point is a trend or comparison, and a navy 'The Bottom Line' band. "
              "Output ONLY the full sequence of <section class=\"page\"> blocks — no <html>, <head>, <style>, or commentary. "
              "Portrait Letter pages (816x1056). Keep each page from overflowing.")
    user = (f"## Report\n{brief}\n\n## Confirmed data (use ONLY these figures)\n{data}\n\n"
            f"## Source / as-of\n{source or 'Internal MarTech'}"
            + (f" — data as of {asof}" if asof else "")
            + "\n\nBuild the report now.")
    try:
        r = await llm_router.complete(prov, sysmsg, [{"role": "user", "content": user}], max_tokens=4000)
        if r.get("error"):
            return {"ok": False, "error": r["error"]}
        body = (r.get("output") or "").strip()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if "<section" not in body:
        return {"ok": False, "error": "The model did not return report pages."}
    html = (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{entity} — {brief}</title>"
            f"<style>{SHELL_CSS}</style></head><body>{body}</body></html>")
    rid = f"{int(time.time())}-{_slug(entity + '-' + brief)}"
    html_path = os.path.join(REPORTS_DIR, rid + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    pdf_path = await _render_pdf(html, rid)
    return {"ok": True, "id": rid, "html_path": html_path, "pdf_path": pdf_path,
            "has_pdf": bool(pdf_path)}


def _esc(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _delta(p):
    if p is None:
        return ""
    return f"▲ +{p}%" if p >= 0 else f"▼ {p}%"


def render_traffic(entity: str, ds: dict) -> str:
    """Deterministically render a website-traffic report from the structured GA4
    dataset — NO LLM. Same fixed brand kit, so output is identical every time."""
    host = ds.get("host", "")
    t = ds["totals"]
    s, u, pv = t["sessions"]["current"], t["users"]["current"], t["page_views"]["current"]
    src = ds.get("sessions_by_source", [])
    pages = ds.get("top_pages", [])
    mx = max((x["sessions"] for x in src), default=1) or 1
    mon = datetime.date.today().strftime("%B %Y")

    bars = ""
    for i, x in enumerate(src):
        cls = "o" if i < 3 else "n"
        w = round(x["sessions"] / mx * 100, 1)
        bars += (f'<div class="hrow"><div class="lbl">{_esc(x["source"])}</div>'
                 f'<div class="track"><div class="hbar {cls}" style="width:{w}%"></div>'
                 f'<span class="val">{_fmt(x["sessions"])}</span></div></div>')
    rows = ""
    for p in pages:
        rows += f'<tr><td class="p">{_esc(p.get("page") or "/")}</td><td class="r">{_fmt(p["sessions"])}</td></tr>'

    def yo(metric):
        return f' <span class="up">{_delta(metric["yoy_pct"])}</span>' if metric["yoy_pct"] is not None else ""

    lead = (f'For the current fiscal year to date ({_esc(ds["window"]["current"])}), {_esc(host)} recorded '
            f'<b>{_fmt(s)} sessions</b>'
            + (f' ({_delta(t["sessions"]["yoy_pct"])} year over year)' if t["sessions"]["yoy_pct"] is not None else "")
            + f' and <b>{_fmt(u)} users</b>'
            + (f' ({_delta(t["users"]["yoy_pct"])})' if t["users"]["yoy_pct"] is not None else "") + ".")
    top_src = (f'<b>{_esc(src[0]["source"])}</b> with {_fmt(src[0]["sessions"])} sessions') if src else "n/a"
    bl = (f'{_esc(host)} is at <b>{_fmt(s)} sessions</b> fiscal year to date'
          + (f', {_delta(t["sessions"]["yoy_pct"])} year over year' if t["sessions"]["yoy_pct"] is not None else "")
          + f'. The largest source is {top_src}.')

    body = f'''<section class="page">
  <div class="hdr"><div class="l">{_esc(entity).upper()}</div><div class="r">WEBSITE TRAFFIC&nbsp;&nbsp;SUMMARY</div></div>
  <div class="body">
    <div class="kick top">Website Traffic&nbsp;&nbsp;|&nbsp;&nbsp;{_esc(host)}&nbsp;&nbsp;|&nbsp;&nbsp;{mon}</div>
    <h1>{_esc(entity)} Website Traffic &mdash; Year-Over-Year</h1>
    <p class="lead">{lead}</p>
    <div class="kick">Traffic at a Glance &mdash; Fiscal Year to Date</div>
    <div class="stats">
      <div class="stat navy"><div class="n">{_fmt(s)}</div><div class="lab">Sessions{yo(t["sessions"])}</div></div>
      <div class="stat peach"><div class="n">{_fmt(u)}</div><div class="lab">Users{yo(t["users"])}</div></div>
      <div class="stat peach"><div class="n">{_fmt(pv)}</div><div class="lab">Page Views{yo(t["page_views"])}</div></div>
    </div>
    <div class="kick">Where the Traffic Comes From &mdash; Sessions by Source</div>
    <div class="hb">{bars}<div class="cap">Sessions by session source, fiscal year to date. Orange = top three sources.</div></div>
    <div class="kick">Most-Visited Pages</div>
    <table><tr><th>PAGE</th><th class="r">SESSIONS</th></tr>{rows}</table>
    <div class="bl"><div class="k">The Bottom Line</div><p>{bl}</p></div>
    <div class="src">Source: GA4 export (property {_esc(ds.get("property"))}) via BigQuery, host {_esc(host)}. Windows: {_esc(ds["window"]["current"])} vs matched prior {_esc(ds["window"]["prior_matched"])}. Data as of {_esc(ds.get("as_of"))}.</div>
  </div>
  <div class="foot">Confidential&nbsp;&nbsp;|&nbsp;&nbsp;{_esc(entity)}&nbsp;&nbsp;|&nbsp;&nbsp;Marketing Technology</div>
</section>'''
    return f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{_esc(entity)} — Website Traffic</title><style>{SHELL_CSS}</style></head><body>{body}</body></html>'


async def build_traffic_report(entity: str, brief: str, ds: dict) -> dict:
    """Structured GA4 dataset -> full 3-page branded HTML (no LLM) -> print-ready PDF."""
    import traffic_report
    html = traffic_report.render(entity, ds)
    rid = f"{int(time.time())}-{_slug(entity + '-' + (brief or 'website-traffic'))}"
    html_path = os.path.join(REPORTS_DIR, rid + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    pdf_path = await _render_pdf(html, rid)
    return {"ok": True, "id": rid, "html_path": html_path, "pdf_path": pdf_path, "has_pdf": bool(pdf_path)}


async def _render_pdf(html: str, rid: str):
    """Render the report HTML to a print-ready PDF via headless Chromium (same path
    as report_email). Returns the PDF path, or None if Chromium is unavailable."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log.warning("playwright not importable: %s", e)
        return None
    pdf_path = os.path.join(REPORTS_DIR, rid + ".pdf")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            await page.pdf(path=pdf_path, width="816px", height="1056px",
                           print_background=True,
                           margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            await browser.close()
        return pdf_path
    except Exception as e:
        log.warning("board report PDF render failed: %s", e)
        return None
