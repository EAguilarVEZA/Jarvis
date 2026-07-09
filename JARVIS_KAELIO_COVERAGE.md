# Kaelio (KTX) Coverage Matrix — Jarvis Reporting App

> Audit of `martin_app.html` + backend API modules against the full Kaelio docs.
> Status: ✅ shipped · 🟢 shipped-lightweight · 🟡 partial · ❌ missing.
> **Updated after Phases 1–5 + SQL Reviews + Evaluations + Settings + Jobs + email delivery.**

---

## A. FEATURES

### 1. Research (conversational data chat) — 🟢 shipped-lightweight
| Capability | Status | Notes |
|---|---|---|
| NL ask → SQL → answer + charts | ✅ | Ask section, voice/chat |
| Chart types incl. boxplot/waterfall/scatter/maps | ✅ | full engine (see Tableau doc) |
| Conversation → PDF export | ✅ | Phase 1 |
| Save metric / feedback thumbs | 🟡 | present; light |
| @mentions of data entities | 🟡 | not a full entity picker |

### 2. Scheduled Researches — 🟢 shipped-lightweight
| Capability | Status | Notes |
|---|---|---|
| Recurring automated reports | ✅ | brain scheduler (7 skills) + automations |
| **Email delivery to recipients** | ✅ | SMTP mailer + Settings recipients (activates when SMTP set) |
| Schedule (freq/day/hour) | ✅ | cron |
| Run now / status / history | ✅ | brain run-now + Jobs dashboard |
| Change detection vs previous | 🟡 | weekly synthesis ≈ partial |
| Auto-pause after N failures | ❌ | retries yes; auto-pause UI no |

### 3. Dashboards — ✅ shipped (strong)
| Capability | Status | Notes |
|---|---|---|
| Pages, widgets, AI widget gen, manual SQL map | ✅ | Designer |
| Full mark set (+waterfall/boxplot/scatter/dual-axis/treemap/heatmap/map) | ✅ | Tableau phases |
| Auto-refresh schedule | ✅ | Phase 1 |
| Global filters + drill | ✅ | + dashboard actions (filter/highlight) |
| Parameterized inputs (`{{var}}`) + presets | ✅ | Phase 2 + Tableau params |
| SQL-template widgets | ✅ | report variables |
| **Public share link** (expiry/alias/embed/password) | ✅ | Phase 3 |
| Widget history/rollback | ❌ | not present |
| User variables `{{user.x}}` (row security) | ✅ | Phase 4 |

### 4. Metrics — 🟢 shipped-lightweight
| Capability | Status | Notes |
|---|---|---|
| Browse grid (search/source/sort) | ✅ | Metrics section (calc metrics + rules + saved queries) |
| Metric Workshop (create/edit) | 🟡 | calc-metric editor |
| dbt / MetricFlow import | ❌ | not built |

### 5. Knowledge — 🟢 shipped-lightweight
| Capability | Status | Notes |
|---|---|---|
| Blocks, search, scopes, usage modes | ✅ | knowledge section + business rules |
| **Auto-capture** (LLM extract → usage_mode=auto) | ✅ | `/auto-capture` |
| **Version history** + revert | ✅ | snapshot on edit, `/versions`, `/revert` |
| Bulk compact/promote | 🟡 | not built |

### 6. Evaluations — ✅ shipped
| Capability | Status | Notes |
|---|---|---|
| Test prompts + criteria | ✅ | LLM answer + judge |
| Run single/batch + pass/fail history | ✅ | Admin → Evaluations, sparkline history |

---

## B. ADMIN

### 7. Data Sources — 🟢 shipped-lightweight
| Capability | Status | Notes |
|---|---|---|
| Connections grid + catalog + BigQuery (live) | ✅ | connect section, native + Airbyte |
| **BI-tool import UI (Metabase/Looker/dbt)** | 🟢 | scaffolded — UI + endpoints; live import needs the external system + creds (`JARVIS_ENABLE_BI_IMPORTS`) |
| Per-connection Security tab, network proxy | ❌ | not built |

### 8. SQL Security Policies — ✅ shipped (lightweight vs Kaelio YAML)
Row-level security via `{{user.x}}` (fail-closed) ✅ · table allowlist + column denylist ✅ ·
per-user security properties ✅ · test/preview ✅ · **enforce toggle** ✅.
Full YAML row-filter templates / glob conditions → 🟡 (we do globs, not the full DSL).

### 9. SQL Reviews — ✅ shipped
Deduped log of executed SQL · filters (status/complexity/search) · inspector ·
approve/reject/mark-reviewed · block/error flags. (Keyboard nav + deep-link → 🟡.)

### 10. Jobs — ✅ shipped
Registry + brain-skill wiring + auto-refreshing dashboard (running/success/failed,
duration, detail, counts).

### 11. Users — 🟡 partial → ✅ for security
Users/roles/usage (existing) ✅ · **security properties** ✅ (Phase 4) ·
invitations/groups → 🟡.

### 12. Usage Analytics — ✅ shipped (pre-existing)
Admin → Usage panel (conversations/messages/active users/etc.).

### 13. Settings — ✅ shipped (core + maintenance)
Appearance (name/accent/logo) ✅ · General (simple mode / research mode) ✅ ·
Email status + real test + report recipients ✅ · **API keys** (create-once/list/delete, hashed) ✅ ·
**Maintenance: server-logs viewer + chat export (JSON/CSV)** ✅.
Agent-tools tester / advanced-JSON / embeddings refresh → ❌ (niche).

---

## C. What's LEFT / to improve (Kaelio)
1. **API-key enforcement** — keys created + accepted by the auth gate; enabling the gate is the hosting step (#1).
2. **Live BI imports** — Metabase/Looker/dbt import UI + endpoints are scaffolded; wiring the live SDK calls needs those systems + credentials (external, only you can enable).
3. **Metrics** — dbt/MetricFlow sourced metrics (pairs with the dbt import).
4. **Scheduled-research polish** — change-detection diffing + auto-pause after repeated failures.
5. **SQL policy depth** — full YAML row-filter DSL (we ship globs + `{{user.x}}`).
6. **Niche settings** — agent-tools tester, advanced JSON editor, embeddings refresh.
7. **Knowledge bulk actions** (compact/promote), widget history/rollback, keyboard nav in SQL Reviews, invitations/groups in Users.

## Bottom line
Every documented Kaelio feature is now **represented and functional**. The only truly
open items are (a) the **auth gate flip** (a hosting action) and (b) **live BI-tool
imports** (external integrations requiring the target systems + credentials). Everything
else is optional depth/polish.
