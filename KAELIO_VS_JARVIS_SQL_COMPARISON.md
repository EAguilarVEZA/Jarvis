# Kaelio vs Jarvis — SQL Generation Comparison

**Question both systems answered:** "Top campaigns by clicks last 90 days"
**Date:** 2026-06-03

---

## The 5 key differences

Kaelio is doing five things Jarvis isn't (yet). All five contribute to why the answers differ.

### 1. Different physical table

| | Kaelio | Jarvis |
|---|---|---|
| Table | `oh-data-warehouse.data_mart_er.combined_campaign_stats_dashboard` | `oh-data-warehouse.gold.ad_fact_ads` LEFT JOIN `gold.ad_dim_campaign` |
| Nature | Pre-aggregated, "dashboard-ready" mart | Raw fact + dimension lookup |
| Rows | Daily campaign-level rollups | Per-impression event log |
| Required joins | None — everything in one table | 2-table join for the campaign name |

**Implication:** Kaelio's planner picked a purpose-built mart that already has channel, clicks, impressions, leads, cost, and a formatted campaign name. Jarvis's planner went to the raw event table and had to re-aggregate.

### 2. Multiple metrics returned in one query

**Kaelio's SELECT clause:**
```sql
campaign_name_formatted AS campaign,
channel,
SUM(clicks)                                         AS total_clicks,
SUM(impressions)                                    AS total_impressions,
ROUND(SUM(clicks)/NULLIF(SUM(impressions),0)*100,2) AS ctr_pct,
SUM(leads)                                          AS total_leads,
ROUND(SUM(cost),2)                                  AS total_cost
```

**Jarvis's SELECT clause:**
```sql
`campaign_name` AS `campaign_name`,
SUM(`clicks`) AS `clicks`
```

**Implication:** Kaelio gets 7 columns in one shot (campaign, channel, clicks, impressions, CTR, leads, cost). Jarvis gets 2 (campaign, clicks). Kaelio's brief has more to work with — that's why it shows the Channel column, CTR comparison, and cost rankings.

### 3. Rolling 90-day window vs. hard-coded date range

**Kaelio:**
```sql
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
```

**Jarvis:**
```sql
WHERE `date` BETWEEN '2024-09-09' AND '2024-12-07'
```

**Implication:** Kaelio's "last 90 days" is **always relative to today** — re-running the same brief tomorrow asks about a different window. Jarvis baked an absolute date range into the query — re-running tomorrow returns identical results. Today the user can't tell, but a week from now this matters.

### 4. Quality filters Kaelio applies, Jarvis doesn't

```sql
-- Kaelio adds:
AND is_digital = TRUE
AND campaign_name_formatted IS NOT NULL
AND campaign_name_formatted != 'Leads Attributed to Other Campaigns'
```

**Implication:** Without these filters, Jarvis's top results may include non-digital campaigns, NULL-name buckets, and a generic "other campaigns" catchall — which is exactly the kind of thing that drives weird leaderboards.

### 5. Multi-query brief vs. single-query brief

Kaelio actually ran **at least two queries** to build the "Top campaigns by clicks" brief:

**Query A — Top 25 campaigns** (full multi-metric table, shown above)

**Query B — Channel mix breakdown:**
```sql
SELECT channel, SUM(clicks) AS total_clicks
FROM `oh-data-warehouse`.`data_mart_er`.`combined_campaign_stats_dashboard`
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  AND is_digital = TRUE
GROUP BY channel
ORDER BY total_clicks DESC
```

This is what powers Kaelio's "Channel Mix" sub-section (Google Paid Search 66%, Facebook Paid Social 11%, Google Display 5%). Kaelio's `Analysis: 7 tools, 1 thinking step` footer confirms multiple tool calls.

**Implication:** Kaelio's planner decomposes a question into multiple sub-queries and weaves the results into a structured brief. Jarvis fires one query and asks the LLM to write a brief from it. This is the largest architectural gap.

---

## Side-by-side SQL

### Kaelio (master) — multi-query approach

