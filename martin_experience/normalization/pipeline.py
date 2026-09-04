"""
Canonical normalization (spec §29 step 5, §6 bronze→gold).

A connector's RawBatch lands as immutable `raw_record` (bronze), then a source-specific
Normalizer maps each record into the canonical `experience_event` + `feedback_item` (gold),
resolving entity crosswalks and emitting `lineage_edge` rows. Idempotent: re-running the
same batch upserts, never duplicates (spec §28.5). AI enrichment (topic/sentiment) is a
SEPARATE downstream step — this stage never invents topics or overwrites source (§28.2/§28.11).
"""
from __future__ import annotations

import abc
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def deterministic_id(*parts: str) -> str:
    """Stable id from source keys → idempotent upserts across re-runs."""
    return uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(p) for p in parts)).hex


class Normalizer(abc.ABC):
    """Maps one raw payload → a canonical event dict (+ text segments)."""
    source_type: str = "generic"

    @abc.abstractmethod
    def to_event(self, payload: dict, store) -> tuple[dict, list[dict]]:
        """Return (experience_event_row, [feedback_item_rows])."""
        ...


class NormalizationPipeline:
    def __init__(self, store):
        self.store = store

    def _land_raw(self, batch_meta: dict, record, source_system: str) -> str:
        raw_id = deterministic_id(source_system, record.source_record_key, record.payload_hash)
        self.store.upsert("raw_record", {
            "raw_id": raw_id,
            "ingestion_batch_id": batch_meta["ingestion_batch_id"],
            "source_system": source_system,
            "source_record_key": record.source_record_key,
            "payload_json": json.dumps(record.payload, default=str),
            "payload_hash": record.payload_hash,
            "operation_type": record.operation_type,
            "schema_version": None,
            "extracted_at": record.extracted_at,
            "received_at": _now(),
        }, conflict_cols=["source_system", "source_record_key", "payload_hash"])
        return raw_id

    def run(self, connector, normalizer: Normalizer, batch) -> dict:
        source_system = connector.source_system
        meta = connector.normalize_metadata(batch)
        batch_id = deterministic_id(source_system, meta.watermark_value or "", str(batch.count))

        # register source + ingest batch (source metadata / lineage — spec §28.6)
        self.store.upsert("source_system", {
            "source_system": source_system, "connector_type": connector.connector_type,
            "source_owner": connector.config.source_owner, "phi_classification": connector.phi_classification,
            "extraction_mode": connector.config.mode, "schedule_cron": connector.config.schedule,
            "active": 1, "created_at": _now(),
        }, conflict_cols=["source_system"])
        self.store.upsert("ingest_batch", {
            "ingestion_batch_id": batch_id, "source_system": source_system,
            "extracted_at": batch.extracted_at, "received_at": _now(),
            "record_count": batch.count, "schema_fingerprint": batch.schema_fingerprint,
            "source_watermark": meta.watermark_value, "status": "ok",
            "dq_report_json": json.dumps(connector.validate_batch(batch).to_dict()),
        }, conflict_cols=["ingestion_batch_id"])

        events = 0
        for record in batch.records:
            raw_id = self._land_raw({"ingestion_batch_id": batch_id}, record, source_system)
            event, items = normalizer.to_event(record.payload, self.store)
            self.store.upsert("experience_event", event,
                              conflict_cols=["source_system", "source_record_id"])
            for it in items:
                self.store.upsert("feedback_item", it, conflict_cols=["feedback_item_id"])
            # lineage raw → gold (every claim traces to evidence — spec §28.4)
            self.store.upsert("lineage_edge", {
                "lineage_id": deterministic_id(raw_id, event["experience_event_id"]),
                "from_kind": "raw", "from_id": raw_id,
                "to_kind": "gold", "to_id": event["experience_event_id"],
                "transform": "normalize." + normalizer.source_type, "transform_version": "1.0",
                "created_at": _now(),
            }, conflict_cols=["lineage_id"])
            events += 1

        self.store.commit()
        return {"batch_id": batch_id, "raw_landed": batch.count, "events": events}
