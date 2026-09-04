"""
MARTIN connector SDK (Epic B) — the standard contract every source connector implements.
Per spec §5 and the acceptance contract in §24 / §26 Phase 1.

Design goals (spec §28): idempotent ingestion, explicit checkpoints, schema-drift
detection, data-quality reporting, dead-letter handling, PHI classification, and full
lineage. No web framework here — pure Python so the ingestion worker, tests, and the
API layer can all use it.

A connector's job stops at RAW: it authenticates, discovers schema, pulls batches of
source-shaped rows, validates them, and reports a watermark. Normalization into the
canonical model (experience_event, ...) is a separate downstream step (spec §6, §29.5).
"""
from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_hash(payload: dict) -> str:
    """Stable hash of a source payload → idempotent replay + change detection."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def schema_fingerprint(field_names: Iterable[str]) -> str:
    """Fingerprint the set of source fields so schema drift is detectable (spec §5)."""
    return hashlib.sha256(",".join(sorted(field_names)).encode("utf-8")).hexdigest()[:16]


# ── Contract value objects ───────────────────────────────────────────────────

@dataclass
class ConnectionTestResult:
    ok: bool
    detail: str = ""
    checked_at: str = field(default_factory=_utcnow)


@dataclass
class SourceSchema:
    connector_id: str
    fields: list[str]
    fingerprint: str = ""
    phi_classification: str = "internal"   # public|internal|confidential|phi|restricted

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = schema_fingerprint(self.fields)


@dataclass
class RawRecord:
    """One source-shaped record on its way to the raw/bronze zone (spec §6.1)."""
    source_record_key: str
    payload: dict
    operation_type: str = "upsert"          # upsert|delete|correction
    extracted_at: Optional[str] = None

    @property
    def payload_hash(self) -> str:
        return payload_hash(self.payload)


@dataclass
class RawBatch:
    connector_id: str
    records: list[RawRecord]
    watermark: "Watermark"
    schema_fingerprint: str = ""
    extracted_at: str = field(default_factory=_utcnow)

    @property
    def count(self) -> int:
        return len(self.records)


@dataclass
class DataQualityIssue:
    rule: str
    severity: str          # info|warn|error
    count: int
    detail: str = ""


@dataclass
class DataQualityReport:
    connector_id: str
    checked: int
    passed: int
    issues: list[DataQualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict:
        return {
            "connector_id": self.connector_id, "checked": self.checked,
            "passed": self.passed, "ok": self.ok,
            "issues": [i.__dict__ for i in self.issues],
        }


@dataclass
class SourceBatchMetadata:
    connector_id: str
    source_system: str
    connector_type: str
    phi_classification: str
    record_count: int
    schema_fingerprint: str
    watermark_value: Optional[str]
    extracted_at: str = field(default_factory=_utcnow)


@dataclass
class Watermark:
    """Opaque, serialisable checkpoint — a connector defines its own contents
    (a timestamp, a page token, an id cursor). Persisted between runs (spec §5)."""
    value: Optional[str] = None
    strategy: str = "timestamp"             # timestamp|token|id_cursor|full
    updated_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {"value": self.value, "strategy": self.strategy, "updated_at": self.updated_at}


@dataclass
class ConnectorConfig:
    """Declarative connector configuration (spec §5)."""
    connector_id: str
    connector_type: str                      # reputation|survey|ehr|scheduling|...
    mode: str = "incremental"                # full|incremental
    schedule: str = ""                       # cron
    source_owner: str = ""
    phi_classification: str = "internal"
    identity_mapping: dict = field(default_factory=dict)
    destination_raw_table: str = ""
    dq_profile: str = ""
    options: dict = field(default_factory=dict)


# ── The connector interface ──────────────────────────────────────────────────

class MartinConnector(abc.ABC):
    """Every source connector implements this. See google_reviews_mock.py for a
    reference implementation against fixtures."""

    connector_type: str = "generic"
    phi_classification: str = "internal"

    def __init__(self, config: ConnectorConfig):
        self.config = config

    # identity ----------------------------------------------------------------
    @property
    def connector_id(self) -> str:
        return self.config.connector_id

    @property
    def source_system(self) -> str:
        return self.config.connector_id

    # required contract (spec §5) --------------------------------------------
    @abc.abstractmethod
    def test_connection(self) -> ConnectionTestResult: ...

    @abc.abstractmethod
    def discover_schema(self) -> SourceSchema: ...

    @abc.abstractmethod
    def initial_load(self, watermark: Optional[Watermark] = None) -> Iterable[RawBatch]: ...

    @abc.abstractmethod
    def incremental_load(self, watermark: Watermark) -> Iterable[RawBatch]: ...

    def validate_batch(self, batch: RawBatch) -> DataQualityReport:
        """Default DQ: required-key presence + duplicate detection. Override for
        source-specific rules (spec §24 'connector contract tests')."""
        issues: list[DataQualityIssue] = []
        seen: set[str] = set()
        dupes = 0
        missing_key = 0
        for r in batch.records:
            if not r.source_record_key:
                missing_key += 1
            h = r.payload_hash
            if h in seen:
                dupes += 1
            seen.add(h)
        if missing_key:
            issues.append(DataQualityIssue("source_record_key_present", "error", missing_key,
                                           "records missing a source_record_key"))
        if dupes:
            issues.append(DataQualityIssue("no_duplicate_payloads", "warn", dupes,
                                           "duplicate payload hashes in batch"))
        return DataQualityReport(self.connector_id, batch.count, batch.count - missing_key, issues)

    def normalize_metadata(self, batch: RawBatch) -> SourceBatchMetadata:
        return SourceBatchMetadata(
            connector_id=self.connector_id, source_system=self.source_system,
            connector_type=self.connector_type, phi_classification=self.phi_classification,
            record_count=batch.count, schema_fingerprint=batch.schema_fingerprint,
            watermark_value=batch.watermark.value,
        )

    @abc.abstractmethod
    def checkpoint(self) -> Watermark: ...
