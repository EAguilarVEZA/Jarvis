"""
Conversation quality evaluation suite (blueprint §18, Phase 4). Labeled batteries → metrics,
so the engine stays honest under change: interruption-classification accuracy, tool-selection
accuracy, artifact-validation handling, correction retention, and memory retrieval precision.
Deterministic — runs in CI with no model or audio.
"""
from __future__ import annotations

from .turn_manager import classify, Interruption
from .orchestrator import Router
from .memory import MemoryStore
from .artifacts import ArtifactService, ArtifactStatus

# ── labeled cases ────────────────────────────────────────────────────────────
INTERRUPTION_CASES = [
    ("stop", True, Interruption.EMERGENCY_STOP),
    ("cancel", True, Interruption.EMERGENCY_STOP),
    ("no, that's wrong", True, Interruption.CORRECTION),
    ("actually I meant Q3", True, Interruption.CORRECTION),
    ("uh-huh", True, Interruption.BACKCHANNEL),
    ("yeah", True, Interruption.BACKCHANNEL),
    ("and also make it about cardiology", True, Interruption.COLLABORATIVE),
    ("by the way add the pricing table", True, Interruption.COLLABORATIVE),
    ("what's the weather in Orlando", True, Interruption.HARD),
    ("pull up the cardiology dashboard", True, Interruption.HARD),
]

ROUTER_CASES = [
    ("write me a campaign brief", "artifact"),
    ("create a document summarizing Q3", "artifact"),
    ("draft the welcome email", "artifact"),
    ("search the latest cardiology news", "tool"),
    ("look up who the CEO is", "tool"),
    ("what's the current star rating", "tool"),
    ("explain how the funnel works", "answer"),
    ("thanks, that helps", "answer"),
]

MEMORY_CASES = [
    ("cardiology funnel", "quiz to $0 calcium screen"),
    ("brand voice", "reassuring"),
    ("unfinished email", "welcome email is unfinished"),
]


def _acc(hits, total):
    return round(hits / total, 3) if total else 1.0


def eval_interruption() -> dict:
    hits = sum(1 for t, sp, exp in INTERRUPTION_CASES if classify(t, sp) == exp)
    return {"interruption_classification_accuracy": _acc(hits, len(INTERRUPTION_CASES)),
            }


def eval_router() -> dict:
    r = Router()
    hits = sum(1 for t, exp in ROUTER_CASES if r.decide(t)["kind"] == exp)
    return {"tool_selection_accuracy": _acc(hits, len(ROUTER_CASES))}


def _mem_store() -> MemoryStore:
    m = MemoryStore()
    m.add("project", "The cardiology funnel is quiz to $0 calcium screen to consult.",
          scope="p", tags=["funnel"])
    m.add("project", "Brand voice is reassuring, expert, never fear-mongering.", scope="p", tags=["brand"])
    m.add("episodic", "Draft welcome email is unfinished; waiting on copy.", scope="p", tags=["email"])
    return m


def eval_memory_precision() -> dict:
    m = _mem_store()
    hits = 0
    for q, expect in MEMORY_CASES:
        top = m.retrieve(q, scope="p", k=1)
        if top and expect in top[0].text:
            hits += 1
    return {"memory_retrieval_precision_at_1": _acc(hits, len(MEMORY_CASES))}


def eval_correction_retention() -> dict:
    m = _mem_store()
    it = next(i for i in m.active(scope="p") if "funnel" in i.tags)
    m.correct(it.id, "The cardiology funnel now ends at a same-week consult.")
    top = m.retrieve("cardiology funnel", scope="p", k=1)
    retained = bool(top and "same-week consult" in top[0].text)
    return {"correction_retention_rate": 1.0 if retained else 0.0}


def eval_artifact_validation() -> dict:
    svc = ArtifactService()
    ok = svc.run("markdown", "ok.md", lambda c: "# Real\n\nBody.")
    bad = svc.run("markdown", "bad.md", lambda c: "")
    correct = (ok.status == ArtifactStatus.COMPLETED) and (bad.status == ArtifactStatus.FAILED)
    return {"artifact_validation_handling_rate": 1.0 if correct else 0.0}


def run_all() -> dict:
    out = {}
    for fn in (eval_interruption, eval_router, eval_memory_precision,
               eval_correction_retention, eval_artifact_validation):
        out.update(fn())
    scores = [v for k, v in out.items() if k.endswith(("accuracy", "precision_at_1", "rate"))]
    out["overall"] = round(sum(scores) / len(scores), 3) if scores else 0.0
    return out
