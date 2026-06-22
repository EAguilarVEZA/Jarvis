"""The operating doc — Jarvis's personal context ("how I actually work").

Every skill reads this first, and a short summary is injected into the live
voice assistant's prompt so it stops sounding generic and starts sounding like
it knows you. Lives at <vault>/07-SYSTEM/OPERATING.md so it's editable in
Obsidian or via the /api/brain/operating-doc endpoint.
"""
from __future__ import annotations

from pathlib import Path

from . import vault

FILENAME = "OPERATING.md"

TEMPLATE = """# JARVIS Operating Doc

> This is the single most important file in the system. Every skill reads it
> first. Write how you *actually* work — not the idealized version. Keep it
> current; the system improves as this sharpens.

## 1. Identity
- Name / role:
- Timezone:
- Who Jarvis is to me (assistant, chief of staff, analyst, ...):

## 2. How I actually work
- Best working hours:
- What derails me:
- What energizes me:
- Rhythms / rituals:

## 3. Focus areas (right now)
- Top priorities:
- Core goals this quarter:
- Boundaries (what I'm NOT doing):

## 4. Active projects
- Project — status — next action — priority:

## 5. Current beliefs
- Principles that guide my decisions:
- How I decide:

## 6. Active questions
- Open questions in motion:
- Decisions I need to make:

## 7. Output standards
- Tone & voice:
- Format / length I prefer:
- Quality bar:

## 8. Permissions
- Jarvis CAN: read the vault, write to 04-JARVIS-OUTPUTS, search, update memory.
- REQUIRES my approval: writing outside outputs, contacting people, financial or
  strategic recommendations beyond analysis.

## 9. Memory rules
- What to always remember:
- What to forget / ignore:

## 10. Update protocol
- Review this doc weekly (after the Sunday synthesis).
- When a belief or priority changes, update it here first.
"""


def path() -> Path:
    return vault.folder("system") / FILENAME


def ensure() -> Path:
    vault.ensure_structure()
    p = path()
    if not p.exists():
        p.write_text(TEMPLATE, encoding="utf-8")
    return p


def load() -> str:
    return ensure().read_text(encoding="utf-8")


def save(text: str) -> Path:
    vault.ensure_structure()
    p = path()
    p.write_text(text, encoding="utf-8")
    return p


def summary(max_chars: int = 1600) -> str:
    """A trimmed version for prompt injection (keeps the token budget sane)."""
    text = load().strip()
    return text if len(text) <= max_chars else text[:max_chars] + "\n…"
