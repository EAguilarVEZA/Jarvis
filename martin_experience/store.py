"""
Portable dev store for the canonical experience model. Applies 0001_canonical.sql to a
SQLite database (local dev / tests) and provides small upsert/query helpers. The SAME
row shapes land in BigQuery gold for prod — this store keeps the pipeline runnable and
testable offline without a warehouse.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Optional

_MIGRATION = os.path.join(os.path.dirname(__file__), "db", "migrations", "0001_canonical.sql")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def apply_migration(self, path: str = _MIGRATION) -> "Store":
        with open(path, encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.commit()
        return self

    # generic helpers --------------------------------------------------------
    def upsert(self, table: str, row: dict, conflict_cols: Optional[list[str]] = None):
        """INSERT ... ON CONFLICT DO UPDATE — idempotent (spec §28.5)."""
        cols = list(row.keys())
        ph = ",".join("?" for _ in cols)
        collist = ",".join(cols)
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        if conflict_cols:
            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_cols)
            sql += f" ON CONFLICT({','.join(conflict_cols)}) DO UPDATE SET {updates}"
        else:
            sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
        self.conn.execute(sql, [row[c] for c in cols])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return r[0] if r else None

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()
