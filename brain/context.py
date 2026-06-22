"""Context assembly — the compounding intelligence the rest of the app draws on.

Two consumers:
  - skills (gather_inputs): the full read context a skill thinks over.
  - the live voice assistant (build_brain_context): a tight summary injected into
    every reply so Jarvis answers grounded in the operating doc, the latest brief
    and open loops — i.e. it gets smarter as the loop runs.
"""
from __future__ import annotations

import logging

from . import operating_doc, vault

log = logging.getLogger("jarvis.brain.context")


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # never let context-gathering break a reply
        log.debug("context source failed: %s", e)
        return default


def gather_inputs() -> dict:
    """Everything a skill might read. Each source degrades gracefully to empty."""
    import memory  # local import to avoid hard coupling at module load
    return {
        "operating_doc":      _safe(operating_doc.load, ""),
        "recent_memories":    _safe(lambda: memory.get_recent_memories(limit=10), []),
        "important_memories": _safe(lambda: memory.get_important_memories(limit=8), []),
        "open_tasks":         _safe(lambda: memory.get_open_tasks(), []),
        "daily_note":         _safe(vault.latest_daily_note, "") or "",
        "inbox":              _safe(lambda: vault.read_recent("inbox", limit=2), []),
        "recent_daily":       _safe(lambda: vault.read_recent("daily", limit=3), []),
        "projects":           _safe(lambda: vault.read_recent("projects", limit=6), []),
        "knowledge":          _safe(lambda: vault.read_recent("knowledge", limit=6), []),
        "last_connection":    _safe(lambda: vault.latest_output("connections"), "") or "",
        "last_brief":         _safe(lambda: vault.latest_output("briefings"), "") or "",
        "last_synthesis":     _safe(lambda: vault.latest_output("syntheses"), "") or "",
    }


def _fmt_memories(mems: list[dict], cap: int = 8) -> str:
    lines = []
    for m in mems[:cap]:
        t = m.get("type") or m.get("mem_type") or "note"
        lines.append(f"  - [{t}] {m.get('content','')}")
    return "\n".join(lines)


def _fmt_notes(notes: list[tuple[str, str]], cap_chars: int = 600) -> str:
    chunks = []
    for name, text in notes:
        chunks.append(f"### {name}\n{text.strip()[:cap_chars]}")
    return "\n\n".join(chunks)


def build_brain_context(max_chars: int = 1700) -> str:
    """The compounding context injected into the voice assistant's system prompt.

    Deliberately compact — operating doc essence + latest brief headline + open
    loops — so the assistant is personal and current without blowing the budget.
    """
    import memory
    parts: list[str] = []

    od = _safe(lambda: operating_doc.summary(900), "")
    if od.strip():
        parts.append("OPERATING DOC (who I am & how I work):\n" + od)

    brief = _safe(lambda: vault.latest_output("briefings"), "") or ""
    if brief.strip():
        parts.append("TODAY'S MORNING BRIEF (most recent):\n" + brief.strip()[:650])

    tasks = _safe(lambda: memory.get_open_tasks(), [])
    if tasks:
        lines = [f"  - {t.get('title','')}" + (f" (due {t['due_date']})" if t.get("due_date") else "")
                 for t in tasks[:6]]
        parts.append("OPEN LOOPS:\n" + "\n".join(lines))

    syn = _safe(lambda: vault.latest_output("syntheses"), "") or ""
    if syn.strip():
        parts.append("LAST WEEKLY SYNTHESIS (headline):\n" + syn.strip()[:350])

    text = "\n\n".join(parts).strip()
    return text[:max_chars]
