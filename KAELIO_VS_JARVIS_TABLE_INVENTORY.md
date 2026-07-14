# Kaelio vs Jarvis — Table Inventory & Gap Analysis

**Generated:** 2026-06-03
**Master:** Kaelio (oh-next.kaelio.com) — Orlando Health production
**Compared:** Jarvis local (`oh-data-warehouse` BigQuery via KTX)

---

## Headline

Kaelio is wired to **57 BigQuery tables across 5 datasets**. Jarvis currently sees **24 tables in 1 dataset (`gold`)**. The critical gap: Jarvis has **none of the lead / pipeline / revenue / report tables** — which is why "Lead source conversion analysis" returns `422 could not build query`.

| | Kaelio (master) | Jarvis | Gap |
|---|---|---|---|
| Datasets connected | 5 BigQuery + 1 Slack | 1 BigQuery | **4 datasets missing** |
| Total tables | 57 | 24 | **33 tables missing** |
| Lead/pipeline tables | 6 (in `data_mart_er`, `data_mart_all`) | 0 | **6 missing** |
| Revenue/ROI tables | 5 (`roi_exercise`) | 0 | **5 missing** |
| Report tables | 13 (`data_mart_all`) | 0 | **13 missing** |
| Campaign aggregates | 4 (`data_mart_er`, `data_mart_facilities`) | 0 | **4 missing** |

---

## Connection-by-connection inventory

### 1. `gold` (Digital Advertising Analytics) — Kaelio uses 18 enabled / 24 total

| Table | Kaelio | Jarvis |
|---|:---:|:---:|
| `ad_dim_account` | ✅ | ✅ |
| `ad_dim_ad` | ✅ | ✅ |
| `ad_dim_ad_platform` | ✅ | ✅ |
| `ad_dim_adgroup` | ✅ | ✅ |
| `ad_dim_campaign` | ✅ | ✅ |
| `ad_fact_ads` | ✅ | ✅ |
| `ad_summary_ad_networks` | ✅ | ✅ |
| `dim_date` | ✅ | ✅ |
| `er_wait_time` | ✅ | ✅ |
| `er_wait_time_mockup` | ✅ | ✅ |
| `ga4_dim_event_params` | ✅ | ✅ |
| `ga4_dim_sessions` | ✅ | ✅ |
| `ga4_dim_users_pseudonymous` | ✅ | ✅ |
| `ga4_dim_webpage` | ✅ | ✅ |
| `ga4_fact_events` | ✅ | ✅ |
| `gsc_fact_site_impression` | ✅ | ✅ |
| `gsc_summary_monthly_site_impression` | ✅ | ✅ |
| `sample_flat_ad_fact` | ✅ | ✅ |
| `campaign_dim_ad` | (disabled) | ✅ |
| `campaign_dim_adgroup` | (disabled) | ✅ |
| `campaign_dim_campaign` | (disabled) | ✅ |
| `campaign_dim_customer` | (disabled) | ✅ |
| `campaign_fact_ads` | (disabled) | ✅ |
| `campaign_summary_ad_networks` | (disabled) | ✅ |

**Status:** ✅ Full parity. Jarvis even has the 6 `campaign_*` tables Kaelio has disabled.

---

### 2. `roi_exercise` (ROI / Revenue Attribution) — Kaelio uses 5 enabled

| Table | Kaelio | Jarvis |
|---|:---:|:---:|
| `revenue_attributed_to_marketing` | ✅ | ❌ |
| `dim_date` | ✅ | ❌ (only has gold.dim_date) |
| `marketing_operation_expense` | ✅ | ❌ |
| `campaign_expense` | ✅ | ❌ |
| `net_charges` | ✅ | ❌ |

**Status:** ❌ Entirely missing. This is the dataset that lets Kaelio answer "marketing attribution revenue from October," "fiscal year 2024 revenue," "campaign ROI" questions.

---

### 3. `data_mart_er` (Emergency Dept. + Leads) — Kaelio uses 8 enabled

| Table | Kaelio | Jarvis |
|---|:---:|:---:|
| `combined_campaign_stats` | ✅ | ❌ |
| `combined_campaign_stats_dashboard` | ✅ | ❌ |
| `digital_ads` | ✅ | ❌ |
| `digital_ads_w_leads` | ✅ | ❌ |
| `digital_ads_w_leads_dashboard` | ✅ | ❌ |
| `er_facilities_region` | ✅ | ❌ |
| `leads` | ✅ | ❌ |
| `paid_keywords_w_leads` | ✅ | ❌ |

