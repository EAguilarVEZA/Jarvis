"""
semantic.dialects.bigquery
──────────────────────────
BigQuery dialect — matches the current resolver behaviour exactly so
no existing query plan changes when the dialect layer is plumbed in.
"""

from __future__ import annotations
from .base import Dialect


class BigQueryDialect(Dialect):
    name = "bigquery"

    def quote_ident(self, name: str) -> str:
        clean = name.replace("`", "")
        return f"`{clean}`"

    def qualify_table(self, project, dataset, table) -> str:
        parts = [p for p in (project, dataset, table) if p]
        return ".".join(f"`{p}`" for p in parts)

    def safe_divide(self, num_sql: str, den_sql: str) -> str:
        # Match the resolver's existing pattern for calculated metrics.
        return f"({num_sql}) / NULLIF({den_sql}, 0)"

    def cast_int64(self, expr: str) -> str:
        return f"CAST({expr} AS INT64)"

    def date_between_literal(self, col_sql: str, start_iso: str, end_iso: str) -> str:
        return f"{col_sql} BETWEEN '{start_iso}' AND '{end_iso}'"
