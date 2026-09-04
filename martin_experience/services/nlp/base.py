"""
NLP service interfaces (spec §11.1-11.3, §12.1). Governed, versioned contracts so the
production classifier (a trained compact model — spec §11.2 "should not remain the only
production method" for the LLM) can slot in behind the same interface as the deterministic
baseline. The LLM may bootstrap labels but is never the sole production classifier.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class Clause:
    text: str
    start: int
    end: int


@dataclass
class TopicHit:
    topic_id: str
    confidence: float
    span_start: int
    span_end: int


@dataclass
class AspectSentiment:
    topic_id: str | None       # None = document-level
    label: str                 # positive|neutral|negative|mixed
    score: float               # -1..1


@dataclass
class NlpResult:
    topics: list[TopicHit] = field(default_factory=list)
    aspects: list[AspectSentiment] = field(default_factory=list)
    document_sentiment: AspectSentiment | None = None
    model_version: str = ""


class TopicClassifier(abc.ABC):
    version: str = "topic-baseline-1.0"

    @abc.abstractmethod
    def classify(self, text: str) -> list[TopicHit]: ...


class SentimentClassifier(abc.ABC):
    version: str = "sentiment-baseline-1.0"

    @abc.abstractmethod
    def classify(self, text: str, topics: list[TopicHit]) -> tuple[list[AspectSentiment], AspectSentiment]:
        """Return (aspect_sentiments, document_sentiment)."""
        ...
