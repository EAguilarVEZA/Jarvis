"""
Google Reviews → canonical experience_event (spec §4.1.C, §7.1).

Maps a public Google review payload into a canonical event, resolving the Google
`place_id` to an internal location via `entity_crosswalk` (spec §8). Preserves the
original 1..5 star scale (never merges unlike scales — §4.1.D). Source text is pointed
at by `raw_text_ref`; `normalized_text` is a light-cleaned derived copy (§28.11). Topic
and sentiment are left NULL — they're produced later by the NLP engines, never here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .pipeline import Normalizer, deterministic_id

SOURCE_SYSTEM = "google_reviews"

# Minimal location seed: internal locations + Google place_id crosswalk (spec §8).
# In prod this is loaded from the location master + a governed crosswalk.
LOCATION_SEED = [
    {"entity_id": "LOC_DOWNTOWN", "name": "Orlando Health Downtown",
     "google_place_id": "ChIJ_OrlandoHealth_Downtown"},
    {"entity_id": "LOC_WINNIE", "name": "Winnie Palmer Hospital",
     "google_place_id": "ChIJ_OrlandoHealth_Winnie"},
]


def seed_locations(store) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for loc in LOCATION_SEED:
        store.upsert("entity_dimension", {
            "entity_key": deterministic_id("location", loc["entity_id"]),
            "entity_type": "location", "entity_id": loc["entity_id"],
            "display_name": loc["name"], "npi": None, "parent_entity_id": None,
            "specialty": None, "attributes_json": None,
            "valid_from": now, "valid_to": None, "is_current": 1,
        }, conflict_cols=["entity_key"])
        store.upsert("entity_crosswalk", {
            "crosswalk_id": deterministic_id("google", loc["google_place_id"], "location"),
            "external_system": "google", "external_id": loc["google_place_id"],
            "internal_entity_type": "location", "internal_entity_id": loc["entity_id"],
            "resolution_level": "exact", "confidence": 1.0, "created_at": now,
        }, conflict_cols=["external_system", "external_id", "internal_entity_type"])
    store.commit()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


class GoogleReviewsNormalizer(Normalizer):
    source_type = "google_reviews"

    def to_event(self, p: dict, store) -> tuple[dict, list[dict]]:
        now = datetime.now(timezone.utc).isoformat()
        src_id = str(p["external_review_id"])
        event_id = deterministic_id(SOURCE_SYSTEM, src_id)

        # crosswalk place_id → internal location (unresolved stays analytically usable — §8)
        location_id = store.scalar(
            "SELECT internal_entity_id FROM entity_crosswalk "
            "WHERE external_system='google' AND external_id=? AND internal_entity_type='location'",
            [p.get("place_id")])

        event = {
            "experience_event_id": event_id,
            "source_system": SOURCE_SYSTEM, "source_type": "reputation", "source_record_id": src_id,
            "patient_token": None, "encounter_id": None, "appointment_id": None,
            "provider_id": None, "location_id": location_id, "department_id": None, "service_line_id": None,
            "event_at": p.get("review_date"), "feedback_at": p.get("review_date"),
            "journey_stage": None,
            "rating_value": p.get("star_rating"), "rating_scale_min": 1, "rating_scale_max": 5,
            "likelihood_to_recommend": None, "source_metric_code": "google_star",
            "raw_text_ref": p.get("source_url"),           # pointer to source text
            "normalized_text": _clean(p.get("review_text", "")),
            "language_code": "en",
            "primary_topic_id": None, "sentiment_label": None, "sentiment_score": None,
            "emotion_json": None, "severity": None, "action_required": None,
            "classification_confidence": None, "model_version": None,
            "event_metadata": None, "created_at": now, "updated_at": now,
        }
        items = []
        if event["normalized_text"]:
            items.append({
                "feedback_item_id": deterministic_id(event_id, "0"),
                "experience_event_id": event_id, "segment_index": 0,
                "original_text": p.get("review_text"),       # source, never mutated
                "normalized_text": event["normalized_text"],
                "language_code": "en", "created_at": now,
            })
        return event, items
