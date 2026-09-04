"""
Mock Google Reviews connector (spec §4.1.C, §29 step 4) — reference implementation of
the MartinConnector contract against local fixtures. Proves the SDK end to end without a
live Google account, and serves as the template for the real connector (swap _fetch()).

phi_classification = "public" — Google reviews are public content (spec §5 example).
Incremental watermark strategy = review timestamp cursor (idempotent replay: the raw
zone dedupes on payload hash + source_record_key).
"""
from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from .base import (
    ConnectionTestResult, ConnectorConfig, DataQualityIssue, DataQualityReport,
    MartinConnector, RawBatch, RawRecord, SourceSchema, Watermark,
    schema_fingerprint,
)

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "google_reviews.json")
_FIELDS = ["external_review_id", "place_id", "location_name", "review_date",
           "reviewer_display_name", "star_rating", "review_text",
           "owner_response", "owner_response_at", "source_url"]


class GoogleReviewsMockConnector(MartinConnector):
    connector_type = "reputation"
    phi_classification = "public"

    def __init__(self, config: Optional[ConnectorConfig] = None, fixture_path: str = _FIXTURE):
        super().__init__(config or ConnectorConfig(
            connector_id="google_reviews", connector_type="reputation", mode="incremental",
            schedule="0 */4 * * *", phi_classification="public",
            identity_mapping={"external_place_id": "entity_crosswalk.google.place_id"},
            destination_raw_table="raw.google_reviews", dq_profile="google_reviews_v1"))
        self._fixture_path = fixture_path
        self._last_watermark = Watermark(strategy="timestamp")

    # ── source access (swap this for the real Google Business Profile API) ──
    def _fetch(self) -> list[dict]:
        with open(self._fixture_path, encoding="utf-8") as f:
            return json.load(f)

    # ── contract ────────────────────────────────────────────────────────────
    def test_connection(self) -> ConnectionTestResult:
        try:
            rows = self._fetch()
            return ConnectionTestResult(True, f"fixture reachable ({len(rows)} reviews)")
        except Exception as e:  # noqa
            return ConnectionTestResult(False, str(e))

    def discover_schema(self) -> SourceSchema:
        return SourceSchema(connector_id=self.connector_id, fields=list(_FIELDS),
                            phi_classification="public")

    def _batch(self, rows: list[dict]) -> RawBatch:
        records = [RawRecord(source_record_key=r["external_review_id"], payload=r,
                             extracted_at=r.get("review_date")) for r in rows]
        wm_value = max((r["review_date"] for r in rows), default=None) if rows else None
        self._last_watermark = Watermark(value=wm_value, strategy="timestamp")
        return RawBatch(connector_id=self.connector_id, records=records,
                        watermark=self._last_watermark,
                        schema_fingerprint=schema_fingerprint(_FIELDS))

    def initial_load(self, watermark: Optional[Watermark] = None) -> Iterable[RawBatch]:
        yield self._batch(self._fetch())

    def incremental_load(self, watermark: Watermark) -> Iterable[RawBatch]:
        cutoff = watermark.value or ""
        rows = [r for r in self._fetch() if str(r.get("review_date", "")) > cutoff]
        yield self._batch(rows)

    def validate_batch(self, batch: RawBatch) -> DataQualityReport:
        # start from the base DQ (key presence + dup detection) then add source rules
        report = super().validate_batch(batch)
        bad_stars = sum(1 for r in batch.records
                        if not (isinstance(r.payload.get("star_rating"), (int, float))
                                and 1 <= r.payload["star_rating"] <= 5))
        if bad_stars:
            report.issues.append(DataQualityIssue("star_rating_in_1_5", "error", bad_stars,
                                                  "star_rating outside 1..5"))
        return report

    def checkpoint(self) -> Watermark:
        return self._last_watermark
