# Ours Privacy × Smart-with-Martin — Integration & Feature Plan

How we plug our governed analytics + agentic layer into Ours Privacy's HIPAA-compliant CDP so their existing customers gain deep, warehouse-native analytics — and what we should build **now** to be ready.

> Research basis: Ours Privacy marketing site + live docs (`docs.oursprivacy.com`), Rock Health, AdExchanger, PRWeb; plus composable-CDP / partner-integration references (Segment, RudderStack, Hightouch, Census/Fivetran, Snowflake, Freshpaint) and HHS HIPAA guidance. Full source list at the end.

---

## 1. Who Ours Privacy is (and why we fit)

Ours Privacy is a **HIPAA-compliant, server-side CDP + privacy platform for healthcare marketers** — spun out of the telehealth couples-counseling startup "Ours" (legal entity *Ours Wellness, Inc.*, Houston TX), funded July 2025 in a round led by **Rock Health** (with Switch Ventures, GreyMatter, TMV). Founders Jessica Holton, Adam Putterman, Tyler Zey (CTO).

**What their platform does, end to end:** one script replaces all third-party pixels with **secure server-side connections**; events flow Source → **Event Allowlist** → optional **Custom Mapper** (rename/hash/redact/null fields, conditional dispatch) → **Destinations**, with a **Global Consent Center gating dispatch in real time**. They strip PHI before it reaches non-BAA tools, and act as the **BAA-backed proxy** in front of GA4, Google/Meta/TikTok ads, etc.

**The strategic gap we fill:** their own analytics suite (Web Analytics as a GA3/GA4 replacement, channel-level multi-touch attribution, session replay, funnels/Journeys, heatmaps, Bayesian A/B testing) is **marketing-ops-grade and dashboard-shaped**. It is deliberately *not* a warehouse-native modeling layer, not deep product/cohort analytics, not statistical MMM, and not an automation/agent platform. Tellingly, **they forward data to BigQuery, Snowflake, Databricks, Mixpanel, Amplitude rather than replicate deep analytics themselves.** That forwarding is our doorway: we are the governed semantic layer + agentic analytics + workflow engine that sits on the compliant data they already produce.

**One-line thesis:** *Ours Privacy is the compliant data pipe and activation layer; we are the analytics brain and automation layer on top of it. They capture and govern; we model, explain, forecast, and act.*

---

## 2. What we already have that complements them

Grounded in our actual codebase (`*_api.py` routers + capability modules):

- **BigQuery-native semantic layer** (`semantic_api.py`, `semantic/`) — tables, typed relationships + cardinality, topics/subject areas, governed **Metric Lab** (`metric_lab.py`: measure + aggregation + time grain + fixed filters + format + direction → deterministic BigQuery SQL). *Their warehouse destinations are BigQuery/Snowflake — we run natively where their data lands.*
- **Metric-grounded AI agent** (`/metric_ask`) — NL question → the model picks a **governed** metric and the compiler runs it (anti-hallucination). Now enriched with business-context descriptions + synonyms (the "wiki" layer).
- **Model self-validation** (`/validate`) + generate-model-from-table + auto-suggested joins — fast, safe modeling of a new data source (i.e., *their* event tables).
- **Pulse-style monitoring** — deterministic facts (change/trend/anomaly/forecast/goal) + AI digest, reusing a Holt-Winters/linear forecaster with prediction bands.
- **Experimentation engine** (`experiments_api.py`) — A/B stats, **CUPED** variance reduction, **synthetic control**, permutation/robustness, segment scans, forecasting, driver analysis. *Deeper than their Bayesian A/B surface, and complementary (we model; they assign variants server-side).*
- **Workflow automation** (`workflows_api.py`) — n8n-style graph engine with connectors, HTTP, branching, retries, **MCP nodes**, schedules. *Turns insights into compliant actions.*
- **Governed MCP servers** — `semantic_mcp.py` (governed metrics as MCP tools) + `marketplace_mcp.py`. *Ours already lets customers drive A/B tests from Claude/ChatGPT/MCP — we speak the same protocol.*
- **De-identification pipeline** (`datavant_client.py`/`_pipeline.py`) — tokenize / de-identify (Safe Harbor / Expert Determination) / **certify** before analytics or LLM. *Matches their PHI-boundary posture exactly.*
- **Governance** — row-level security + SQL policy (`sql_security.py`), encryption at rest (`crypto_store.py`), auth/sessions (`auth.py`), SOC2/HIPAA readiness doc. *The multi-tenant + PHI-safe substrate an embed requires.*
- **Data Marketplace** — governed dataset catalog + licensing + revenue attribution.

We are ~80% of the way to being a drop-in analytics partner for a warehouse-native healthcare CDP. The missing 20% is integration plumbing + multi-tenant/SSO/embedding + compliance sign-off.

---

## 3. Integration architecture — recommended

