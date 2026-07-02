# Kaelio (KTX) Coverage Matrix — Jarvis Reporting App

> Audit of `martin_app.html` + backend API modules against the full Kaelio docs
> (Features + Admin, all sub-pages). Status: ✅ present · 🟡 partial · ❌ missing.
> Statuses are a first pass from code inspection — items marked *(verify)* should
> be confirmed in the running app before we build.

---

## A. FEATURES

### 1. Research (conversational data chat) — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Natural-language ask → SQL → answer | ✅ | `Ask` section, `ask_history_api`, voice/chat |
| `@mentions` of data source/table/column | 🟡 *(verify)* | mention code exists; not confirmed as data-entity picker |
| SQL shown w/ copy, data tables, charts | ✅ | present in Ask + Query Panel |
| Collapsible "analysis steps" | 🟡 | thinking view exists; not step/timing breakdown |
| Chart types incl. **boxplot / waterfall** | ❌ | app has bar/line/area/pie/donut/combo/table; boxplot+waterfall missing |
| File attachments (CSV / PDF) in chat | 🟡 *(verify)* | upload code exists; not confirmed in Ask |
| "Save metric" from a response | 🟡 | Query Panel save + business rules ≈ metric save |
| Share scopes (personal/company/public) | 🟡 | reports/queries have private/group/corporate scope |
| Message feedback (thumbs up/down) | 🟡 *(verify)* | feedback code exists |
| Export conversation to PDF | 🟡 | PPTX export exists; conversation-PDF not confirmed |
| Insights carousel / suggestions | 🟡 | Pulse/connections ≈ insights |

### 2. Scheduled Researches — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Recurring automated reports | 🟡 | `automations` + new **brain** scheduler (7 skills) |
| Create from chat / from scratch | 🟡 | automations page; not the Kaelio wizard |
| Frequency/day/hour schedule | ✅ | brain cron + automations |
| **Email** report to recipients | ❌ | no SMTP send confirmed |
| Change detection vs previous run | 🟡 | brain weekly-synthesis ≈ partial |
| Execution stats / success rate | ❌ | not surfaced |
| Auto-pause after N failures | ❌ | brain has retries, not auto-pause UI |
| Run now / pause / resume | 🟡 | brain has run-now via API |

### 3. Dashboards — 🟡 strong-but-incomplete
| Capability | Status | Notes |
|---|---|---|
| Multi-page dashboards | ✅ | Designer pages + views |
| Widgets: metric card, bar, line, area, pie, table, title | ✅ | all present |
| Widgets: **waterfall, boxplot** | ❌ | missing |
| AI-generated widget from prompt | ✅ | AI design |
| Manual SQL → axis/value/label mapping | ✅ | Query widgets + editable SQL |
| Widget **history / rollback** | ❌ *(verify)* | not confirmed |
| Number/currency/%/date formatting | ✅ | incl. new K/M/B/T |
| Free placement + drag/resize + keyboard | ✅ | Designer (keyboard nudge *(verify)*) |
| **Dashboard inputs: Date Range** (`{{var}}`) | 🟡 | global filters exist; not `{{var}}` SQL substitution |
| **Dashboard inputs: Tree Select** (hierarchical, SQL-driven) | 🟡 | hierarchies exist; not as a dashboard input control |
| Comparison range (YoY/MoM) | ❌ | KPI delta exists; not an input compare range |
| **User variables `{{user.x}}`** (row security) | ❌ | no per-user security properties |
| Input **presets** | 🟡 *(verify)* | preset code exists |
| **Public share link** (expiration, alias, embed) | ❌ | sharing is scope-based, not tokenized public links |
| **Access protection** (email-OTP / password) | ❌ | missing |
| Clone / promote to company | 🟡 | scope promote partial |
| **Auto-refresh** schedule (hourly/daily/weekly) | ❌ | manual refresh only |
| Edit/preview modes + dashboard copilot | 🟡 | maximize/preview partial; copilot = voice |

### 4. Metrics — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Browse metrics (card grid, 24/page) | 🟡 | semantic metrics + business rules; not a paginated grid |
| Filter by connection / source / sort | 🟡 | partial |
| Sources: Manual / **dbt / MetricFlow** / Promoted | ❌ | manual only; no dbt/MetricFlow import |
| Metric Workshop (create/edit, admin) | 🟡 | calculated-metric editor ≈ partial |

### 5. Knowledge — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Knowledge blocks, split list + inspector | 🟡 | `knowledge` section + business rules |
| Scopes: **Global / User** | 🟡 | partial |
| Usage modes: **auto-capture** / manual / disabled | ❌ | auto-capture missing |
| Search / filter | ✅ | present |
| Bulk actions (set-mode, **compact**, promote, delete) | ❌ | compact/promote missing |
| Usage count + version history | 🟡 *(verify)* | partial |

