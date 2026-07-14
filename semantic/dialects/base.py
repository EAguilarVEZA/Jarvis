"""
semantic.dialects.base
──────────────────────
Abstract dialect class — define every SQL-flavor-dependent string here.
"""

from __future__ import annotations


class Dialect:
    name = "abstract"

    # ─── Identifier quoting ─────────────────────────────────────────
    def quote_ident(self, name: str) -> str:
        """Quote a column / table name so it survives reserved-word collisions."""
        raise NotImplementedError

    def qualify_table(self, project: str | None, dataset: str | None, table: str) -> str:
        """Return a fully-qualified table reference."""
        raise NotImplementedError

    # ─── Functions ──────────────────────────────────────────────────
    def safe_divide(self, num_sql: str, den_sql: str) -> str:
        """Divide num by den, NULL on /0. Used by resolver-generated calc metrics."""
        raise NotImplementedError

    def cast_int64(self, expr: str) -> str:
        """Cast to a 64-bit integer (used by SUM_BOOL aggregation)."""
        raise NotImplementedError

    def date_between_literal(self, col_sql: str, start_iso: str, end_iso: str) -> str:
        """Build `col BETWEEN <start> AND <end>` clause using dialect's date literals."""
        raise NotImplementedError

    # ─── String literals ────────────────────────────────────────────
    def string_literal(self, value) -> str:
        """Render a Python value as a SQL string literal, escaping quotes."""
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value).replace("'", "''")
        return f"'{s}'"