Ours exposes **five** integration surfaces (warehouse destinations, HTTP Ingest API, webhooks/streaming, a destination framework, and MCP). We should use a **layered** approach, primary → secondary:

**PRIMARY — Warehouse-native / zero-copy (best HIPAA posture).**
Ours already lands governed, PHI-stripped events into the customer's **BigQuery / Snowflake / Databricks / S3**. We point our semantic layer at that dataset (BigQuery direct; Snowflake via secure data share / BigQuery via Analytics Hub linked dataset for zero-copy). **PHI never leaves the customer's warehouse; we hold read-only access.** This is the Snowplow/warehouse-read pattern and is the cleanest compliance story. Our `generate-model-from-table` + `/validate` auto-model their **Standard Healthcare Events** schema into governed metrics on connect.

**SECONDARY — Governed streaming destination (real-time).**
For live monitoring/alerts, register as an Ours **destination**: receive their allowlisted, de-identified event stream via **Webhook / HTTP API destination** (or PubSub/Kinesis/EventBridge) into our ingestion endpoint, mapped to the same semantic model. This is the Freshpaint/Segment "governed destination" pattern and powers Pulse alerts without waiting on warehouse batch.

**TERTIARY — MCP interop (agentic, both directions).**
Expose our **governed-metrics MCP** to their customers' AI clients so "ask your metrics / forecast / explain this drop" works from the same Claude/ChatGPT/MCP surface they already use for experiments. Reciprocally, consume **their experiment MCP + `/experiment-assignment` / `/experiment-personalization` / `getVisitorContext()`** from our Studio, so a workflow can read a variant/visitor context and branch on it.

**UI — Embedded, white-labeled analytics.**
Deliver our dashboards/Metric Lab/Ask inside their platform via **iframe + backend-signed JWT (RS256) carrying tenant + user claims**, with our **row-level security** filtering off those claims. Per-customer **custom domain** + theming so it feels native (they already ship per-customer custom domains and embedded iframe widgets, so this matches their model).

**ACTIVATION LOOP (the differentiator).**
Close the loop: we compute a high-value cohort/audience (e.g., "likely-to-convert, consented, high-LTV"), and **push it back into Ours as a source** (webhook/HTTP), where their allowlist + consent gate fan it out compliantly to Meta/Google/etc. We become the analytics that *decides*, they remain the compliant rails that *activate*.

```
Patient browser ─► Ours (capture, strip PHI, consent-gate)
                     ├─► Warehouse (BQ/Snowflake)  ─►  [US] semantic layer + Metric Lab + Ask + Pulse + experiments
                     ├─► Webhook/stream            ─►  [US] real-time monitoring/alerts
                     └─◄ Audience push-back        ◄─  [US] computed cohorts  ─► Ours activates to ad destinations
                     MCP  ◄────────────────────────►  [US] governed metrics MCP  ⇄  their experiment MCP
```

---

## 4. What to build NOW (pre-integration), prioritized

**P0 — Foundational (do first; gates everything else)**

1. **Ours source connector + schema map.** A first-class "Connect Ours Privacy" flow: authenticate to the customer's warehouse dataset (or their Ingest/OpenAPI), and **auto-model their Standard Healthcare Events + identify/track/user-profile** into our curated tables, metrics, and topics using generate-model + `/validate`. Align our identity keys to their **`ours_visitor_id` / `external_id` / merged User profile** so our metrics join to their identity graph. *This is the linchpin.*
2. **Consent-aware governance.** Treat their **consent decisions as first-class events** and add a governance rule so metrics/agents **only analyze consented data** (filter by consent state, per jurisdiction). Extends `sql_security.py`. Non-negotiable for the compliance story.
3. **Compliance parity to their bar.** Finalize **SOC 2 Type II** (observation window + CPA), be **BAA-ready** (we sign, flow down to subprocessors — Anthropic BAA on de-identified data only, Datavant, BigQuery), enforce **Safe-Harbor de-id boundary** via Datavant before any LLM call, ship the **append-only audit log** already scoped in our readiness doc, and adopt "**no AI training on customer data**" + **US-hosting** terms to match theirs.

**P1 — The seamless multi-tenant embed (what "all their existing customers" requires)**

4. **Multi-tenancy + per-tenant isolation.** Tenant model (pool with hard `tenant_id` predicate everywhere, or silo per warehouse), so one customer's data/deprovision never touches another's.
5. **SSO + provisioning.** SAML 2.0 / OIDC login, SCIM per-tenant, JIT provisioning — so their customers sign in with their existing IdP.
6. **Embedded analytics.** iframe + **RS256-signed JWT** (tenant/user claims) → our **row-level security**; white-label theming + custom domain. Reuse our existing auth + governance.

**P2 — The complementary analytics that make us worth adopting**

