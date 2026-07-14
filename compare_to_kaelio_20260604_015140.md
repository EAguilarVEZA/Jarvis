# Jarvis (KTX) vs Kaelio — A/B Benchmark

Generated: 2026-06-04T01:51:40
Questions: 8

---

## Summary

| # | Question | Jarvis status | Jarvis table | Jarvis time | Kaelio status | Kaelio table | Kaelio time |
|---|---|---|---|---|---|---|---|
| 1 | Top campaigns by clicks last 90 days | ✅ | `ad_performance` | 8.9s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 2 | Lead source conversion analysis | ✅ | `data_mart_er_leads` | 7.7s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 3 | Spend by service line last 90 days | ✅ | `campaigns` | 6.7s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 4 | CTR by channel last 90 days | ✅ | `combined_campaign_stats_dashboard` | 8.4s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 5 | Top campaigns by leads last 90 days | ✅ | `combined_campaign_stats` | 9.0s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 6 | Pediatrics campaign performance last 90 days | ✅ | `combined_campaign_stats` | 7.0s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 7 | What was our total marketing revenue for fiscal ye | ❌ 422 | `—` | 1.8s | _(fill in)_ | _(fill in)_ | _(fill in)_ |
| 8 | Cost per lead by service line last 90 days | ❌ 422 | `—` | 2.0s | _(fill in)_ | _(fill in)_ | _(fill in)_ |

---

## Per-question detail

### Q1. Top campaigns by clicks last 90 days

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 8.88s
- Primary table: `ad_performance`
- Dimensions: `campaigns.campaign_name`
- Metrics: `ad_performance.clicks`
- Row count: 50

**Headline:** Orlando Health Brand dominates clicks; top 3 campaigns drive 882K of 2M+ total.

**Summary:** The Orlando Health Brand campaign led with 451,260 clicks over 90 days, followed by its paid search variant (218,661) and Baptist Health Brand (212,320). Display advertising comprises the bulk of the long tail, with individual campaigns ranging from 13K–48K clicks. Brand awareness campaigns significantly outperform specialty and recruitment initiatives.

**Key findings:**

- Top campaign (Orlando Health Brand - Brand) captured 451,260 clicks; top 3 campaigns account for 882,241 clicks (~42% of visible volume)
- Paid Search subset underperforms Display: only 4 paid search campaigns in top 30, with largest at 218,661 clicks vs. Display campaigns at 14K–96K range
- Recruitment (Talent Acquisition) campaigns cluster at 14K–17K clicks each; lowest performer among categorical groups despite multi-channel presence

**Recommendation:** Reallocate budget from low-performing specialty display campaigns (14K–25K clicks) toward scaling the top 3 brand campaigns and testing paid search expansion, where the single variant achieved 48% of parent campaign volume.

**Top 5 rows:**

| campaign_name | clicks |
|---|---|
| Orlando Health Brand - Brand | 451,260 |
| [Paid Search] Orlando Health Brand - Brand - 5980 | 218,661 |
| Baptist Health - Brand | 212,320 |
| [Display] OHMG - FMC - 5978 | 96,044 |
| OHMG - FMC | 56,259 |

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q2. Lead source conversion analysis

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 7.67s
- Primary table: `data_mart_er_leads`
- Dimensions: `data_mart_er_leads.lead_source`, `data_mart_er_leads.lead_medium`, `data_mart_er_leads.lead_type`
- Metrics: `data_mart_er_leads.leads`
- Row count: 12

**Headline:** Google paid search dominates with 519 leads; appointment requests are highest-value type.

**Summary:** Google paid search generates 519 of 678 total leads (77%), split between Appointment Requests (320) and Download Guides (160). Appointment Requests are the strongest lead type across all sources, but YouTube and Bing are negligible (<2% of volume combined). Google Display contributes 140 leads but skews toward lower-intent actions like Guide Downloads.

**Key findings:**