**Status:** ❌ Entirely missing. This is why "Lead source conversion analysis" failed — the `leads` table lives here.

---

### 4. `data_mart_facilities` (Facility-level rollups) — Kaelio uses 3 enabled

| Table | Kaelio | Jarvis |
|---|:---:|:---:|
| `combined_campaign_stats` | ✅ | ❌ |
| `combined_campaign_stats_ER` | ✅ | ❌ |
| `view_er_gsc` | ✅ | ❌ |

**Status:** ❌ Entirely missing.

---

### 5. `data_mart_all` (Big Query DWH — Marketing & Lead Gen) — Kaelio uses 23 enabled

| Table | Kaelio | Jarvis |
|---|:---:|:---:|
| `combined_campaign_stats` | ✅ | ❌ |
| `digital_ads` | ✅ | ❌ |
| `digital_ads_w_leads` | ✅ | ❌ |
| `dim_date` | ✅ | ❌ |
| `efficiency_media_campaigns` | ✅ | ❌ |
| `leads` | ✅ | ❌ |
| `paid_keywords_w_leads` | ✅ | ❌ |
| `report_combined_campaign_stats` | ✅ | ❌ |
| `report_digital_ads` | ✅ | ❌ |
| `report_digital_ads_w_leads` | ✅ | ❌ |
| `report_efficiency_media_campaigns` | ✅ | ❌ |
| `report_executive_summary_view` | ✅ | ❌ |
| `report_leads` | ✅ | ❌ |
| `report_paid_keywords_w_leads` | ✅ | ❌ |
| `report_v2_combined_campaign_stats` | ✅ | ❌ |
| `report_v2_digital_ads` | ✅ | ❌ |
| `report_v2_keywords_w_leads` | ✅ | ❌ |
| `report_v2_leads` | ✅ | ❌ |
| `report_yext_metrics` | ✅ | ❌ |
| `sfmc_metrics` | ✅ | ❌ |
| `view_entity_ids` | ✅ | ❌ |
| `view_web_exec_summary` | ✅ | ❌ |
| `view_yext_entities` | ✅ | ❌ |

**Status:** ❌ Entirely missing. Contains nearly all the "report_*" tables Kaelio leans on for its briefs.

---

## Why this matters — root cause of the campaign-name mismatch

When you asked both systems "Top campaigns by clicks last 90 days":

- **Kaelio** picked `data_mart_all.combined_campaign_stats` or `data_mart_er.digital_ads` (rich aggregate with channel + CTR + cost). It got ER-focused campaigns because that table joins to keyword/ER lead data.
- **Jarvis** had only `gold.ad_fact_ads` to use (raw event-level), so it grouped by `campaign_name` and got brand-umbrella names.

**Same data warehouse, different physical tables, different answers.** Kaelio is querying pre-aggregated marts; Jarvis is querying raw fact tables and re-aggregating live.

---

## Action plan to close the gap

To bring Jarvis to parity, in priority order:

1. **Add `roi_exercise` dataset connection** → unlocks revenue attribution & FY queries (5 tables).
2. **Add `data_mart_er` dataset connection** → unlocks leads & ER attribution questions, fixes the "Lead source conversion" failure (8 tables).
3. **Add `data_mart_all` dataset connection** → unlocks all the `report_*` pre-aggregates Kaelio relies on for briefs (23 tables).
4. **Add `data_mart_facilities` dataset connection** → unlocks facility-level rollups (3 tables).
5. **Auto-curate each** through the Curate tab so the AI has friendly names + measures.

**Mechanical steps:**

- For each missing dataset:
  - In Jarvis Connect tab → "+ Add connection" → BigQuery
  - Project: `oh-data-warehouse` (same as current)
  - Dataset: `roi_exercise` / `data_mart_er` / `data_mart_all` / `data_mart_facilities`
  - Use the existing service account JSON at `~/.config/jarvis/bigquery-key.json`
  - In Curate tab → "Curate all remaining" against the new dataset

Once all 4 datasets are connected and curated, both systems will be querying the same physical tables, and Jarvis Ask AI answers will line up with Kaelio's.
