# Smart with Martin × Ours Privacy — Partnership Summary

**The idea in one line:** Ours Privacy is the compliant data pipe and activation layer; Smart with Martin is the analytics brain and automation layer on top of it. You capture and govern the data; we model, explain, forecast, and act — inside your platform, for your existing customers.

---

## Why it fits

Ours Privacy already lands PHI-stripped, consent-gated healthcare-marketing events in the customer's warehouse (BigQuery/Snowflake) and forwards to 50+ destinations. Your analytics suite is intentionally marketing-ops-grade (GA-replacement dashboards, channel attribution, session replay, funnels, Bayesian A/B). Deep, warehouse-native modeling — governed metrics, statistical attribution, forecasting, cohort/retention, agentic analytics, and automation — is a natural complement, not an overlap. We run **natively on BigQuery, exactly where your data already lands.**

## What we've already built to be integration-ready

We built the integration in four tiers and all four are working today (behind the compliance gate below):

- **Connector + auto-model (P0).** A "Connect Ours Privacy" flow that reads your Standard Healthcare Events and auto-builds a governed model — curated tables, an identity-resolved users table, a fan-out-safe relationship, and a starter set of governed metrics. Includes a consent-gated ingestion path (webhook/streaming) and a PHI guard at the boundary.
- **Multi-tenant embedding (P1).** Per-tenant isolation via row-level security, signed embed tokens, JIT/SCIM provisioning scoped per tenant, per-tenant SSO config, and a white-label iframe embed — so any of your customers can be provisioned and see our analytics inside your product.
- **Complementary analytics depth (P2).** A healthcare-marketing analytics pack: acquisition funnel counts, **conversion-rate and revenue-per-visitor ratio metrics**, revenue, and ROAS/CAC (once an ad-spend source is connected). All consent-filtered. Pulse-style monitoring (change/trend/anomaly/forecast/goal + AI digest) and natural-language "Ask your metrics" work on every one automatically.
- **Activation loop + experiment depth + benchmarks (P3).** We compute a high-value cohort and push it **back into Ours** for compliant activation (consent-gated, k-anonymity floor, opt-in). Deeper experiment readouts (uplift, significance, CI, CUPED/synthetic-control) layer on your Bayesian A/B. De-identified peer benchmarks via our governed Marketplace.

## Recommended integration model

Layered, warehouse-native first:

1. **Primary — zero-copy warehouse read.** We point our semantic layer at the customer's Ours-landed dataset. PHI never leaves their warehouse; we hold read-only access.
2. **Secondary — governed streaming destination** for real-time monitoring/alerts.
3. **MCP interop both ways** — your customers already drive experiments from Claude/ChatGPT/MCP; we expose governed metrics on the same protocol, and can read your experiment assignments.
4. **Embedded, white-labeled UI** so it feels native inside Ours.

## Compliance posture (the shared bar)

De-identify-first (Datavant tokenize/de-id/certify before any LLM), encryption at rest, row-level security, audit logging, US-hosted, and no model training on customer data. We are prepared to **sign a BAA and flow it down to subprocessors**, and are on the path to **SOC 2 Type II**.

## What we'd need from Ours to go live

- Confirmation of the partner/OEM model (destination + warehouse share vs. embedded app).
- The full **Standard Healthcare Events** schema + identity fields, to finalize the analytics pack.
- Warehouse path confirmation (customer's warehouse, zero-copy) and a sandbox dataset for a live demo.
- Alignment on **BAA + SOC 2** timing — this is the critical path, not the engineering.

## The honest critical path

The build is largely done; the gating items are **not code**: SOC 2 Type II (observation window + CPA), the BAA, and a security review of the embed layer (asymmetric token signing, read-only enforcement, live SSO handshake). Everything we've built is structured to slot in behind those.

---
*Prepared for the Ours Privacy partnership conversation. Full technical detail in OURS_PRIVACY_INTEGRATION_PLAN.md.*