- Google paid search: 519 leads (77% of total) with Appointment Requests as primary conversion (320 leads)
- Appointment Requests outperform other lead types 3:1 vs Download Guides; Newsletter signups <2% across all channels
- YouTube and Bing combined: 19 leads (3%), indicating underinvestment or poor targeting in video and secondary search

**Recommendation:** Increase Google paid search budget for Appointment Request campaigns; audit YouTube targeting and consider pausing Bing until performance data justifies spend.

**Top 5 rows:**

| lead_source | lead_medium | lead_type | leads |
|---|---|---|---|
| google | paid search | Appointment Request | 320 |
| google | paid search | Download Guide | 160 |
| google | display | Download Guide | 136 |
| google | paid search | General Inquiry | 29 |
| youtube | video | General Inquiry | 15 |

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q3. Spend by service line last 90 days

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 6.75s
- Primary table: `campaigns`
- Dimensions: `campaigns.service_line`
- Metrics: `campaigns.campaign_budget`
- Row count: 35

**Headline:** Talent Acquisition dominates spend at $963; 9 of 35 service lines active.

**Summary:** Total tracked spend is $2,559.10 over 90 days, heavily concentrated in three service lines. Talent Acquisition accounts for 37.6% of all spend, while 26 service lines show null or zero allocation, indicating incomplete budget capture or inactive campaigns.

**Key findings:**

- Talent Acquisition ($963.00) represents 37.6% of total spend; next highest is Orlando Health Brand at $521.00 (20.4%)
- Top 3 service lines (Talent Acquisition, Orlando Health Brand, Baptist Health) account for $1,844.91, or 72% of all spend
- 26 of 35 rows show null values, suggesting data gaps, inactive campaigns, or incomplete period coverage for multiple service lines

**Recommendation:** Audit null-value service lines to confirm whether they are truly inactive or represent missing data; clarify budget allocation rules and reconcile with actual campaign calendar.

**Top 5 rows:**

| service_line | campaign_budget |
|---|---|
| Talent Acquisition | 963 |
| Orlando Health Brand | 521 |
| Baptist Health | 360.91 |
| Longwood FSED | 299 |
| Women's Institute | 189.96 |

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q4. CTR by channel last 90 days

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 8.40s
- Primary table: `combined_campaign_stats_dashboard`
- Dimensions: `combined_campaign_stats_dashboard.channel`
- Metrics: `combined_campaign_stats_dashboard.clicks`, `combined_campaign_stats_dashboard.impressions`
- Row count: 4

**Headline:** Paid Search vastly outperforms display and social on efficiency metrics.

**Summary:** Google Paid Search achieved 11.4% CTR versus 4.6% for Display and 0.9% for Facebook over 90 days. The 'no match' category (likely unattributed traffic) shows 0.004% CTR across 2.8B impressions and should be investigated for tracking gaps.

**Key findings:**

- Google Paid Search: 11.4% CTR (647K clicks / 5.7M impressions) — highest performer by 2.5x
- Google Display: 4.6% CTR (2.1M clicks / 45.3M impressions) — solid volume but half the efficiency
- Facebook Paid Social: 0.9% CTR (526K clicks / 57.4M impressions) — lowest performing despite largest impression volume

**Recommendation:** Shift budget allocation toward Paid Search. Audit 'no match' tracking (2.8B impressions at 0.004% CTR) to recover addressable inventory.

**Top 5 rows:**

| channel | clicks | impressions |
|---|---|---|
| Google Display | 2,086,420 | 45,338,860 |
| Google Paid Search | 647,020 | 5,681,680 |
| Facebook Paid Social | 525,660 | 57,417,220 |
| no match | 100,340 | 2,838,255,660 |

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q5. Top campaigns by leads last 90 days

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 9.02s
- Primary table: `combined_campaign_stats`
- Dimensions: `combined_campaign_stats.campaign_name`
- Metrics: `combined_campaign_stats.leads`
- Row count: 50

