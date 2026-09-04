"""
End-to-end slice (spec §29 through step 5): connector → raw → normalize → canonical gold.

Run:  python3 -m martin_experience.demo_slice
Proves: taxonomy loads; the mock Google connector's batch normalizes into canonical
experience_event + feedback_item; the Google place_id resolves to an internal location via
the crosswalk; lineage links raw→gold; and a second run is idempotent (no duplicates).
"""
from __future__ import annotations

from .store import Store
from .taxonomy.service import load_taxonomy
from .connectors.google_reviews_mock import GoogleReviewsMockConnector
from .normalization.pipeline import NormalizationPipeline
from .normalization.google_reviews import GoogleReviewsNormalizer, seed_locations


def build(store: Store) -> dict:
    store.apply_migration()
    topics = load_taxonomy(store)
    seed_locations(store)
    conn = GoogleReviewsMockConnector()
    pipe = NormalizationPipeline(store)
    norm = GoogleReviewsNormalizer()
    result = {"topics_loaded": topics, "runs": []}
    for _ in range(2):  # run twice → prove idempotency
        batch = list(conn.initial_load())[0]
        result["runs"].append(pipe.run(conn, norm, batch))
    return result


def main():
    store = Store(":memory:")
    r = build(store)
    print("taxonomy topics loaded:", r["topics_loaded"])
    print("run 1:", r["runs"][0])
    print("run 2 (idempotent):", r["runs"][1])

    n_events = store.scalar("SELECT COUNT(*) FROM experience_event")
    n_items = store.scalar("SELECT COUNT(*) FROM feedback_item")
    n_lineage = store.scalar("SELECT COUNT(*) FROM lineage_edge")
    resolved = store.scalar("SELECT COUNT(*) FROM experience_event WHERE location_id IS NOT NULL")
    print(f"\ncanonical events: {n_events} | feedback items: {n_items} | "
          f"lineage edges: {n_lineage} | location-resolved: {resolved}")

    print("\nevents by location (gold, joined to the location dimension):")
    rows = store.query(
        "SELECT d.display_name AS location, COUNT(*) AS reviews, "
        "ROUND(AVG(e.rating_value),2) AS avg_stars "
        "FROM experience_event e JOIN entity_dimension d "
        "  ON d.entity_type='location' AND d.entity_id=e.location_id "
        "GROUP BY d.display_name ORDER BY reviews DESC")
    for row in rows:
        print(f"  {row['location']:28} reviews={row['reviews']}  avg_stars={row['avg_stars']}")

    assert n_events == 5, "expected 5 canonical events"
    assert store.scalar("SELECT COUNT(*) FROM experience_event") == 5, "idempotency broken"
    assert resolved == 5, "all reviews should resolve to a location"
    print("\nOK — source→raw→canonical slice is live, idempotent, resolved, and traceable.")


if __name__ == "__main__":
    main()
