"""
ktx_client
──────────
The ONLY module in Jarvis that talks to the ktx context layer.

ktx ("kaelio context layer") is installed locally and exposes a CLI:

    ktx sql -c <conn> --json "<SQL>"
        → execute parser-validated, read-only SQL  (Seam B: execution)

    ktx sl <query> --json --limit N
        → semantic search over indexed sources     (provenance for UI / briefs)

    ktx sl query --measure .. --dimension .. --filter .. --format sql
        → compile approved metrics to SQL           (Seam A: compile — Phase 2,
          requires measures to be seeded into ktx's semantic layer first)

Everything here returns results in the SAME shapes Jarvis already uses
(semantic.executor.QueryResult), so it drops in behind a feature flag
(see jarvis_engine.use_ktx) without changing any callers.

Config (env vars, all optional):
    KTX_BIN          path to the ktx binary   (default: auto-detect → /opt/homebrew/bin/ktx)
    KTX_PROJECT_DIR  ktx project directory     (default: ~/Jarvis/ktx-project)
    KTX_CONNECTION   ktx connection id         (default: bigquery-warehouse)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from semantic.executor import (
    QueryResult,
    run_query as _bq_run_query,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES_BILLED,
    DEFAULT_MAX_ROWS_RETURNED,
)

log = logging.getLogger("ktx_client")

_DEFAULT_PROJECT_DIR = Path.home() / "Jarvis" / "ktx-project"


# ─── Configuration ──────────────────────────────────────────────────
def _ktx_bin() -> str:
    explicit = os.environ.get("KTX_BIN")
    if explicit:
        return explicit
    found = shutil.which("ktx")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ktx", "/usr/local/bin/ktx"):
        if Path(cand).exists():
            return cand
    return "ktx"  # last resort — will raise a clear error if truly missing


def _project_dir() -> str:
    return os.environ.get("KTX_PROJECT_DIR", str(_DEFAULT_PROJECT_DIR))


def _connection() -> str:
    return os.environ.get("KTX_CONNECTION", "bigquery-warehouse")


# `ktx sql --max-rows` accepts an integer in [1, 10000]. Jarvis's internal
# DEFAULT_MAX_ROWS_RETURNED (100k) exceeds that, so we clamp what we pass to ktx.
KTX_SQL_MAX_ROWS_CAP = 10_000


class KtxError(Exception):
    """ktx CLI invocation failed or returned unparseable output."""


# ─── Low-level CLI runner ───────────────────────────────────────────
def _run_ktx(args: list[str], timeout: int) -> str:
    # Lockdown gate — refuse to spawn the binary at all when KAELIO_LOCKDOWN=1.
    # Defense in depth: jarvis_engine.use_ktx() should already prevent reaching
    # this code path, but a defensive check here guarantees no rogue caller can
    # bypass it. The binary's telemetry is independent of Jarvis's flow — by
    # refusing to spawn at all, we eliminate even the possibility of an event.
    try:
        from jarvis_engine import kaelio_lockdown
        if kaelio_lockdown():
            raise KtxError(
                "KAELIO_LOCKDOWN=1 — refusing to spawn the ktx binary. "
                "Unset KAELIO_LOCKDOWN to re-enable, or use JARVIS_SQL_ENGINE=bigquery to route around ktx."
            )
    except ImportError:
        pass

    cmd = [_ktx_bin(), "--project-dir", _project_dir(), *args]
    # Force telemetry-off env into the child process, just in case the global
    # opt-out file isn't present yet.
    child_env = dict(os.environ)
    child_env.setdefault("KTX_TELEMETRY_DISABLED", "1")
    child_env.setdefault("DO_NOT_TRACK", "1")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=child_env)
    except FileNotFoundError as e:
        raise KtxError(
            f"ktx binary not found at '{_ktx_bin()}'. Set the KTX_BIN env var. ({e})"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise KtxError(f"ktx timed out after {timeout}s running: {' '.join(args)}") from e
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise KtxError(f"ktx exited {proc.returncode}: {msg[:500]}")
    return proc.stdout


def _extract_json(stdout: str) -> Any:
    """ktx prints a daemon banner line (e.g. 'Using existing KTX daemon: ...')
    before the JSON payload. Locate and decode the first JSON value in the stream."""
    candidates = [i for i in (stdout.find("{"), stdout.find("[")) if i != -1]
    if not candidates:
        raise KtxError(f"no JSON found in ktx output: {stdout[:200]!r}")
    start = min(candidates)
    try:
        obj, _ = json.JSONDecoder().raw_decode(stdout[start:])
        return obj
    except json.JSONDecodeError as e:
        raise KtxError(f"could not parse ktx JSON: {e}; raw={stdout[start:start+200]!r}") from e


# ─── Seam B: execute read-only SQL through ktx ──────────────────────
def execute_sql(
    sql: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,  # accepted for signature parity; ktx enforces its own caps
    max_rows: int = DEFAULT_MAX_ROWS_RETURNED,
    dry_run: bool = False,
) -> QueryResult:
    """Execute SQL via `ktx sql` and return a Jarvis QueryResult.

    Signature matches semantic.executor.run_query so it's a drop-in replacement.
    `ktx sql` has no dry-run mode, so dry_run delegates to the BigQuery executor
    (keeps semantic.estimate_cost working unchanged).
    """
    if dry_run:
        return _bq_run_query(
            sql,
            timeout_seconds=timeout_seconds,
            max_bytes_billed=max_bytes_billed,
            max_rows=max_rows,
            dry_run=True,
        )

    # ktx caps --max-rows at 10000; clamp the value we pass while remembering
    # the caller's intent so we can flag truncation correctly.
    requested = max_rows if max_rows else KTX_SQL_MAX_ROWS_CAP
    ktx_max = min(requested, KTX_SQL_MAX_ROWS_CAP)

    start = time.monotonic()
    out = _run_ktx(
        ["sql", "-c", _connection(), "--json", "--max-rows", str(ktx_max), sql],
        timeout=timeout_seconds + 30,
    )
    data = _extract_json(out)

    columns = data.get("headers", []) or []
    rows = data.get("rows", []) or []
    row_count = int(data.get("rowCount", len(rows)))
    elapsed_ms = int((time.monotonic() - start) * 1000)

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=row_count,
        bytes_processed=int(data.get("bytesProcessed", 0) or 0),
        elapsed_ms=elapsed_ms,
        sql=sql,
        truncated=(len(rows) >= ktx_max),
    )


# ─── Provenance: semantic search over ktx sources ───────────────────
def search_context(query: str, limit: int = 5) -> list[dict]:
    """Search ktx's indexed semantic-layer sources. Returns a simplified list
    suitable for surfacing provenance (table name, description, match snippet,
    score) in dashboards and AI briefs."""
    out = _run_ktx(["sl", "--json", "--limit", str(limit), *query.split()], timeout=60)
    data = _extract_json(out)
    items = []
    if isinstance(data, dict):
        items = (data.get("data") or {}).get("items", []) or []
    results: list[dict] = []
    for it in items:
        results.append(
            {
                "name": it.get("name"),
                "description": it.get("description"),
                "path": it.get("path"),
                "score": it.get("score"),
                "snippet": it.get("snippet"),
                "columns": it.get("columnCount"),
                "joins": it.get("joinCount"),
                "measures": it.get("measureCount"),
            }
        )
    return results


# ─── Seam A (Phase 2): compile approved metrics to SQL ──────────────
def compile_semantic_query(
    *,
    measures: Optional[list[str]] = None,
    dimensions: Optional[list[str]] = None,
    filters: Optional[list[str]] = None,
    order_by: Optional[list[str]] = None,
    limit: Optional[int] = None,
    execute: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
):
    """Compile (or execute) a ktx semantic-layer query via `ktx sl query`.

    NOTE: requires measures to be seeded into ktx's semantic layer first
    (current ingest produced dimensions + joins but 0 measures). Until then,
    Jarvis keeps using semantic.build_sql for compilation.

    Returns the compiled SQL string when execute=False, or a QueryResult when
    execute=True.
    """
    args = ["sl", "query", "--connection-id", _connection()]
    for m in measures or []:
        args += ["--measure", m]
    for d in dimensions or []:
        args += ["--dimension", d]
    for f in filters or []:
        args += ["--filter", f]
    for o in order_by or []:
        args += ["--order-by", o]
    if limit:
        args += ["--limit", str(limit)]

    if execute:
        args += ["--execute", "--json", "--no-input"]
        out = _run_ktx(args, timeout=timeout_seconds + 30)
        data = _extract_json(out)
        columns = data.get("headers") or data.get("columns") or []
        rows = data.get("rows") or []
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=int(data.get("rowCount", len(rows))),
            bytes_processed=int(data.get("bytesProcessed", 0) or 0),
            elapsed_ms=0,
            sql=data.get("sql", ""),
        )

    args += ["--format", "sql", "--no-input"]
    out = _run_ktx(args, timeout=timeout_seconds)
    try:
        data = _extract_json(out)
        return data.get("sql") if isinstance(data, dict) else out.strip()
    except KtxError:
        return out.strip()  # --format sql may emit raw SQL rather than JSON


# ─── Seam A: compile a Jarvis StructuredQuery via ktx ───────────────
class NotMappable(Exception):
    """The structured query can't be fully expressed in ktx; caller should fall back to the BigQuery builder."""


