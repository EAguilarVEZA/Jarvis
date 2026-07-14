"""
semantic.dialects.snowflake
───────────────────────────
Snowflake quotes identifiers with double-quotes, uses fully-qualified
DB.SCHEMA.TABLE references, and a few function differences:
  • DIV0 / DIV0NULL exists, but NULLIF + division is equivalent and
    portable, so we keep that pattern.
  • Boolean → integer cast uses ::INT.
  • Date literals: BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD' — same as BQ.
"""

from __future__ import annotations
from .base import Dialect


class SnowflakeDialect(Dialect):
    name = "snowflake"

    def quote_ident(self, name: str) -> str:
        clean = name.replace('"', '')
        return f'"{clean}"'

    def qualify_table(self, project, dataset, table) -> str:
        # In Snowflake "project" maps to DATABASE, "dataset" to SCHEMA.
        parts = [p for p in (project, dataset, table) if p]
        return ".".join(f'"{p}"' for p in parts)

    def safe_divide(self, num_sql: str, den_sql: str) -> str:
        return f"({num_sql}) / NULLIF({den_sql}, 0)"

    def cast_int64(self, expr: str) -> str:
        return f"({expr})::INT"

    def date_between_literal(self, col_sql: str, start_iso: str, end_iso: str) -> str:
        return f"{col_sql} BETWEEN '{start_iso}' AND '{end_iso}'"
