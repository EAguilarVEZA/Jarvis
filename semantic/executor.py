"""
semantic.executor
─────────────────
Executes BigQuery SQL and returns structured results.

Wraps google-cloud-bigquery with:
  • Connection caching (one client instance reused)
  • Credential loading from ~/.config/jarvis/bigquery-key.json
  • Query timeout protection
  • Result formatting (rows + metadata) for the workspace
  • Defensive error handling that surfaces useful messages

The executor is the bridge between the structured query layer and BigQuery.
Higher-level code calls executor.run_query(sql, model) and gets back a
QueryResult with rows it can render.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
    BIGQUERY_AVAILABLE = True
except ImportError:
    BIGQUERY_AVAILABLE = False


# ─── Paths and config ───────────────────────────────────────────────
_DEFAULT_CREDS_PATH = Path.home() / ".config" / "jarvis" / "bigquery-key.json"
_CREDS_PATH = Path(os.environ.get("JARVIS_BQ_CREDENTIALS", _DEFAULT_CREDS_PATH))

# Hard limits to protect the warehouse
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_BYTES_BILLED = 10 * 1024 * 1024 * 1024   # 10 GB cap
DEFAULT_MAX_ROWS_RETURNED = 100_000


# ─── Exceptions ────────────────────────────────────────────────────
class ExecutorError(Exception):
    """Base for executor errors."""
    pass


class ExecutorConfigError(ExecutorError):
    """Misconfiguration (missing creds, missing library, etc.)."""
    pass


class QueryExecutionError(ExecutorError):
    """The query ran but failed."""
    def __init__(self, message: str, sql: Optional[str] = None):
        super().__init__(message)
        self.sql = sql


# ─── Result types ──────────────────────────────────────────────────
@dataclass
class QueryResult:
    """The structured result returned to callers."""
    columns: list[str]                          # field aliases in column order
    rows: list[list[Any]]                       # each row is a list of values
    row_count: int
    bytes_processed: int
    elapsed_ms: int
    sql: str
    truncated: bool = False                     # True if we hit MAX_ROWS_RETURNED

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "bytes_processed": self.bytes_processed,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
        }


# ─── Client management (singleton) ─────────────────────────────────
_client_lock = threading.Lock()
_client: Optional["bigquery.Client"] = None


def _get_client() -> "bigquery.Client":
    """Get or create the BigQuery client. Thread-safe singleton.

    Reads credentials from ~/.config/jarvis/bigquery-key.json by default.
    Override with JARVIS_BQ_CREDENTIALS env var.
    """
    global _client

    if not BIGQUERY_AVAILABLE:
        raise ExecutorConfigError(
            "google-cloud-bigquery is not installed. "
            "Run: pip install google-cloud-bigquery"
        )

    with _client_lock:
        if _client is not None:
            return _client

        if not _CREDS_PATH.exists():
            raise ExecutorConfigError(
                f"BigQuery credentials not found at {_CREDS_PATH}. "
                f"Set JARVIS_BQ_CREDENTIALS env var or place a service account JSON at the default path."
            )

        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(_CREDS_PATH),
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _client = bigquery.Client(
                credentials=credentials,
                project=credentials.project_id,
            )
        except Exception as e:
            raise ExecutorConfigError(
                f"Failed to initialize BigQuery client from {_CREDS_PATH}: {e}"
            ) from e

        return _client


def reset_client():
    """Force the client to be recreated on next call. For testing or recovery."""
    global _client
    with _client_lock:
        _client = None


# ─── Query execution ───────────────────────────────────────────────
def run_query(
    sql: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
    max_rows: int = DEFAULT_MAX_ROWS_RETURNED,
    dry_run: bool = False,
) -> QueryResult:
    """Execute a BigQuery SQL string.

    Args:
        sql: The SQL to execute. Must be a complete, parameter-free query string.
             (Phase 1 doesn't support parameterized queries; values come from the
             semantic layer, not raw user input.)
        timeout_seconds: Hard timeout for the query.
        max_bytes_billed: BigQuery cap on bytes billed (protects against runaway costs).
        max_rows: Maximum number of rows to fetch back (truncates if exceeded).
        dry_run: If True, validates the query and returns bytes that would be processed,
                 but doesn't return rows.

    Returns:
        QueryResult with rows, columns, and metadata.

    Raises:
        ExecutorConfigError: If client cannot be set up.
        QueryExecutionError: If the query fails.
    """
    client = _get_client()

    job_config = bigquery.QueryJobConfig(
        use_query_cache=True,
        maximum_bytes_billed=max_bytes_billed,
        dry_run=dry_run,
    )

    start = time.monotonic()

    try:
        job = client.query(sql, job_config=job_config, timeout=timeout_seconds)
    except Exception as e:
        raise QueryExecutionError(f"BigQuery query submission failed: {e}", sql=sql) from e

    if dry_run:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return QueryResult(
            columns=[],
            rows=[],
            row_count=0,
            bytes_processed=job.total_bytes_processed or 0,
            elapsed_ms=elapsed_ms,
            sql=sql,
        )

    # Wait for completion. The `timeout` on client.query() is for the submit;
    # we need to wait for the actual results separately.
    try:
        result_iterator = job.result(timeout=timeout_seconds)
    except Exception as e:
        raise QueryExecutionError(f"BigQuery query failed: {e}", sql=sql) from e

    # Extract column names from schema
    schema = result_iterator.schema
    columns = [f.name for f in schema]

    # Fetch rows up to max_rows
    rows = []
    truncated = False
    for i, row in enumerate(result_iterator):
        if i >= max_rows:
            truncated = True
            break
        rows.append([_coerce_value(row[c]) for c in columns])

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        bytes_processed=job.total_bytes_processed or 0,
        elapsed_ms=elapsed_ms,
        sql=sql,
        truncated=truncated,
    )


def _coerce_value(value: Any) -> Any:
    """Convert BigQuery types to JSON-serializable Python types."""
    if value is None:
        return None
    # datetime, date, time → ISO strings
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # Decimal → float (loses precision but JSON-friendly)
    import decimal
    if isinstance(value, decimal.Decimal):
        return float(value)
    # bytes → base64 (rare for marketing data)
    if isinstance(value, bytes):
        import base64
        return base64.b64encode(value).decode("ascii")
    return value


# ─── Convenience: dry-run cost check ───────────────────────────────
def estimate_cost(sql: str) -> int:
    """Return the bytes that this query would process. Useful for cost preview."""
    result = run_query(sql, dry_run=True)
    return result.bytes_processed


def format_bytes(byte_count: int) -> str:
    """Human-readable byte count."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if byte_count < 1024:
            return f"{byte_count:.1f} {unit}"
        byte_count /= 1024
    return f"{byte_count:.1f} PB"