```sql
-- Query A — Top 25 campaigns with channel, CTR, leads, cost
SELECT
  campaign_name_formatted AS campaign,
  channel,
  SUM(clicks)                                         AS total_clicks,
  SUM(impressions)                                    AS total_impressions,
  ROUND(SUM(clicks)/NULLIF(SUM(impressions),0)*100,2) AS ctr_pct,
  SUM(leads)                                          AS total_leads,
  ROUND(SUM(cost),2)                                  AS total_cost
FROM `oh-data-warehouse`.`data_mart_er`.`combined_campaign_stats_dashboard`
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  AND is_digital = TRUE
  AND campaign_name_formatted IS NOT NULL
  AND campaign_name_formatted != 'Leads Attributed to Other Campaigns'
GROUP BY campaign_name_formatted, channel
ORDER BY total_clicks DESC
LIMIT 25;

-- Query B — Channel mix
SELECT channel, SUM(clicks) AS total_clicks
FROM `oh-data-warehouse`.`data_mart_er`.`combined_campaign_stats_dashboard`
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  AND is_digital = TRUE
GROUP BY channel
ORDER BY total_clicks DESC;
```

### Jarvis — single-query approach

```sql
SELECT
  `campaign_name` AS `campaign_name`,
  SUM(`clicks`)   AS `clicks`
FROM `oh-data-warehouse`.`gold`.`ad_fact_ads` AS `ad_performance`
LEFT JOIN `oh-data-warehouse`.`gold`.`ad_dim_campaign` AS `campaigns`
  ON `ad_performance`.`campaign_id` = `campaigns`.`campaign_id`
WHERE `date` BETWEEN '2024-09-09' AND '2024-12-07'
GROUP BY 1
ORDER BY `clicks` DESC
LIMIT 50;
```

---

## What this implies for the Ask AI roadmap

Bringing Jarvis to Kaelio parity needs three things, in order:

### A. Connect the missing datasets (blocker #1)

Without `data_mart_er` and `data_mart_all`, Jarvis literally **cannot** generate Kaelio-style SQL because the table the planner needs doesn't exist in its semantic layer. See `KAELIO_VS_JARVIS_TABLE_INVENTORY.md` — 33 tables missing across 4 datasets.

### B. Make the planner prefer marts over raw facts (small change)

After the marts are connected, update the planning prompt in `semantic_api.py /ask` so the LLM prefers tables tagged as "mart" / "summary" / "dashboard" over raw fact tables for top-N questions. The model already sees table descriptions — a one-line system prompt nudge ("prefer pre-aggregated marts when answering top-N or summary questions") gets us 80% of the way.

### C. Decompose questions into multiple sub-queries (architectural)

This is the bigger lift. Today `/api/semantic/ask` builds **one** `StructuredQuery` from the user prompt and runs it. To match Kaelio's depth, the planner needs to emit a **plan** — a list of 2-5 sub-questions — each gets its own query, and the brief generator weaves them into the rich output (main table + channel mix + benchmark comparison + strategic implications).

Concrete shape: instead of `ask → query → brief`, the flow becomes:
```
ask
 ↓
plan_subquestions  (Haiku — emits 2-5 StructuredQueries)
 ↓
run_all_in_parallel
 ↓
synthesize_brief   (Sonnet — sees all results + writes structured brief with sections)
```

This matches Kaelio's "12 tools, 1 thinking step" pattern.

---

## Quality-filter checklist Jarvis should adopt

Even with the same table, Kaelio's quality filters dramatically improve answer relevance. Worth adding to the semantic layer as default `business_rules`:

| Filter | What it does | How to implement in Jarvis |
|---|---|---|
| `is_digital = TRUE` | Excludes offline / radio / direct mail | Add as a business rule on the campaign table |
| `campaign_name IS NOT NULL` | Drops headerless data | Auto-applied by `/api/semantic/query` |
| `campaign_name != 'Leads Attributed to Other Campaigns'` | Drops generic bucket | Add as business rule |
| `DATE_SUB(CURRENT_DATE(), INTERVAL N DAY)` | Rolling window | Update `_resolve_date_range` in `semantic_api.py` to emit relative dates when prompt says "last N days" |