7. **Healthcare-marketing analytics pack.** Pre-built topics + governed metrics + dashboards on their Standard Healthcare Events: acquisition funnels, **warehouse-grade multi-touch attribution + ROAS**, CAC/LTV, cohort & retention, channel/campaign driver analysis — going *deeper* than their channel MTA (statistical attribution, incrementality via our synthetic-control/CUPED engine) without duplicating their session replay / heatmaps / CMP.
8. **Pulse for marketing.** Auto-monitoring + AI digests + threshold alerts on their conversion/spend/ROAS metrics (change/trend/anomaly/forecast/goal), delivered to Slack/email or back into their UI.
9. **Agentic analytics surface.** "Ask your metrics / explain this change / forecast next quarter" over their compliant data, via our grounded agent + MCP — the deep-analysis layer their dashboard-first product doesn't have.

**P3 — Differentiation / stickiness**

10. **Audience push-back → activation.** Computed cohorts pushed into Ours as a source for compliant activation (the loop above).
11. **Experiment analytics depth.** Consume their experiment assignments and layer our CUPED / synthetic-control / segment-scan analysis on top of their Bayesian A/B — deeper readouts, heterogeneous-effect detection, guardrail metrics.
12. **Marketplace tie-in.** Offer governed, de-identified benchmark datasets/metrics (via our Marketplace) so their customers can benchmark against peer cohorts.

---

## 5. Sequencing

- **Phase 1 (now → integration-ready):** P0 items 1–3. Deliver a working "Connect Ours → auto-model → governed metrics + Ask + Pulse" demo on a sample of their Standard Healthcare Events in BigQuery. Start SOC 2 Type II clock and BAA paperwork in parallel (long lead time).
- **Phase 2 (partner-grade):** P1 items 4–6 (multi-tenant + SSO + embed) so any of their customers can be provisioned and see our analytics inside their platform.
- **Phase 3 (value + stickiness):** P2/P3 — analytics pack, Pulse-for-marketing, agentic surface, activation loop, experiment depth.

---

## 6. Open questions to resolve with Ours Privacy (partnership)

- Is there a **formal partner / OEM program**, or is integration via account + Source token + MSA/BAA? (No public partner/OAuth program was found — likely a direct commercial conversation.)
- Warehouse-native path: will data land in the **customer's** warehouse (ideal — zero-copy read) or Ours-hosted? Confirm BigQuery Analytics Hub / Snowflake share feasibility.
- Their **Standard Healthcare Events** full schema + identity fields (so we can pre-build the analytics pack).
- Do they want us **embedded (white-label inside Ours)**, **co-sold**, or **referral**? Determines how hard we push P1.
- Experimentation is **Beta**; confirm stability before we build deep experiment-analytics on it.

---

## 7. Risks / honest caveats

- **No public partner program or API pricing/rate limits found** — integration terms are a commercial negotiation, and onboarding is gated by **MSA + BAA**. Budget legal + time.
- **EHR connectivity is partner-mediated** on their side (HL7/FHIR via a third party), not a native catalog — don't assume we can reach EHR data directly through them.
- **Compliance is the gate, not the code.** SOC 2 Type II needs a 3–12 month observation window and a CPA; BAAs and de-id boundaries must be real and evidenced before any PHI-adjacent data flows. This is the critical-path item — start it immediately.
- **Don't duplicate their surface.** Session replay, heatmaps, CMP, server-side variant assignment are theirs. We win by going *deeper* (warehouse modeling, governed metrics, forecasting, agentic analysis, automation), not by rebuilding their marketing-ops dashboards.

---

## Sources
Ours Privacy: oursprivacy.com (/product, /about, /integrations, /product/analytics, /product/consent-management-platform, /product/ab-testing-personalization, /product/signed-business-associate-agreement) · docs.oursprivacy.com (/docs/overview, /understanding-the-event-flow, /visitor-identity-and-matching, /data-mapping, /data-retention, /reference, /reference/track, /experimentation, /cookie-consent, /webhooks) · security.oursprivacy.com · status.oursprivacy.com.
Context: rockhealth.com (investment thesis) · adexchanger.com (founding story) · prweb.com (analytics suite, consent, 100+ EHR, session replay announcements) · f4.fund/startups/oursprivacy · crunchbase.com/organization/ours-privacy.
Integration patterns: hightouch.com (composable CDP, reverse-ETL, custom/embedded destinations) · rudderstack.com (composable CDP, HTTP API) · cdpinstitute.org (composable vs packaged primer) · segment.com/twilio.com (Spec, Action Destinations, webhooks) · docs.snowflake.com (Secure Data Sharing, Native Apps) · docs.cloud.google.com (BigQuery Analytics Hub) · fivetran.com (Census/Activations embedded) · documentation.freshpaint.io (HIPAA mode) · hhs.gov (HIPAA de-identification §164.514, online-tracking bulletin).
Internal: semantic_api.py · metric_lab.py · experiments_api.py · workflows_api.py · datavant_*.py · semantic_mcp.py · sql_security.py · crypto_store.py · SOC2_HIPAA_READINESS.md.