_FIELD_MAP = None


def _field_map() -> dict:
    """Lazy-load the Jarvis->ktx field map (table keys -> ktx sources/measures/dimensions)."""
    global _FIELD_MAP
    if _FIELD_MAP is None:
        p = Path(__file__).parent / "jarvis_ktx_field_map.json"
        _FIELD_MAP = json.loads(p.read_text())
    return _FIELD_MAP


_TABLE_RE = re.compile(r"(oh-data-warehouse\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)")


def _backtick_tables(sql: str) -> str:
    """ktx emits `FROM oh-data-warehouse.gold.tbl` without backticks; BigQuery needs them
    because the project id is hyphenated. Wrap fully-qualified table refs in backticks."""
    return _TABLE_RE.sub(r"`\1`", sql)


def _lit(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def compile_structured_query(query, model=None) -> str:
    """Map a semantic.StructuredQuery to a `ktx sl query` and return compiled SQL
    (table refs backticked, ready for execute_sql). Raises NotMappable when any field
    can't be expressed in ktx (combined dimensions, metric filters, unknown fields),
    so the caller can fall back to the BigQuery builder."""
    fm = _field_map()
    srcmap, dimmap, metmap, colmap = fm["sources"], fm["dimensions"], fm["metrics"], fm.get("columns", {})
    combined = set(fm.get("combined_dimensions") or [])

    def src(tk: str) -> str:
        s = srcmap.get(tk)
        if not s:
            raise NotMappable(f"no ktx source for table '{tk}'")
        return s

    def col_for(tk: str, fieldkey: str) -> str:
        """Resolve a dimension/filter field to a ktx column: explicit dimension alias first,
        then a raw source column of the same name."""
        if fieldkey in combined:
            raise NotMappable(f"combined dimension '{fieldkey}' not expressible in ktx")
        explicit = (dimmap.get(tk, {}) or {}).get(fieldkey)
        if explicit:
            return explicit
        if fieldkey in (colmap.get(tk, []) or []):
            return fieldkey
        raise NotMappable(f"field {tk}.{fieldkey} not resolvable to a ktx column")

    measures, dimensions, filters, order = [], [], [], []

    for m in query.metrics:
        mm = metmap.get(m.table, {})
        if m.field not in mm:
            raise NotMappable(f"metric {m.table}.{m.field} not defined in ktx")
        measures.append(f"{src(m.table)}.{mm[m.field]}")

    for d in query.dimensions:
        dimensions.append(f"{src(d.table)}.{col_for(d.table, d.field)}")

    for f in query.filters:
        ref = f"{src(f.table)}.{col_for(f.table, f.field)}"
        op = f.op.lower()
        if op == "eq":
            filters.append(f"{ref} = {_lit(f.value)}")
        elif op == "neq":
            filters.append(f"{ref} != {_lit(f.value)}")
        elif op == "gte":
            filters.append(f"{ref} >= {_lit(f.value)}")
        elif op == "lte":
            filters.append(f"{ref} <= {_lit(f.value)}")
        elif op == "gt":
            filters.append(f"{ref} > {_lit(f.value)}")
        elif op == "lt":
            filters.append(f"{ref} < {_lit(f.value)}")
        elif op == "between":
            filters.append(f"{ref} >= {_lit(f.value[0])}")
            filters.append(f"{ref} <= {_lit(f.value[1])}")
        elif op == "in":
            filters.append(f"{ref} IN (" + ", ".join(_lit(v) for v in f.value) + ")")
        else:
            raise NotMappable(f"filter op '{op}' not supported via ktx")

    # order-by: map alias to its ktx name via the primary table's maps; skip if unknown
    pmet = metmap.get(query.primary_table, {})
    pdim = dimmap.get(query.primary_table, {})
    for o in query.order_by:
        nm = pmet.get(o.field) or pdim.get(o.field)
        if nm:
            order.append(f"{nm}:{o.direction.lower()}")

    args = ["sl", "query", "--connection-id", _connection()]
    for x in measures:
        args += ["--measure", x]
    for x in dimensions:
        args += ["--dimension", x]
    for x in filters:
        args += ["--filter", x]
    for x in order:
        args += ["--order-by", x]
    lim = getattr(query, "limit", None)
    if lim:
        args += ["--limit", str(lim)]
    args += ["--format", "sql", "--no-input"]

    out = _run_ktx(args, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        data = _extract_json(out)
        sql = data.get("sql") if isinstance(data, dict) else out.strip()
    except KtxError:
        sql = out.strip()
    if not sql:
        raise NotMappable("ktx returned empty SQL")
    sql = _backtick_tables(sql)
    # ktx aliases columns with its own measure names (e.g. total_clicks); rewrite them
    # back to the field keys the caller requested so results match the BigQuery engine.
    for m in query.metrics:
        ktxname = (metmap.get(m.table, {}) or {}).get(m.field)
        if ktxname and ktxname != m.field:
            sql = re.sub(rf"\bAS {re.escape(ktxname)}\b", f"AS {m.field}", sql)
    return sql


# ─── Health check ───────────────────────────────────────────────────
def healthcheck() -> dict:
    """Verify ktx is reachable and executing. Used by smoke tests / status."""
    r = execute_sql("SELECT 1 AS ok")
    return {
        "ok": bool(r.rows and r.rows[0] and r.rows[0][0] == 1),
        "columns": r.columns,
        "rows": r.rows,
        "elapsed_ms": r.elapsed_ms,
        "connection": _connection(),
        "project_dir": _project_dir(),
        "ktx_bin": _ktx_bin(),
    }
