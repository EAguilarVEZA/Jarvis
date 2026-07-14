"""
semantic.schema_introspect
──────────────────────────
Fetches live table schemas from BigQuery.

The semantic.yaml is a curated subset of what BigQuery actually has. The
introspector lets the Curation Mode UI show ALL raw columns of a table
(not just the curated ones) so a user can choose to surface more fields.

Cached aggressively because schemas rarely change between sessions.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .loader import SemanticModel
from .executor import _get_client, ExecutorConfigError


@dataclass
class RawColumn:
    """One column as it exists in BigQuery."""
    name: str
    type: str                       # 'STRING', 'INT64', 'FLOAT64', 'TIMESTAMP', etc.
    mode: str = "NULLABLE"          # 'NULLABLE' | 'REQUIRED' | 'REPEATED'
    description: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "mode": self.mode,
            "description": self.description,
        }


@dataclass
class RawTableSchema:
    """A BigQuery table's full schema as it exists in the warehouse."""
    project: str
    dataset: str
    table: str
    columns: list[RawColumn] = field(default_factory=list)
    num_rows: Optional[int] = None
    size_bytes: Optional[int] = None
    fetched_at: float = 0.0          # monotonic time when fetched

    @property
    def fqn(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"

    def column(self, name: str) -> Optional[RawColumn]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "dataset": self.dataset,
            "table": self.table,
            "fqn": self.fqn,
            "columns": [c.to_dict() for c in self.columns],
            "num_rows": self.num_rows,
            "size_bytes": self.size_bytes,
        }


# ─── Cache ─────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_schema_cache: dict[str, RawTableSchema] = {}
CACHE_TTL_SECONDS = 3600   # 1 hour — schemas rarely change


def _cache_key(project: str, dataset: str, table: str) -> str:
    return f"{project}.{dataset}.{table}"


# ─── Introspection ─────────────────────────────────────────────────
def introspect_table(project: str, dataset: str, table: str, *, force: bool = False) -> RawTableSchema:
    """Fetch a single table's schema from BigQuery.

    Cached with a 1-hour TTL.
    """
    key = _cache_key(project, dataset, table)
    now = time.monotonic()

    with _cache_lock:
        cached = _schema_cache.get(key)
        if cached and not force and (now - cached.fetched_at) < CACHE_TTL_SECONDS:
            return cached

    # Fetch from BigQuery
    client = _get_client()
    table_ref = client.get_table(f"{project}.{dataset}.{table}")

    columns = []
    for field_ in table_ref.schema:
        columns.append(RawColumn(
            name=field_.name,
            type=field_.field_type,
            mode=field_.mode or "NULLABLE",
            description=field_.description,
        ))

    schema = RawTableSchema(
        project=project,
        dataset=dataset,
        table=table,
        columns=columns,
        num_rows=table_ref.num_rows,
        size_bytes=table_ref.num_bytes,
        fetched_at=now,
    )

    with _cache_lock:
        _schema_cache[key] = schema

    return schema


def introspect_curated_table(table_key: str, model: SemanticModel) -> RawTableSchema:
    """Introspect a table that's already curated in the semantic layer.

    Looks up the source and raw_table from the model and fetches the live schema.
    """
    table = model.table(table_key)
    if not table:
        raise ValueError(f"Unknown table key: {table_key}")

    source = model.sources.get(table.source)
    if not source:
        raise ValueError(f"Unknown source: {table.source}")

    if source.type != "bigquery":
        raise ValueError(f"Schema introspection only supports BigQuery sources (got {source.type})")

    return introspect_table(source.project, source.dataset, table.raw_table)


def list_dataset_tables(project: str, dataset: str) -> list[str]:
    """List all tables in a BigQuery dataset. Used by Curation Mode to show
    the "raw warehouse" — all tables available for curation.
    """
    client = _get_client()
    tables = client.list_tables(f"{project}.{dataset}")
    return [t.table_id for t in tables]


def clear_cache():
    """Clear the schema cache (forces re-fetch on next call)."""
    with _cache_lock:
        _schema_cache.clear()