### 6. Evaluations — ❌ missing
| Capability | Status | Notes |
|---|---|---|
| Define test prompts + expected output | ❌ | no evaluations UI |
| Run single / batch, pass-fail time series | ❌ | — |

---

## B. ADMIN

### 7. Data Sources — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Connections card grid + search | ✅ | `connect` (connections_api, airbyte) |
| Add-connection gallery by category | 🟡 | partial |
| Types: PG/MySQL/SQLServer/SQLite/Snowflake/**BigQuery**/Redshift | 🟡 | BigQuery ✅; others partial |
| BI tools: Metabase / Looker / dbt; Slack | ❌ | Looker compare exists; imports missing |
| Detail tabs: Overview/Schema/**Security**/Settings | 🟡 | Schema ✅; Security tab ❌ |
| Schema management (scan, graph view) | ✅ | Schema Layer / ERD |
| Connection settings (enable-for-chat, scan schedule) | 🟡 | partial |
| **Network proxy** (SSH tunnel / Tailscale) | ❌ | missing |
| **Metabase integration** (import questions) | ❌ | missing |

### 8. SQL Security Policies — ❌ missing (entirely)
YAML policies · table access · **column filtering** · **row-level security** ·
`default_allow_tables` · glob patterns · conditions · template vars · enforce
toggle · test/preview. None present.

### 9. SQL Reviews — ❌ mostly missing
List of AI SQL · filters (status/complexity/connection/table/tag/date) · keyboard
nav · inspector · **approve/reject/mark-reviewed** · context-injection · deep link.
(Query history exists; the review/approval workflow does not.)

### 10. Jobs — ❌ missing
Background-job dashboard (schema scan, metabase sync, AI insights, eval run,
knowledge backfill, embeddings refresh) with progress. Not surfaced.

### 11. Users — 🟡 partial
| Capability | Status | Notes |
|---|---|---|
| Users table, status filter, roles (Admin/User) | 🟡 | `users_api`, admin section |
| Edit profile + enable/disable + delete | 🟡 | partial |
| **Security properties** (key/value for row security) | ❌ | missing |
| **Invite** user (send/resend/revoke) | ❌ *(verify)* | not confirmed |
| **Groups** tab (view-access control) | ❌ *(verify)* | not confirmed |

### 12. Usage Analytics — 🟡 mostly missing
Date-range · summary cards (conversations/messages/active users) · activity chart ·
top users · messages-by-source · AI-capabilities · feedback donut.
(`usage_log` exists on the backend; no analytics UI.)

### 13. Settings — 🟡 partial
| Section | Status | Notes |
|---|---|---|
| **Appearance** (logo, mascot, brand colors, fonts) | 🟡 | OH brand themes ≈ partial |
| General (research mode, simple mode, user-scoped knowledge, SQL batch) | ❌ | missing |
| Scheduler (enable, interval) | 🟡 | brain scheduler exists; no settings UI |
| Dashboard refresh concurrency/timeout | ❌ | missing |
| AI analysis word limits | ❌ | missing |
| **Email** (SMTP status + test) | ❌ | missing |
| Maintenance (server logs, chat export, agent-tools tester) | ❌ | missing |
| **API keys** (create, `X-API-Key`) | 🟡 *(verify)* | key code exists |
| Advanced (raw JSON config) | ❌ | missing |
| Jobs (knowledge backfill) | ❌ | missing |
| Misc (server info, embeddings refresh) | ❌ | missing |

---

## C. Biggest gaps (net-new, highest effort)
1. **SQL security policies** + per-user security properties + `{{user.x}}` row security.
2. **Public share links** with expiration/alias/embed + email-OTP/password protection.
3. **Evaluations** feature (prompt tests + pass/fail tracking).
4. **SQL Reviews** approval workflow.
5. **Jobs** dashboard + **Usage analytics** dashboard.
6. **Scheduled-research email delivery** + change detection + auto-pause.
7. Dashboard **parameterized inputs** (`{{var}}` date-range + tree-select) + **presets** + **auto-refresh**.
8. **Waterfall + Boxplot** charts.
9. **Metrics** sources (dbt/MetricFlow) + a proper metrics browse grid.
10. **Settings** suite (appearance/branding, email, API keys, maintenance).

## D. Suggested build phases
- **Phase 1 (visible wins, low risk):** waterfall + boxplot charts; dashboard auto-refresh; a Metrics browse grid; conversation → PDF export.
- **Phase 2 (parameterization):** dashboard inputs (`{{var}}` date-range + tree-select) + presets; comparison ranges.
- **Phase 3 (sharing & security):** public tokenized share links (expiration/alias/embed) + email-OTP/password protection.
- **Phase 4 (governance):** SQL security policies + user security properties + `{{user.x}}`; SQL Reviews approval workflow.
- **Phase 5 (ops):** Jobs dashboard, Usage analytics, Settings suite, Evaluations, scheduled-research email.
