"""
Memory acceptance tests (blueprint §11, §4.3, §17 #9).
Run:  python3 -m jarvis_convo.demo_memory
"""
from __future__ import annotations

from .memory import ContextBuilder, MemoryStore
from .state import WorkingMemory

PROJ = "cardiology-launch"


def build_store() -> MemoryStore:
    m = MemoryStore()
    m.add("project", "The cardiology funnel is quiz to $0 calcium screen to consult.",
          scope=PROJ, tags=["funnel", "cardiology"], provenance="turn-3")
    m.add("project", "Brand voice for cardiology is reassuring, expert, never fear-mongering.",
          scope=PROJ, tags=["brand", "voice"], provenance="turn-5")
    m.add("episodic", "Draft welcome email is unfinished; waiting on the risk-score copy.",
          scope=PROJ, tags=["email", "unfinished"], provenance="turn-9")
    m.add("preference", "Keep spoken answers concise; put depth in artifacts.",
          scope="global", tags=["style"], provenance="turn-1", confidence=0.95)
    m.add("project", "Colette pastry AOV target is 18 dollars.", scope="colette",
          tags=["aov"], provenance="other-project")   # different scope — must stay isolated
    return m


def test_resume_coherence():
    m = build_store()
    hits = m.retrieve("what did we decide about the cardiology funnel", scope=PROJ)
    texts = " ".join(h.text for h in hits)
    assert "quiz to $0 calcium screen" in texts, texts
    assert "Colette" not in texts, "other project's memory leaked across scope"
    return "resume: funnel decision recalled, other project isolated"


def test_relevance_only():
    m = build_store()
    hits = m.retrieve("where is the email we were drafting", scope=PROJ)
    top = hits[0].text
    assert "welcome email is unfinished" in top, top
    return "retrieval returns the relevant item (unfinished email), not the funnel"


def test_preferences_always_apply():
    m = build_store()
    ctx = ContextBuilder(m).build(WorkingMemory(), "draft the cardiology email", scope=PROJ)
    assert any("concise" in p for p in ctx["preferences"]), ctx["preferences"]
    return "preferences surface in context even without keyword overlap"


def test_delete_control():
    m = build_store()
    email = next(i for i in m.active(scope=PROJ) if "email" in i.text)
    assert m.forget(email.id)
    hits = m.retrieve("email draft", scope=PROJ)
    assert all("welcome email is unfinished" not in h.text for h in hits), "deleted item still retrieved"
    return "user deletion removes an item from retrieval"


def test_correction_supersedes():
    m = build_store()
    funnel = next(i for i in m.active(scope=PROJ) if "funnel" in i.tags)
    new = m.correct(funnel.id, "The cardiology funnel now ends at a same-week consult, not a screen.")
    assert new.version == 2 and funnel.deleted and funnel.superseded_by == new.id
    hits = m.retrieve("cardiology funnel", scope=PROJ)
    assert any("same-week consult" in h.text for h in hits)
    assert all("calcium screen to consult" not in h.text for h in hits), "old version still surfaced"
    return "correction supersedes the old memory"


def test_provenance_and_governed_write():
    m = MemoryStore()
    cand = m.propose("project", "Tentative: sunset the old landing page.", scope=PROJ)
    # not committed yet → not retrievable
    assert m.retrieve("landing page", scope=PROJ) == []
    m.commit(cand)
    hits = m.retrieve("landing page", scope=PROJ)
    assert hits and hits[0].provenance and hits[0].ts, "every item carries provenance + timestamp"
    return "governed write (propose→commit) + provenance/timestamp present"


def main():
    for fn in (test_resume_coherence, test_relevance_only, test_preferences_always_apply,
               test_delete_control, test_correction_supersedes, test_provenance_and_governed_write):
        print(f"  PASS  {fn()}")
    print("\nOK — episodic/preference/project memory with scoped retrieval, deletion, "
          "correction, governed writes, and provenance all pass.")


if __name__ == "__main__":
    main()
