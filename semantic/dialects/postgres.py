"""
semantic.dialects.postgres
──────────────────────────
PostgreSQL dialect — also serves Redshift (Postgres-compatible wire
protocol).  Differences from BigQuery:
  • Identifiers in double-quotes.
  • Two-part naming SCHEMA.TABLE (no separate "project").
  • Boolean → int cast uses ::INT.
  • NULLIF + division is portable.
"""

from __future__ import annotations
from .base import Dialect


class PostgresDialect(Dialect):
    name = "postgres"

    def quote_ident(self, name: str) -> str:
        clean = name.replace('"', '')
        return f'"{clean}"'

    def qualify_table(self, project, dataset, table) -> str:
        # Postgres ignores "project"; use schema.table.
        parts = [p for p in (dataset, table) if p]
        return ".".join(f'"{p}"' for p in parts)

    def safe_divide(self, num_sql: str, den_sql: str) -> str:
        return f"({num_sql}) / NULLIF({den_sql}, 0)"

    def cast_int64(self, expr: str) -> str:
        return f"({expr})::BIGINT"

    def date_between_literal(self, col_sql: str, start_iso: str, end_iso: str) -> str:
        return f"{col_sql} BETWEEN DATE '{start_iso}' AND DATE '{end_iso}'"
