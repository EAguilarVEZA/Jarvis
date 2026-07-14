"""
semantic.dialects
─────────────────
SQL dialect abstractions used by the semantic engine. Each dialect
implements identifier quoting, divide-by-zero guards, and a handful
of function-name differences so the same StructuredQuery can resolve
into BigQuery / Snowflake / Postgres / Redshift / etc. without the
resolver hard-coding any one flavor.

Phase 1 ships:
  • BigQuery  — full implementation (matches current behaviour).
  • Snowflake — sufficient for read queries against shipped widgets.
  • Postgres  — sufficient for the same.
  • Default   — falls back to BigQuery for any unknown dialect, so
                existing code that doesn't know about dialects keeps
                working unchanged.

Phase 2 will route based on the connection bound to each source
(see semantic.executor).
"""

from __future__ import annotations
from typing import Optional

from .base import Dialect
from .bigquery import BigQueryDialect
from .snowflake import SnowflakeDialect
from .postgres import PostgresDialect

_DIALECTS = {
    "bigquery": BigQueryDialect(),
    "snowflake": SnowflakeDialect(),
    "postgres": PostgresDialect(),
    "redshift": PostgresDialect(),   # Postgres-compatible wire protocol
}


def get_dialect(name: Optional[str] = None) -> Dialect:
    """Resolve a dialect by name. Unknown / None → BigQuery (current default)."""
    if not name:
        return _DIALECTS["bigquery"]
    return _DIALECTS.get(name.lower(), _DIALECTS["bigquery"])


__all__ = ["Dialect", "BigQueryDialect", "SnowflakeDialect", "PostgresDialect", "get_dialect"]
