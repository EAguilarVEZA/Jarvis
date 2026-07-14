# Tableau Coverage Matrix — Jarvis Reporting

> Reporting surface (Query Panel + Designer + Dashboards) vs Tableau's analyst core.
> Status: ✅ shipped · 🟢 shipped-lightweight · 🟡 partial · ❌ missing.
> **Updated after Tableau T-Phases 1–6.**

---

## A. Data & calculation
| Tableau capability | Status | Notes |
|---|---|---|
| Dimensions / Measures shelves + drag | ✅ | Query Panel |
| **Calculated fields** (row-level formulas) | ✅ | T-2: safe parser, `[col]` refs, function palette |
| Aggregations | ✅ | metrics + semantic layer |
| **Table calculations** (running total, % of total, rank, moving avg, difference) | ✅ | T-4 |
| **Parameters** (interactive controls) | ✅ | T-3: manager + bar, bound to calc fields & `{{SQL}}` |
| LOD expressions (FIXED/INCLUDE/EXCLUDE) | ❌ | advanced; not built |
| Filters (categorical, IN, range) | ✅ | + top-N/wildcard 🟡 |
| Bins / groups / sets | ❌ | not built |
| Data blending / relationships | 🟢 | semantic joins (Schema Layer) |

## B. Marks / viz types
| Mark | Status | Mark | Status |
|---|---|---|---|
| Bar / stacked / combo | ✅ | Waterfall | ✅ (P1) |
| Line / area | ✅ | Boxplot | ✅ (P1) |
| Pie / donut | ✅ | **Scatter** | ✅ (T-1) |
| Table / KPI / funnel | ✅ | **Dual-axis** | ✅ (T-1) |
| **Treemap** | ✅ (T-1) | **Heatmap** | ✅ (T-1) |
| **Symbol map** | ✅ (T-6) | **Choropleth map** | ✅ (states + FL counties) |
| Histogram | 🟡 | Gantt | ❌ |
| Bullet | 🟡 (KPI goal pip) | City/county maps | ❌ (state-level geocode only) |

## C. Interactivity (dashboard actions)
| Capability | Status | Notes |
|---|---|---|
| Click-to-filter | ✅ | drill; now a selectable mode |
| **Filter / Highlight / Off modes** | ✅ | T-5, per-dashboard |
| **Highlight actions (brushing)** | ✅ | T-5: cross-widget highlight + banner |
| Rich tooltips | ✅ | ECharts |
| Cross-page / global filter bar | ✅ | |
| Parameter actions (click sets a param) | ❌ | not built |
| Legend-click filter / URL actions | 🟡 / ❌ | |

## D. Authoring / layout / formatting
| Capability | Status | Notes |
|---|---|---|
| Free-form layout, drag/resize | ✅ | Designer |
| Text/shapes/images/containers | ✅ | shape library, local image import |
| Number/date formatting (K/M/B/T) | ✅ | |
| Templates / themes | ✅ | 12 templates + brand themes |
| Export PPTX / PDF | ✅ | |
| Auto-refresh / live data | ✅ | P1 |
| Publish / share (public link) | ✅ | P3 |
| Multi-page ✅ / story points 🟡 | | narration guided story = partial |
| Conditional color encoding | 🟡 | some; no general color-by-measure |

## E. Governance / platform
Row-level security (`{{user.x}}`), SQL policy, SQL reviews, users/usage, scheduled
email delivery — ✅ (Kaelio-parity phases).

---

## What's LEFT / to improve (Tableau)
1. **LOD expressions** (FIXED/INCLUDE/EXCLUDE) — the biggest remaining calc gap.
2. **Bins / groups / sets** — grouping dimension values, numeric bins, ad-hoc sets.
3. **Maps depth** — city/county/ZIP geocoding + choropleth beyond states/FL; a legend/color-scale for symbol maps.
4. **Parameter actions** — click a mark to set a parameter (not just filter/highlight).
5. **Histogram / gantt / bullet** as first-class marks; **dual-axis synchronize** option.
6. **Conditional formatting** — general color-by-measure encoding on tables & marks.
7. **Story points** — guided narrative sequence over dashboard pages.
8. **Top-N / wildcard filters**, legend-click filtering.

## Bottom line
The **analyst-critical 80%** is shipped: calculated fields, parameters, table calcs,
a broad mark library incl. maps, and dashboard filter/highlight actions. Remaining is
the advanced calc tail (LOD, bins/sets), maps depth, and a few interaction niceties.
