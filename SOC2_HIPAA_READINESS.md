# SOC 2 & HIPAA Readiness — Smart With Martin / Jarvis Platform

**Status:** Engineering readiness assessment · **Owner:** Edgar · **Data in scope:** Orlando Health de-identified & (potentially) PHI datasets, marketplace orders, credentials.

> **Disclaimer.** This is an internal engineering readiness roadmap, not legal or audit sign-off. SOC 2 certification requires an independent CPA audit; HIPAA compliance requires a formal organizational program (risk analysis, policies, workforce training, signed BAAs). Have counsel and a qualified auditor review before making any compliance claim to customers.

---

## 1. Architecture decision: de-identify first

The platform will **de-identify PHI through the Datavant pipeline before any data reaches a cloud LLM.** This is the lowest-risk posture:

- Raw PHI is tokenized + de-identified (Safe Harbor / Expert Determination) and **certified** before it is used in analytics, agents, or the marketplace.
- Only **de-identified** data is sent to Claude (Anthropic Messages API).
- For any workflow that must touch identifiable PHI, use the **local Ollama (no-egress) provider** so PHI never leaves the network.

This keeps the majority of the platform out of PHI scope entirely, dramatically shrinking the compliance surface.

---

## 2. Subprocessor / BAA register (organizational — must be completed)

Every vendor that could receive PHI needs a signed BAA or a documented "no-PHI" guarantee.

| Subprocessor | Used for | PHI exposure | Action |
|---|---|---|---|
| **Anthropic (Claude)** | Agent reasoning, transforms | Only de-identified data (per §1) | Messages API is **BAA-eligible**. Sign the BAA, enable HIPAA-ready config, use **covered models**, keep **30-day retention** (not ZDR). *(Cowork is NOT BAA-covered — never route PHI through Cowork.)* |
| **Datavant** | Tokenize / de-identify / certify | PHI (by design) | Execute BAA + data-use agreement; confirm certified environment. |
| **Airbyte** | Data ingestion → warehouse | Potentially PHI | BAA/DPA; run self-hosted/air-gapped for PHI feeds. |
| **Fish Audio (TTS)** | Voice responses | Could transmit PHI if spoken | BAA or restrict to non-PHI text only. |
| **Hosting / DB (BigQuery, Postgres, object storage)** | Data at rest | PHI | BAA with cloud provider; enable encryption + access logging. |
| **Email/SMTP provider** | Report/alert delivery | Possible PHI in bodies | BAA or send links only, never PHI in email. |

---

## 3. HIPAA Technical Safeguards (§164.312) — status & remediation

| Control | Requirement | Current status | Remediation |
|---|---|---|---|
| Access control | Unique IDs, emergency access, auto-logoff, encryption | ◐ Login + signed sessions, roles/projects, row-security | Add MFA, idle auto-logoff, break-glass account |
| Audit controls (b) | Record + examine PHI access | ◐ Marketplace/license/review audit trails; server logs | Build centralized **append-only access log** (who/what/when/IP) + retention |
| Integrity | Prevent improper alteration | ◐ Versioning, snapshots | Add checksums/immutability on stored datasets & logs |
| Person/entity auth | Verify identity | ◐ Password login | Add **MFA (TOTP)** |
| Transmission security | Encrypt in transit | ◐ HTTPS (self-signed in dev) | CA-signed TLS in prod; enforce TLS 1.2+ |
| **Encryption at rest** | Encrypt ePHI at rest | ✅ **Done for credentials + secrets (`crypto_store`)** | Extend to memory DB, run/data stores; encrypt disks/volumes |

## HIPAA Administrative & Physical (§164.308 / §164.310) — organizational

Risk analysis & risk management · sanction policy · workforce security & training · access authorization/termination · **BAAs (§2)** · contingency plan (backup, DR, emergency mode) · incident response & **breach notification** · facility access controls · workstation & device/media controls (secure disposal). *None of these are code — they are policies + processes your org must adopt and evidence.*

---

## 4. SOC 2 Trust Services Criteria — mapping

- **Security (required):** access control, MFA, encryption, change management, vulnerability management, monitoring/logging, incident response.
- **Availability:** SLAs, backups, DR, uptime monitoring.
- **Processing Integrity:** input validation, run auditing, error handling (partly covered — retries, error branches, run history).
- **Confidentiality:** encryption at rest/in transit ✅ started, least-privilege, data classification.
- **Privacy:** notice, consent, de-identification ✅ (Datavant), data-subject handling.

**Path to Type II:** adopt a compliance platform (Vanta / Drata / Secureframe) to codify policies + collect evidence, remediate the gaps below, run an independent pen test, then a **3–12 month observation window** audited by a CPA firm.

---

## 5. Prioritized remediation roadmap

**Phase A — technical controls (in the app, engineering):**
1. ✅ **Encryption at rest** for credentials & secrets (`crypto_store`, Fernet/AES). *Done.*
2. Centralized **append-only audit log** of authentication + PHI-resource access, surfaced in Admin.
3. **MFA (TOTP)** + **idle auto-logoff** + secure session cookies/revocation.
4. **Compliance mode**: force PHI workflows to local (no-egress) models; block external connectors; redact PHI before any cloud LLM call.
5. **Data retention & secure disposal** jobs (runs, memory, logs) with documented periods.
6. Extend encryption to the memory DB and data stores; enforce TLS 1.2+ with a valid cert.
7. ✅ **Compliance self-check** endpoint (`/api/workflows/compliance/status`). *Done.*

**Phase B — organizational (you + counsel + auditor):**
1. Execute **BAAs** with all subprocessors (§2).
2. Complete HIPAA **risk analysis** and write policies/procedures.
3. Designate **Privacy Officer** and **Security Officer**.
4. Workforce **training** + sanction policy.
5. **Incident response** + **breach notification** + **contingency (BC/DR)** plans.
6. Engage a **compliance platform** + **CPA auditor**; schedule pen test + Type II window.

---

## 6. Already in place (credit where due)

Login + signed sessions · Admin security governance with row-level (`{{user.x}}`) controls · projects + roles · **Datavant de-identification & certification pipeline** · external-secrets abstraction (Vault/env/value) with **write-only** secret values · **local no-egress LLM** option · workflow versioning/snapshots · marketplace/license/review **audit trails** · per-node error handling + run history · **encryption at rest for credentials & secrets** · **live compliance self-check**.

---

*Generated as an engineering readiness aid. Validate with qualified HIPAA counsel and a SOC 2 auditor before representing compliance to any customer or partner.*
