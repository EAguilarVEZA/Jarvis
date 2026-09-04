"""
Metric engine demo (spec §11.7, §18). Builds the canonical slice, rolls a period up into
metric_observation, prints the unified scorecard, and runs a period-over-period comparison.

Run:  python3 -m martin_experience.metrics_demo
"""
from __future__ import annotations

from .store import Store
from .demo_slice import build
from .services.metrics import engine

FULL_START, FULL_END = "2026-08-01", "2026-08-31"
PRIOR_START, PRIOR_END = "2026-08-01", "2026-08-07"
CUR_START, CUR_END = "2026-08-08", "2026-08-31"


def main():
    store = Store(":memory:")
    build(store)  # source → raw → canonical gold

    wrote = engine.rollup_period(store, "location", FULL_START, FULL_END)
    wrote2 = engine.rollup_period(store, "location", FULL_START, FULL_END)  # idempotent
    total = store.scalar("SELECT COUNT(*) FROM metric_observation")
    print(f"metric_observation rows written: {wrote} (re-run wrote {wrote2}); total in table: {total}")

    print("\nUnified scorecard — locations, Aug 2026:")
    for e in engine.scorecard(store, "location", FULL_START, FULL_END):
        m = e["metrics"]
        print(f"  {e['name']:26} n={m['review_count']['value']:>2.0f}  "
              f"avg={m['avg_rating']['value']:.2f}  "
              f"top5%={m['top_box_rate']['value']*100:4.0f}  "
              f"bottom≤2%={m['bottom_box_rate']['value']*100:4.0f}  "
              f"neg_topic%={m['negative_topic_rate']['value']*100:4.0f}")

    print("\nPeriod-over-period — avg_rating at Downtown (early Aug vs later Aug):")
    cmp = engine.compare_periods(store, "avg_rating", "location", "LOC_DOWNTOWN",
                                 CUR_START, CUR_END, PRIOR_START, PRIOR_END)
    print(f"  current={cmp['current']} (n={cmp['n_current']})  "
          f"prior={cmp['comparison']} (n={cmp['n_comparison']})  "
          f"rel_change={cmp['relative_change']}  method={cmp['method']}")

    # verify computed values against the fixtures
    sc = {e["entity_id"]: e for e in engine.scorecard(store, "location", FULL_START, FULL_END)}
    assert sc["LOC_DOWNTOWN"]["metrics"]["review_count"]["value"] == 3
    assert round(sc["LOC_DOWNTOWN"]["metrics"]["avg_rating"]["value"], 2) == 3.33
    assert sc["LOC_WINNIE"]["metrics"]["review_count"]["value"] == 2
    assert round(sc["LOC_WINNIE"]["metrics"]["avg_rating"]["value"], 2) == 2.5
    assert total == wrote, "rollup should be idempotent (no new rows on re-run)"
    print("\nOK — metrics computed deterministically, match the source, and are idempotent.")


if __name__ == "__main__":
    main()