**Headline:** 20,360 unattributed leads dominate; Paid Search brand campaigns drive 2,120 attributed leads.

**Summary:** Over the last 90 days, 20,360 leads lack campaign attribution, representing ~73% of total volume. Named campaigns generated 2,120 leads, with Paid Search ORMC Brand (760), Bayfront ENT&A (740), and Bayfront Brand (620) as top three. Paid Search outperforms Display 4:1 in attributed lead volume.

**Key findings:**

- 20,360 null-campaign leads suggest major tracking or attribution gap—likely 50%+ of actual campaign ROI unmeasured
- Top 3 named campaigns account for 2,120 leads (78% of attributed total); heavy concentration in Bayfront and ORMC brand initiatives
- Paid Search (Paid Search) consistently outperforms Display by ~4x per comparable facility/specialty (e.g., Bayfront Brand: 620 vs. untracked Display: 0 direct comparison)

**Recommendation:** Audit UTM tagging and CRM integration to recapture the 20,360 orphaned leads, then reallocate budget toward top-performing Paid Search hospital/brand combinations (ORMC, Bayfront) while evaluating Display channel ROI.

**Top 5 rows:**

| campaign_name | leads |
|---|---|
| — | 20,360 |
| [Paid Search] ORMC - Brand - 5980 | 760 |
| [Paid Search] Bayfront - ENT&A - 5978 | 740 |
| [Paid Search] Bayfront - Brand - 5932 | 620 |
| [Paid Search] Dr. Phillips Hospital - ER - 5932 | 540 |

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q6. Pediatrics campaign performance last 90 days

**Jarvis (KTX)**

- Status: ✅ ok
- Time: 6.96s
- Primary table: `combined_campaign_stats`
- Dimensions: `combined_campaign_stats.campaign_name`, `combined_campaign_stats.date`
- Metrics: `combined_campaign_stats.impressions`, `combined_campaign_stats.clicks`, `combined_campaign_stats.cost`, `combined_campaign_stats.leads`
- Row count: 0

**Headline:** No pediatrics campaign data available for the last 90 days.

**Summary:** The query returned zero records. Either no pediatrics campaigns ran in the last 90 days, the campaigns exist under a different naming convention, or data hasn't been logged in the system. Immediate verification of campaign naming, date range, and data pipeline status is required.

**Key findings:**

- Zero rows returned—no measurable impressions, clicks, cost, or leads data exists
- Cannot assess performance, ROI, or campaign effectiveness without baseline data
- Potential data integrity issue or campaign gap that needs investigation

**Recommendation:** Verify campaign naming conventions, confirm 90-day date range is correct, and audit data pipeline to confirm pediatrics campaign data is being captured.

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q7. What was our total marketing revenue for fiscal year 2024?

**Jarvis (KTX)**

- Status: ❌ 422
- Time: 1.84s
- Primary table: `—`
- Dimensions: —
- Metrics: —
- Row count: —

**Error:** `HTTPError` — {"error":"could not build query","detail":"No join path between 'revenue_attributed_to_marketing' and 'date'. Phase 1 supports only directly-joined tables."}

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---

### Q8. Cost per lead by service line last 90 days

**Jarvis (KTX)**

- Status: ❌ 422
- Time: 2.05s
- Primary table: `—`
- Dimensions: —
- Metrics: —
- Row count: —

**Error:** `HTTPError` — {"error":"could not build query","detail":"No join path between 'campaigns' and 'campaign_expense'. Phase 1 supports only directly-joined tables."}

**Kaelio** _(fill in from Kaelio UI)_

- Time: _(e.g. 73s)_
- Primary table: _(e.g. `data_mart_er.combined_campaign_stats_dashboard`)_
- Headline: _
- Top 3 rows: _
- Notable differences: _

**A/B verdict**

- Same table? _yes/no_
- Same magnitude? _yes/no_
- Same categorization / scope? _yes/no_
- Brief richness gap: _

---
