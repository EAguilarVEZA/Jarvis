# Security, HIPAA & Privacy

Implements spec §19. Reuses MARTIN's existing controls (`auth.py`, `tenancy.py`,
`embed_api.py` RLS, `crypto_store.py`, `datavant_*`) and adds AI-specific guards.

## Data classification (every field)
`public | internal | confidential | phi | restricted`. Carried on `source_system.phi_classification`
and `SourceSchema.phi_classification`; drives redaction, retrieval allowlists, and logging.

## Core controls
- Encryption in transit (TLS 1.2+) and at rest (`crypto_store.py`).
- RBAC/ABAC + row-level security (existing governance engine, `{{user.x}}`).
- **Minimum cell sizes** enforced before exposing entity analytics (spec §28.13) — provider/
  small-cohort views suppressed below threshold.
- Audit logs + model access logs; prompt/response retention policy; secrets vault;
  non-production de-identification via `datavant_*`.
- Patient identifiers used by AI retrieval are **tokenized/pseudonymized** (`patient_token`),
  never raw PII (spec §8).

## AI-specific controls (spec §12.5, §19)
- Prompt-injection defense; retrieval **allowlists** + mandatory metadata filters before
  similarity search (spec §11.15).
- Model output **schema validation** (structured tool outputs only).
- PHI-aware logging (never log raw PHI or full prompts containing PHI).
- Evidence trace on every answer; model/version registry; approved-prompt registry.
- The LLM only reads the **de-identified** layer and computed findings — never raw PHI,
  never clinical note content by default (spec §4.2.A).

## Human-in-the-loop (required)
Urgent complaints, grievance/regulatory categorization, patient-safety escalation, legal
risk, and public-response drafting route to human review — a generative model never makes
clinical or legal escalation decisions autonomously (spec §11.5, §19, §28.9).

## Non-negotiables (spec §28)
Never let the LLM compute regulated metrics · never overwrite source with AI output · version
every inference · trace every claim to evidence · idempotent connectors · auditable entity
mapping · versioned taxonomy · least-privilege PHI · no generative clinical advice · local
model swap must not require business-logic rewrites · separate source vs. redacted text ·
preserve historical hierarchy · minimum sample policies · state uncertainty over inventing.
