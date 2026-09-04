# Model Strategy

Implements spec §12–§13. Principle: **the LLM is the interaction/explanation layer, not the
statistical source of truth** (spec §1). All numbers come from deterministic engines.

## Phase A — no custom fine-tuning (start here)
- Local/open-weight instruction model behind an OpenAI-compatible endpoint (vLLM), reached
  through the `LLMGateway` abstraction (spec §12.4), alongside the existing `system_llm.py`.
- Local embeddings + a vector index with **mandatory metadata filtering** (retrieval service).
- RAG + constrained structured output + tool calling. Deterministic stats via reused
  `experiments_api` (anomaly, driver, DiD, forecasting) and the metric engine.
- Rule engine for severity/actionability (spec §11.5) — never generative for escalation.

## Phase B — gold dataset
Sample 5,000–15,000 representative feedback items; human-label topic(s), aspect sentiment,
journey stage, severity, actionability, praise/complaint. Documented labeling rules;
adjudicated disagreements (spec §13.B).

## Phase C — narrow models (the moat)
Train topic classifier, aspect-sentiment classifier, severity classifier, journey-stage
classifier. Evaluate **per facility / service line / source** to catch domain drift. These
compact models — not the LLM — do high-volume production inference (spec §11.2).

## Phase D — LLM fine-tune only if evals justify it
LoRA/QLoRA for MARTIN terminology, structured response + tool-use behavior, experience
reasoning format, and interviewing style. **Never a substitute for retrieval** (spec §13.D).
Bootstrap the tuning set from verified {question → governed tool call → grounded answer}
traces; grade with the existing Evaluations harness. Fine-tune on Databricks Model Serving
or Modal.

## LLM gateway (spec §12.4)
```
class LLMGateway:
    def complete(self, messages, response_schema=None, tools=None): ...
    def embed(self, texts): ...
    def health(self): ...
```
Adapters: local vLLM (default, PHI-safe) · optional enterprise endpoint · optional external
endpoint for approved non-PHI experiments only. Swapping the model must not touch business
logic (spec §28.10).

## Evaluation (spec §13)
Frozen test sets: straightforward, ambiguous, multilingual, short text, long complaints,
praise+complaint, sarcasm, clinical terms, adversarial prompt-injection, PHI-containing.
Metrics: macro/micro F1, per-topic precision/recall, calibration, hallucination rate,
tool-selection accuracy, grounded-answer rate, human usefulness.
