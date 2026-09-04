# Experience API (skeleton)

Implements spec §15. Mounts under `/v1/experience/*` (and sibling `/v1/*`) in `server.py`
behind the existing auth/tenancy middleware, feature-flagged (default off). Every route is
authenticated, authorized, tenant-scoped, audited, and traceable to source lineage.

## Routes
```
POST /v1/experience/query                 # NL question → orchestrated, grounded answer
GET  /v1/experience/events                 # filtered canonical events
GET  /v1/metrics/{metric_id}               # governed metric value
POST /v1/metrics/compare                   # period / entity comparison
POST /v1/analytics/segment                 # segment a metric
POST /v1/analytics/root-cause              # structured FindingBundle
GET  /v1/anomalies                         # early-warning issue candidates
GET  /v1/issues/{issue_id}
POST /v1/retrieval/feedback                # metadata-filtered evidence search
GET  /v1/entities/{type}/{id}
GET  /v1/entities/{type}/{id}/hierarchy
GET  /v1/interventions
POST /v1/interventions
POST /v1/interventions/{id}/measure
POST /v1/conversations                     # conversational feedback agent (spec §14)
POST /v1/conversations/{id}/turn
GET  /v1/model-registry
```

## Answer contract — every analytical answer carries evidence (spec §2.4, CLAUDE.md)
The orchestrator composes a `FindingBundle` from deterministic engines; the LLM only narrates
it and must not add unsupported facts:
```json
{
  "question": "Why did experience decline?",
  "entity": {"type": "facility", "id": "FAC123", "name": "Example Facility"},
  "period": {"start": "2026-08-01", "end": "2026-08-31"},
  "comparison_period": {"start": "2026-07-01", "end": "2026-07-31"},
  "findings": [{
    "type": "metric_change", "metric_id": "survey_ltr",
    "current": 82.1, "comparison": 87.4, "n_current": 1244, "n_comparison": 1198,
    "method": "difference_of_periods", "confidence": 0.98, "evidence_refs": ["metric:..."]
  }],
  "candidate_drivers": [], "feedback_evidence": [], "limitations": [],
  "generated_at": "...", "engine_versions": {}
}
```

## Tool registry (spec §12.3) — what the orchestrator can call
`get_metric`, `compare_metric`, `trend_metric`, `segment_metric`, `get_topic_distribution`,
`get_sentiment_distribution`, `find_anomalies`, `investigate_root_cause`,
`retrieve_feedback_examples`, `get_operational_context`, `get_entity_hierarchy`,
`get_benchmark`, `get_active_interventions`, `measure_intervention`,
`search_experience_knowledge`. Each is a governed, versioned, deterministic function —
registered into MARTIN's existing agent tool loop.
