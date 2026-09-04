# Connector Contract

Implements spec §5 and the acceptance contract (§24, §26 Phase 1). SDK: `connectors/base.py`.
Reference implementation: `connectors/google_reviews_mock.py`.

## Interface (`MartinConnector`)
```
test_connection()  -> ConnectionTestResult
discover_schema()  -> SourceSchema           # fields + fingerprint + PHI class
initial_load(wm?)  -> Iterable[RawBatch]     # full history
incremental_load(wm) -> Iterable[RawBatch]   # since watermark
validate_batch(b)  -> DataQualityReport      # base: key-presence + dup detection
normalize_metadata(b) -> SourceBatchMetadata
checkpoint()       -> Watermark
```
A connector's responsibility ends at **raw**. Normalization into `experience_event` is a
separate downstream step (spec §29.5), so connectors stay thin and testable.

## Declarative config (`ConnectorConfig`)
```yaml
connector_id: google_reviews
connector_type: reputation
mode: incremental
schedule: "0 */4 * * *"
phi_classification: public
identity_mapping: { external_place_id: entity_crosswalk.google.place_id }
destination_raw_table: raw.google_reviews
dq_profile: google_reviews_v1
```

## Acceptance tests every connector must pass (spec §24)
1. **connection test** — `test_connection().ok`.
2. **initial load** — full history yields batches with a schema fingerprint.
3. **incremental load** — only records after the watermark (verified: 5 → 2 after a mid cutoff).
4. **idempotent replay** — re-running yields identical `payload_hash`; raw zone dedupes on
   `UNIQUE(source_system, source_record_key, payload_hash)`.
5. **schema drift** — a changed field set changes `discover_schema().fingerprint`.
6. **malformed row handling** — DQ `error` issues (e.g. star_rating outside 1..5) fail the batch.
7. **checkpoint recovery** — `checkpoint()` returns a serialisable watermark that resumes load.
8. **DQ reporting** — `DataQualityReport.to_dict()` is persisted on `ingest_batch.dq_report_json`.
9. **source lineage** — batch metadata records source_system + fingerprint + watermark; a
   `lineage_edge` links raw → gold on normalization.

## Roadmap of connectors (spec §4)
Reputation (Google reviews ✓ mock, star ratings) → survey → HCAHPS/CAHPS → complaints →
call-center → digital → operational (EHR/ADT, scheduling, wait/queue, provider/location/org
masters, referral, billing, outcomes). Each starts as a mock against fixtures, then swaps
`_fetch()` for the real source (spec §29 step 16).
