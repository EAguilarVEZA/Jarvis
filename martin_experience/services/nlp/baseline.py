"""
Deterministic baseline NLP (spec §11.2-11.3). Clause-level topic matching against the
taxonomy lexicon and aspect sentiment via a polarity lexicon with negation handling. Runs
with zero model dependency so the pipeline is testable now; a trained compact classifier or
an LLM adapter can replace either engine behind the same interface (base.py).
"""
from __future__ import annotations

import re

from .base import AspectSentiment, SentimentClassifier, TopicClassifier, TopicHit
from .lexicons import (NEG_PHRASES, NEGATIVE, NEGATORS, POSITIVE, TOPIC_LEXICON)

# split on contrast words + sentence punctuation → clauses carry one polarity (spec §11.3)
_SPLIT = re.compile(r"\b(?:but|however|although|though)\b|[.;!?]", re.IGNORECASE)


def clauses(text: str):
    pos = 0
    for m in _SPLIT.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            yield seg, pos
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        yield tail, pos


class BaselineTopicClassifier(TopicClassifier):
    version = "topic-baseline-1.0"

    def classify(self, text: str) -> list[TopicHit]:
        t = (text or "").lower()
        hits: dict[str, TopicHit] = {}
        for topic_id, phrases in TOPIC_LEXICON.items():
            n = 0
            first = -1
            for ph in phrases:
                idx = t.find(ph)
                if idx >= 0:
                    n += 1
                    first = idx if first < 0 else min(first, idx)
            if n:
                conf = min(0.95, 0.65 + 0.1 * (n - 1))
                hits[topic_id] = TopicHit(topic_id, round(conf, 3), first, first + 1)
        return sorted(hits.values(), key=lambda h: h.confidence, reverse=True)


class BaselineAspectSentiment(SentimentClassifier):
    version = "sentiment-baseline-1.0"

    def _clause_polarity(self, clause: str) -> float:
        c = clause.lower()
        pos = sum(1 for w in re.findall(r"[a-z']+", c) if w in POSITIVE)
        neg = sum(1 for w in re.findall(r"[a-z']+", c) if w in NEGATIVE)
        neg += sum(1 for ph in NEG_PHRASES if ph in c)
        has_negator = any(w in NEGATORS for w in re.findall(r"[a-z']+", c))
        score = pos - neg
        # a negator with net-positive wording flips it ("nobody told me" style handled by NEG words too)
        if has_negator and score > 0:
            score = -abs(score) - 1
        if score > 0:
            return min(1.0, 0.4 + 0.3 * score)
        if score < 0:
            return max(-1.0, -(0.4 + 0.3 * abs(score)))
        return 0.0

    @staticmethod
    def _label(score: float) -> str:
        return "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"

    def classify(self, text: str, topics: list[TopicHit]):
        topic_ids = {h.topic_id for h in topics}
        aspects: dict[str, AspectSentiment] = {}
        scores: list[float] = []
        topic_clf = BaselineTopicClassifier()
        for clause, _ in clauses(text):
            s = self._clause_polarity(clause)
            scores.append(s)
            for h in topic_clf.classify(clause):        # topics local to THIS clause
                if h.topic_id in topic_ids:
                    prev = aspects.get(h.topic_id)
                    # keep the strongest-magnitude signal per topic
                    if prev is None or abs(s) > abs(prev.score):
                        aspects[h.topic_id] = AspectSentiment(h.topic_id, self._label(s), round(s, 3))
        # document-level: mean of clause polarities; "mixed" if it spans both signs
        doc_score = round(sum(scores) / len(scores), 3) if scores else 0.0
        if any(s > 0.15 for s in scores) and any(s < -0.15 for s in scores):
            doc_label = "mixed"
        else:
            doc_label = self._label(doc_score)
        return list(aspects.values()), AspectSentiment(None, doc_label, doc_score)
