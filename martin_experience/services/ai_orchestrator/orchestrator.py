"""
AI Orchestrator (spec §12.2). Turns a natural-language question into a grounded answer:
intent route → entity resolve → tool selection → deterministic tool execution → narration
by the LLM gateway (facts only) → citations → audit. The orchestrator, not the LLM, decides
which governed tool runs; the LLM only narrates the structured result (spec §1, §12.1).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..llm_gateway.gateway import MockGateway, SYSTEM_RULES
from ..nlp.baseline import BaselineTopicClassifier
from . import tools


class Orchestrator:
    def __init__(self, store, gateway=None):
        self.store = store
        self.gateway = gateway or MockGateway()
        self.audit: list[dict] = []

    # ── entity resolution (spec §12.2) ───────────────────────────────────────
    def _resolve_entity(self, question: str):
        q = question.lower()
        for r in self.store.query(
            "SELECT entity_type, entity_id, display_name FROM entity_dimension WHERE is_current=1"):
            name = (r["display_name"] or "").lower()
            tokens = [t for t in re.split(r"\W+", name) if len(t) > 3 and t not in ("hospital", "health")]
            if any(t in q for t in tokens):
                return r["entity_type"], r["entity_id"]
        return None, None

    def _resolve_topic(self, question: str):
        hits = BaselineTopicClassifier().classify(question)
        return hits[0].topic_id if hits else None

    # ── intent routing ──────────────────────────────────────────────────────
    @staticmethod
    def _route(q: str) -> str:
        q = q.lower()
        if re.search(r"\bwhy\b|driv|decline|declin|low|root cause|worst", q):
            return "investigate_root_cause"
        if re.search(r"anomal|emerging|early warning|flag|which issues|biggest issues", q):
            return "find_anomalies"
        if re.search(r"example|reviews? about|feedback about|show me|complaints? about", q):
            return "retrieve_feedback_examples"
        if re.search(r"topic|themes?|talking about", q):
            return "get_topic_distribution"
        return "scorecard"

    # ── main entry ───────────────────────────────────────────────────────────
    def ask(self, question: str, start: str, end: str,
            prior_start: str | None = None, prior_end: str | None = None) -> dict:
        intent = self._route(question)
        etype, eid = self._resolve_entity(question)
        used: list[str] = []

        # root cause needs an entity — fall back to the top current issue's entity
        if intent == "investigate_root_cause" and not eid:
            iss = tools.find_anomalies(self.store, "location", start, end)["issues"]
            used.append("find_anomalies")
            if iss:
                etype, eid = iss[0]["entity_type"], iss[0]["entity_id"]

        task, payload = self._run(intent, etype, eid, question, start, end, prior_start, prior_end, used)
        result = self._narrate(task, payload)

        record = {"question": question, "intent": intent, "task": task,
                  "entity": {"type": etype, "id": eid}, "tools_used": used,
                  "citations": self._citations(payload),
                  "ts": datetime.now(timezone.utc).isoformat()}
        self.audit.append(record)
        return {**result, **record}

    def _run(self, intent, etype, eid, question, start, end, ps, pe, used):
        if intent == "investigate_root_cause" and eid:
            used.append("investigate_root_cause")
            b = tools.investigate(self.store, etype, eid, start, end, ps, pe, question)
            if not b["candidate_drivers"] and (b["findings"][0]["n_current"] or 0) == 0:
                return "insufficient", {"message": f"No experience data for {eid} in this window."}
            return "root_cause", b
        if intent == "find_anomalies":
            used.append("find_anomalies")
            iss = tools.find_anomalies(self.store, "location", start, end, ps, pe)["issues"]
            if not iss:
                return "insufficient", {"message": "No issues cross the governed thresholds this period."}
            return "scorecard", tools.scorecard(self.store, "location", start, end)  # summarize entities
        if intent == "retrieve_feedback_examples":
            used.append("retrieve_feedback_examples")
            topic = self._resolve_topic(question)
            ex = tools.retrieve_feedback_examples(self.store, start, end, topic_id=topic,
                                                  entity_type=etype, entity_id=eid,
                                                  sentiment="negative" if topic else None)
            name = self.store.scalar("SELECT name FROM topic WHERE topic_id=? LIMIT 1", [topic]) if topic else None
            return "examples", {"topic": name or topic or "recent feedback", **ex}
        if intent == "get_topic_distribution":
            used.append("get_topic_distribution")
            return "topics", tools.get_topic_distribution(self.store, start, end, etype, eid, negative_only=True)
        used.append("scorecard")
        return "scorecard", tools.scorecard(self.store, "location", start, end)

    def _narrate(self, task, payload):
        messages = [{"role": "system", "content": SYSTEM_RULES},
                    {"role": "user", "content": json.dumps({"task": task, "payload": payload})}]
        r = self.gateway.complete(messages)
        return {"answer": r.get("text", ""), "grounded": r.get("grounded", False),
                "data": payload}

    @staticmethod
    def _citations(payload) -> list[str]:
        refs = []
        for x in (payload or {}).get("feedback_evidence", []) or (payload or {}).get("examples", []):
            if x.get("evidence_ref"):
                refs.append(x["evidence_ref"])
        for fnd in (payload or {}).get("findings", []):
            refs += fnd.get("evidence_refs", [])
        return refs
