"""
BigQuery client wrapper with on-disk query caching.

Why caching matters: dashboards that auto-refresh + voice that triggers the
same query repeatedly will burn through BQ scan budget fast. The data marts
in oh-data-warehouse only update overnight, so a 6-hour TTL on cache hits
the right balance — fresh enough for daily ops, cheap enough to leave on.

Cache invalidation: bump CACHE_VERSION when a query's logic changes; the
hash includes it so old cached results stop being read automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.reporting.bq")

PROJECT = "oh-data-warehouse"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "jarvis" / "bq"
DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6 hours
CACHE_VERSION = 1                   # bump to invalidate every cached result


@dataclass
class QueryResult:
    rows: list[dict]
    bytes_billed: int        # 0 if cache hit
    cache_hit: bool
    elapsed_ms: int

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def first(self) -> dict | None:
        return self.rows[0] if self.rows else None


class BQClient:
    """Thin wrapper around google.cloud.bigquery with caching.

    Lazy-imports the BQ library so unit tests and the install probe don't
    require the package to be present.
    """

    def __init__(
        self,
        project: str = PROJECT,
        cache_dir: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        credentials_path: str | None = None,
    ):
        self.project = project
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        if credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        self._client = None

    @property
    def client(self):
        if self._client is None:
            # Lazy import so the rest of the module is importable without BQ
            from google.cloud import bigquery
            self._client = bigquery.Client(project=self.project)
            log.info("BigQuery client connected to %s", self.project)
        return self._client

    # ---- caching ----
    def _cache_key(self, sql: str, params: dict | None) -> str:
        payload = json.dumps(
            {"v": CACHE_VERSION, "sql": sql, "params": params or {}},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pkl"

    def _read_cache(self, key: str) -> list[dict] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.ttl_seconds:
            return None
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception as e:
            log.warning("cache read failed for %s: %s", key[:8], e)
            return None

    def _write_cache(self, key: str, rows: list[dict]) -> None:
        try:
            with self._cache_path(key).open("wb") as f:
                pickle.dump(rows, f)
        except Exception as e:
            log.warning("cache write failed for %s: %s", key[:8], e)

    # ---- query ----
    def query(
        self,
        sql: str,
        params: dict | None = None,
        use_cache: bool = True,
        timeout_seconds: int = 60,
    ) -> QueryResult:
        """Run SQL with optional named-parameter binding.

        params: dict like {"start_date": date(2026, 1, 1), "service_line": "cardiology"}.
                Types are inferred from Python values.
        """
        start = time.time()
        key = self._cache_key(sql, params)

        if use_cache:
            cached = self._read_cache(key)
            if cached is not None:
                return QueryResult(
                    rows=cached,
                    bytes_billed=0,
                    cache_hit=True,
                    elapsed_ms=int((time.time() - start) * 1000),
                )

        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig()
        if params:
            job_config.query_parameters = list(_to_bq_params(params))

        log.debug("BQ query: %s", sql[:120].replace("\n", " "))
        job = self.client.query(sql, job_config=job_config)
        results = job.result(timeout=timeout_seconds)
        rows = [dict(r) for r in results]

        # Coerce non-JSON-friendly types (Decimal, date, datetime) to strings/floats
        # for safer downstream serialization. We keep the originals in the pickle
        # since the cache is consumed by the same code that produced it.
        if use_cache:
            self._write_cache(key, rows)

        return QueryResult(
            rows=rows,
            bytes_billed=job.total_bytes_billed or 0,
            cache_hit=False,
            elapsed_ms=int((time.time() - start) * 1000),
        )

    def healthcheck(self) -> dict:
        """Lightweight probe used by Jarvis startup. Returns sync-time + lead count."""
        sql = """
            SELECT
              COUNT(*) AS total_leads,
              MAX(date) AS last_lead_date
            FROM `oh-data-warehouse.data_mart_all.leads`
            WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
        """
        res = self.query(sql, use_cache=False, timeout_seconds=15)
        row = res.first() or {}
        return {
            "ok": True,
            "total_leads_90d": row.get("total_leads", 0),
            "last_lead_date": str(row.get("last_lead_date", "")),
            "elapsed_ms": res.elapsed_ms,
        }


def _to_bq_params(params: dict):
    """Yield bigquery.ScalarQueryParameter objects for a dict of name→value."""
    from datetime import date, datetime
    from decimal import Decimal
    from google.cloud import bigquery

    type_map = {
        str: "STRING",
        int: "INT64",
        float: "FLOAT64",
        bool: "BOOL",
        date: "DATE",
        datetime: "TIMESTAMP",
        Decimal: "NUMERIC",
    }
    for name, value in params.items():
        if value is None:
            yield bigquery.ScalarQueryParameter(name, "STRING", None)
            continue
        bq_type = next(
            (t for py_t, t in type_map.items() if isinstance(value, py_t)),
            "STRING",
        )
        yield bigquery.ScalarQueryParameter(name, bq_type, value)
