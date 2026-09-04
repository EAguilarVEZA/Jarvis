"""
LLM gateway abstraction (spec §12.4). One interface, swappable adapters, so replacing the
model never touches business logic (spec §28.10). The LLM is the interaction/explanation
layer only — it narrates already-computed findings and must not invent values (spec §1, §12.1).

Adapters:
  - MockGateway (here): deterministic, template-based narration of structured payloads. Lets
    the orchestrator + API be built and tested with no model. Grounded by construction — it can
    only render values present in the payload, so it literally cannot fabricate a number.
  - LocalVLLMGateway / SystemLLMGateway (later): send the SAME system rules + payload to a
    local vLLM (OpenAI-compatible) endpoint or MARTIN's existing system_llm.py.

System prompt rules (spec §12.5) are shared across adapters via SYSTEM_RULES.
"""
from __future__ import annotations

import abc
import json

SYSTEM_RULES = (
    "You are MARTIN's experience explainer. Use ONLY the findings provided. Never invent or "
    "estimate values. If evidence is insufficient, say so plainly. Cite evidence identifiers. "
    "Separate observation from association from hypothesis. Never state causation without a "
    "stated valid design. Respect minimum cell-size policies; do not infer protected attributes."
)


class LLMGateway(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def complete(self, messages: list[dict], response_schema=None, tools=None) -> dict: ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def health(self) -> dict:
        return {"ok": True, "adapter": self.name}


class MockGateway(LLMGateway):
    """Deterministic narrator. Expects the final user message to be JSON:
    {"task": "...", "payload": {...}}. Renders a grounded narrative from the payload only."""
    name = "mock"

    def complete(self, messages: list[dict], response_schema=None, tools=None) -> dict:
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        try:
            req = json.loads(user.get("content", "{}"))
        except Exception:
            return {"text": "I could not read the request payload.", "grounded": False}
        task, p = req.get("task"), req.get("payload", {})
        fn = getattr(self, f"_narrate_{task}", None)
        if not fn:
            return {"text": "No narration available for this request.", "grounded": False}
        return {"text": fn(p), "grounded": True, "adapter": self.name}

    # ── grounded renderers (values come only from the payload) ──────────────
    def _narrate_root_cause(self, b: dict) -> str:
        e = b.get("entity", {})
        f = {x["metric_id"]: x for x in b.get("findings", [])}
        parts = [f"At {e.get('name')}, the picture over {b['period']['start']}–{b['period']['end']}:"]
        if "avg_rating" in f:
            parts.append(f"average rating is {f['avg_rating']['current']} (n={f['avg_rating']['n_current']}),")
        if "negative_topic_rate" in f and f["negative_topic_rate"]["current"] is not None:
            parts.append(f"and {round(f['negative_topic_rate']['current']*100)}% of reviews carry a negative topic.")
        drivers = b.get("candidate_drivers", [])[:3]
        if drivers:
            names = ", ".join(d["name"] for d in drivers)
            parts.append(f"The strongest negative drivers are {names}.")
        else:
            parts.append("No dominant negative driver stands out yet.")
        ev = b.get("feedback_evidence", [])[:2]
        for x in ev:
            parts.append(f'Evidence [{x["source_record_id"]}, {x["rating"]}★]: "{x["quote"]}".')
        if b.get("limitations"):
            parts.append("Caveats: " + " ".join(b["limitations"]))
        return " ".join(parts)

    def _narrate_scorecard(self, p: dict) -> str:
        rows = p.get("rows", [])
        if not rows:
            return "No experience data for this period."
        lines = [f"Scorecard for {p.get('period','the period')}:"]
        for r in rows:
            m = r["metrics"]
            lines.append(
                f"- {r['name']}: {m['review_count']['value']:.0f} reviews, "
                f"avg {m['avg_rating']['value']:.2f}, "
                f"{round(m['negative_topic_rate']['value']*100)}% negative-topic rate.")
        return "\n".join(lines)

    def _narrate_examples(self, p: dict) -> str:
        ex = p.get("examples", [])
        if not ex:
            return "No representative feedback matched that filter (insufficient evidence)."
        head = f"Representative feedback about {p.get('topic','this')}:"
        body = "\n".join(f'- [{x["source_record_id"]}, {x["rating"]}★] "{x["quote"]}"' for x in ex)
        return head + "\n" + body

    def _narrate_insufficient(self, p: dict) -> str:
        return p.get("message", "There isn't enough evidence to answer that reliably.")
