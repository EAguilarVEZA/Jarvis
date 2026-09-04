"""
NLP demo (spec §11.2-11.3). Runs topic + aspect-sentiment over the canonical events, shows
the aspect breakdown for the mixed 'wonderful but waited' review, then RE-RUNS the metric
engine so negative_topic_rate + topic prevalence come alive from the fact tables.

Run:  python3 -m martin_experience.nlp_demo
"""
from __future__ import annotations

from .store import Store
from .demo_slice import build
from .services.nlp.pipeline import NlpPipeline
from .services.metrics import engine

START, END = "2026-08-01", "2026-08-31"


def main():
    store = Store(":memory:")
    build(store)
    res = NlpPipeline(store).run()
    print("NLP:", res)

    print("\nAspect sentiment for the mixed review (gr_1001):")
    eid = store.scalar("SELECT experience_event_id FROM experience_event WHERE source_record_id='gr_1001'")
    for r in store.query(
        "SELECT COALESCE(topic_id,'(document)') AS topic, sentiment_label, sentiment_score "
        "FROM sentiment_fact WHERE experience_event_id=? ORDER BY topic", [eid]):
        print(f"  {r['topic']:32} {r['sentiment_label']:8} ({r['sentiment_score']})")

    print("\nTop topics across all reviews (prevalence):")
    for r in store.query(
        "SELECT tf.topic_id, t.name, COUNT(DISTINCT tf.experience_event_id) AS events "
        "FROM topic_fact tf JOIN topic t ON t.topic_id=tf.topic_id "
        "GROUP BY tf.topic_id ORDER BY events DESC, tf.topic_id LIMIT 8"):
        print(f"  {r['topic_id']:32} {r['name']:26} events={r['events']}")

    # negative_topic_rate is now real — re-run the metric rollup
    engine.rollup_period(store, "location", START, END)
    print("\nScorecard with live negative_topic_rate:")
    for e in engine.scorecard(store, "location", START, END):
        m = e["metrics"]
        print(f"  {e['name']:26} avg={m['avg_rating']['value']:.2f}  "
              f"neg_topic%={m['negative_topic_rate']['value']*100:4.0f}")

    # verify the flagship mixed-signal case
    labels = {r["topic_id"]: r["sentiment_label"] for r in store.query(
        "SELECT topic_id, sentiment_label FROM sentiment_fact WHERE experience_event_id=? AND topic_id IS NOT NULL", [eid])}
    assert labels.get("CLIN.PHYSICIAN_COMMUNICATION") == "positive", labels
    assert labels.get("WAIT.DELAY_COMMUNICATION") == "negative", labels
    doc = store.scalar("SELECT sentiment_label FROM sentiment_fact WHERE experience_event_id=? AND topic_id IS NULL", [eid])
    assert doc == "mixed", doc
    neg_rate_downtown = next(e for e in engine.scorecard(store, "location", START, END)
                             if e["entity_id"] == "LOC_DOWNTOWN")["metrics"]["negative_topic_rate"]["value"]
    assert neg_rate_downtown > 0, "negative_topic_rate should now be > 0"
    print("\nOK — physician=positive + delay=negative in one review; document=mixed; "
          "negative_topic_rate is live.")


if __name__ == "__main__":
    main()
