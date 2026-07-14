"""
HTML dashboard generator for service-line reports.

Single self-contained HTML file (no external CSS/JS hosting) so Jarvis can
write it to disk and open it in the browser the same way the OHNI reports
work today. Uses Chart.js from a CDN for the time-series visualization.
"""

from __future__ import annotations

import html
import json
from datetime import date

from .queries import (
    Reports,
    Period,
    Summary,
    CampaignBreakdown,
    TimeSeries,
)
from .catalog import resolve, ServiceLine


def build_dashboard(
    reports: Reports,
    service_line: str | ServiceLine,
    period: Period | None = None,
) -> str:
    """Run the full report query set and return a complete HTML document."""
    sl = service_line if isinstance(service_line, ServiceLine) else resolve(service_line)
    if not sl:
        raise ValueError(f"unknown service line: {service_line}")
    period = period or Period.last_n_days(30)

    comparison = reports.summary_with_comparison(sl, period)
    campaigns = reports.top_campaigns(sl, period, limit=15, order_by="cost")
    timeseries = reports.daily_timeseries(sl, Period.last_n_days(90))
    sources = reports.lead_source_mix(sl, period)

    return _render(sl, period, comparison, campaigns, timeseries, sources)


# -------- rendering --------

def _money(n: float | None) -> str:
    if n is None:
        return "—"
    return f"${n:,.0f}"


def _pct(n: float | None) -> str:
    if n is None:
        return "—"
    arrow = "▲" if n >= 0 else "▼"
    color_class = "delta-up" if n >= 0 else "delta-down"
    return f'<span class="{color_class}">{arrow} {abs(n):.1f}%</span>'


def _num(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _render(
    sl: ServiceLine,
    period: Period,
    comparison: dict,
    campaigns: CampaignBreakdown,
    timeseries: TimeSeries,
    sources: list[dict],
) -> str:
    cur = comparison["current"]
    deltas = comparison["deltas"]

    # Chart.js needs JSON arrays
    chart_data = {
        "labels": [str(p.date) for p in timeseries.points],
        "leads": [p.leads for p in timeseries.points],
        "cost": [round(p.cost, 2) for p in timeseries.points],
    }

    campaign_rows = "\n".join(
        f"""
        <tr>
          <td class="campaign-name">{html.escape(c.campaign_name or '—')}</td>
          <td>{html.escape(c.ad_source or '—')}</td>
          <td class="num">{_num(c.impressions)}</td>
          <td class="num">{_num(c.clicks)}</td>
          <td class="num">{_money(c.cost)}</td>
          <td class="num">{_num(c.leads)}</td>
          <td class="num">{_money(c.cpa)}</td>
        </tr>
        """
        for c in campaigns.campaigns
    )

    source_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(str(s.get('source', '—')))}</td>
          <td>{html.escape(str(s.get('medium', '—')))}</td>
          <td class="num">{_num(s.get('leads', 0))}</td>
          <td>{'Paid' if s.get('is_paid') else 'Organic'}</td>
        </tr>
        """
        for s in sources[:20]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(sl.label)} Marketing Report</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #0d1117;
      --surface: #161b22;
      --border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --good: #3fb950;
      --bad: #f85149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px; }}
    header {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
    h1 {{ margin: 0; font-size: 28px; font-weight: 600; }}
    .period {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .kpi {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
    }}
    .kpi .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .kpi .value {{ font-size: 32px; font-weight: 600; margin: 8px 0 4px; }}
    .kpi .delta {{ font-size: 13px; }}
    .delta-up {{ color: var(--good); }}
    .delta-down {{ color: var(--bad); }}
    section {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    h2 {{ margin: 0 0 16px; font-size: 18px; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }}
    th {{ color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.campaign-name {{ max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    canvas {{ width: 100% !important; height: 320px !important; }}
    footer {{ color: var(--muted); font-size: 12px; text-align: center; margin-top: 32px; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>{html.escape(sl.label)} Marketing Report</h1>
      <div class="period">{period.start.strftime('%B %d, %Y')} – {period.end.strftime('%B %d, %Y')}
        &nbsp;·&nbsp; {period.days} days</div>
    </header>

    <div class="kpis">
      <div class="kpi">
        <div class="label">Total Leads</div>
        <div class="value">{_num(cur['leads'])}</div>
        <div class="delta">vs prior period: {_pct(deltas['leads_pct'])}</div>
      </div>
      <div class="kpi">
        <div class="label">Paid Leads</div>
        <div class="value">{_num(cur['paid_leads'])}</div>
        <div class="delta">{_num(cur['organic_leads'])} organic</div>
      </div>
      <div class="kpi">
        <div class="label">Spend</div>
        <div class="value">{_money(cur['cost'])}</div>
        <div class="delta">vs prior period: {_pct(deltas['cost_pct'])}</div>
      </div>
      <div class="kpi">
        <div class="label">CPA</div>
        <div class="value">{_money(cur['cpa'])}</div>
        <div class="delta">CTR {cur['ctr'] or '—'}% · CPC {_money(cur['cpc'])}</div>
      </div>
    </div>

    <section>
      <h2>Daily trend (last 90 days)</h2>
      <canvas id="trendChart"></canvas>
    </section>

    <section>
      <h2>Top campaigns by spend</h2>
      <table>
        <thead>
          <tr>
            <th>Campaign</th><th>Source</th>
            <th style="text-align:right">Impressions</th>
            <th style="text-align:right">Clicks</th>
            <th style="text-align:right">Spend</th>
            <th style="text-align:right">Leads</th>
            <th style="text-align:right">CPA</th>
          </tr>
        </thead>
        <tbody>{campaign_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Lead sources</h2>
      <table>
        <thead><tr><th>Source</th><th>Medium</th>
          <th style="text-align:right">Leads</th><th>Type</th></tr></thead>
        <tbody>{source_rows}</tbody>
      </table>
    </section>

    <footer>Generated {date.today().strftime('%B %d, %Y')} from oh-data-warehouse · Jarvis Reporting</footer>
  </div>

  <script>
    const data = {json.dumps(chart_data)};
    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{
        labels: data.labels,
        datasets: [
          {{
            label: 'Leads',
            data: data.leads,
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.1)',
            yAxisID: 'y',
            tension: 0.3,
          }},
          {{
            label: 'Spend ($)',
            data: data.cost,
            borderColor: '#f85149',
            backgroundColor: 'rgba(248,81,73,0.05)',
            yAxisID: 'y1',
            tension: 0.3,
          }},
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: '#e6edf3' }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
          y:  {{ position: 'left',  ticks: {{ color: '#58a6ff' }}, grid: {{ color: '#21262d' }} }},
          y1: {{ position: 'right', ticks: {{ color: '#f85149' }}, grid: {{ display: false }} }},
        }}
      }}
    }});
  </script>
</body>
</html>
"""
