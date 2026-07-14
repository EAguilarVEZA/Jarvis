"""
compare_to_kaelio
─────────────────
A/B benchmark harness for Jarvis (KTX) vs Kaelio.

Drives Jarvis's `/api/semantic/ask` endpoint over a list of questions,
captures structured results, and writes a Markdown report. The Kaelio
side is left as fields in the report — fill them by running the same
question on https://oh-next.kaelio.com/ and pasting the headline /
table / time. (Kaelio's `/api/v2/knowledge` and chat endpoints require
auth tokens we can't lift programmatically.)

Usage:
    # Default question set (the original 8 benchmarks):
    python compare_to_kaelio.py

    # Custom question set from a file (one prompt per line):
    python compare_to_kaelio.py --questions my_prompts.txt

    # Different Jarvis host:
    python compare_to_kaelio.py --host https://localhost:8000 --insecure

Output:
    `compare_to_kaelio_<timestamp>.md` in the current directory, plus
    a JSON sidecar with the raw `/ask` responses.

The harness is intentionally Kaelio-agnostic — it doesn't talk to
Kaelio directly. The output report has structured columns you can
fill from Kaelio's UI (headline, table key, time, top rows) so the
A/B comparison is consistent across runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import ssl
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_QUESTIONS = [
    "Top campaigns by clicks last 90 days",
    "Lead source conversion analysis",
    "Spend by service line last 90 days",
    "CTR by channel last 90 days",
    "Top campaigns by leads last 90 days",
    "Pediatrics campaign performance last 90 days",
    "What was our total marketing revenue for fiscal year 2024?",
    "Cost per lead by service line last 90 days",
]


# ─── Jarvis-side runner ─────────────────────────────────────────────


def _ask_jarvis(host: str, prompt: str, insecure: bool = False, timeout: float = 60.0) -> dict:
    """Hit /api/semantic/ask and return structured result."""
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        host.rstrip("/") + "/api/semantic/ask",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl._create_unverified_context() if insecure else None
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            j = json.loads(raw)
            ok = bool(j.get("widget"))
            return {
                "ok": ok,
                "status": resp.status,
                "ms": int((time.time() - t0) * 1000),
                "table": (j.get("interpreted") or {}).get("primary_table"),
                "dimensions": [
                    f"{d.get('table')}.{d.get('field')}"
                    for d in (j.get("interpreted") or {}).get("dimensions", [])
                ],
                "metrics": [
                    f"{m.get('table')}.{m.get('field')}"
                    for m in (j.get("interpreted") or {}).get("metrics", [])
                ],
                "filters": [
                    {"field": f.get("table") + "." + f.get("field"),
                     "op": f.get("op"),
                     "value": f.get("value")}
                    for f in (j.get("interpreted") or {}).get("filters", [])
                ],
                "headline": (j.get("brief") or {}).get("headline"),
                "summary": (j.get("brief") or {}).get("summary"),
                "findings": (j.get("brief") or {}).get("key_findings") or [],
                "recommendation": (j.get("brief") or {}).get("recommendation"),
                "columns": j.get("columns") or [],
                "row_count": j.get("row_count"),
                "rows_top5": (j.get("rows") or [])[:5],
                "error": j.get("error"),
                "detail": j.get("detail"),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:400]
        return {
            "ok": False,
            "status": e.code,
            "ms": int((time.time() - t0) * 1000),
            "error": "HTTPError",
            "detail": body,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "ms": int((time.time() - t0) * 1000),
            "error": type(e).__name__,
            "detail": str(e),
        }


# ─── Report writer ──────────────────────────────────────────────────


def _fmt_rows(rows: list, columns: list, limit: int = 5) -> str:
    if not rows:
        return "_no rows_"
    out = []
    if columns:
        out.append("| " + " | ".join(str(c) for c in columns) + " |")
        out.append("|" + "|".join("---" for _ in columns) + "|")
    for r in rows[:limit]:
        out.append("| " + " | ".join(_fmt_cell(c) for c in r) + " |")
    return "\n".join(out)


def _fmt_cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        # Round large numbers nicely
        if abs(v) >= 1_000_000:
            return f"${v:,.0f}" if abs(v) >= 100_000 else f"{v:,.2f}"
        return f"{v:,.2f}"
    if isinstance(v, int) and abs(v) >= 1000:
        return f"{v:,}"
    return str(v)


def _write_report(results: list[dict], path: Path) -> None:
    lines = [
        f"# Jarvis (KTX) vs Kaelio — A/B Benchmark",
        f"",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Questions: {len(results)}",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| # | Question | Jarvis status | Jarvis table | Jarvis time | Kaelio status | Kaelio table | Kaelio time |",
        f"|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        j = r["jarvis"]
        lines.append(
            f"| {i} | {r['question'][:50]} | "
            f"{'✅' if j['ok'] else '❌ ' + str(j.get('status', '?'))} | "
            f"`{j.get('table') or '—'}` | "
            f"{j.get('ms', 0)/1000:.1f}s | "
            f"_(fill in)_ | _(fill in)_ | _(fill in)_ |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Per-question detail")
    lines.append("")

    for i, r in enumerate(results, 1):
        q = r["question"]
        j = r["jarvis"]
        lines += [
            f"### Q{i}. {q}",
            f"",
            f"**Jarvis (KTX)**",
            f"",
            f"- Status: {'✅ ok' if j['ok'] else '❌ ' + str(j.get('status'))}",
            f"- Time: {j.get('ms', 0)/1000:.2f}s",
            f"- Primary table: `{j.get('table') or '—'}`",
            f"- Dimensions: {', '.join('`'+d+'`' for d in j.get('dimensions') or []) or '—'}",
            f"- Metrics: {', '.join('`'+m+'`' for m in j.get('metrics') or []) or '—'}",
            f"- Row count: {j.get('row_count') if j.get('row_count') is not None else '—'}",
            f"",
        ]
        if j.get("headline"):
            lines += [f"**Headline:** {j['headline']}", ""]
        if j.get("summary"):
            lines += [f"**Summary:** {j['summary']}", ""]
        if j.get("findings"):
            lines += ["**Key findings:**", ""]
            lines += [f"- {f}" for f in j["findings"][:5]]
            lines += [""]
        if j.get("recommendation"):
            lines += [f"**Recommendation:** {j['recommendation']}", ""]
        if j.get("rows_top5"):
            lines += ["**Top 5 rows:**", "", _fmt_rows(j.get("rows_top5") or [], j.get("columns") or []), ""]
        if j.get("error"):
            lines += [f"**Error:** `{j['error']}` — {j.get('detail') or ''}", ""]

        lines += [
            f"**Kaelio** _(fill in from Kaelio UI)_",
            f"",
            f"- Time: _(e.g. 73s)_",
            f"- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_",
            f"- Headline: _",
            f"- Top 3 rows: _",
            f"- Notable differences: _",
            f"",
            f"**A/B verdict**",
            f"",
            f"- Same table? _yes/no_",
            f"- Same magnitude? _yes/no_",
            f"- Same categorization / scope? _yes/no_",
            f"- Brief richness gap: _",
            f"",
            f"---",
            f"",
        ]
    path.write_text("\n".join(lines))


# ─── CLI ─────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description="Jarvis vs Kaelio A/B benchmark harness")
    p.add_argument("--host", default="https://localhost:8000",
                   help="Jarvis backend host (default: https://localhost:8000)")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS verification (needed for self-signed localhost cert)")
    p.add_argument("--questions", type=Path, default=None,
                   help="Path to file with one question per line. Default: the 8 OH benchmark questions.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output Markdown report path (default: compare_to_kaelio_<ts>.md)")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="Per-question timeout in seconds (default 60)")
    args = p.parse_args()

    questions = (args.questions.read_text().splitlines() if args.questions else DEFAULT_QUESTIONS)
    questions = [q.strip() for q in questions if q.strip() and not q.strip().startswith("#")]

    print(f"→ Running {len(questions)} questions against {args.host}", file=sys.stderr)
    results = []
    for i, q in enumerate(questions, 1):
        print(f"  [{i}/{len(questions)}] {q[:60]}... ", end="", flush=True, file=sys.stderr)
        j = _ask_jarvis(args.host, q, insecure=args.insecure, timeout=args.timeout)
        results.append({"question": q, "jarvis": j})
        if j["ok"]:
            print(f"✓ {j['ms']/1000:.1f}s `{j.get('table')}`", file=sys.stderr)
        else:
            print(f"✗ {j.get('error') or 'fail'}", file=sys.stderr)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_md = args.out or Path(f"compare_to_kaelio_{ts}.md")
    out_json = out_md.with_suffix(".json")

    _write_report(results, out_md)
    out_json.write_text(json.dumps(results, indent=2, default=str))

    print(f"\n→ Report:  {out_md}", file=sys.stderr)
    print(f"→ Raw:     {out_json}", file=sys.stderr)
    print(f"\nNext step: paste Kaelio's answer into each Q's 'Kaelio _(fill in)_' block.", file=sys.stderr)


if __name__ == "__main__":
    main()
