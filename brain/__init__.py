"""
JARVIS Brain — the active "operating loop" that turns the assistant from a
prompt-and-forget chat tool into a background intelligence layer.

Architecture (from the Obsidian "active vault" concept, instantiated in-app):

    knowledge store (vault)  →  Claude reads on a schedule  →  writes outputs
            ↑                                                        │
            └────────── compounding memory + operating doc ──────────┘

Pieces:
  - vault.py          knowledge store: folders + output read/write
  - operating_doc.py  the personal operating doc ("how I actually work")
  - context.py        the compounding context injected into the voice prompt
  - skills.py         the seven scheduled skills (read → think → write → remember)
  - runner.py         cron scheduler + run-now + per-skill state
  - api.py            /api/brain endpoints (skills, outputs, operating doc)

Nothing here imports server.py, so it's safe to import from server during wiring.
"""

from . import vault, operating_doc, context, skills, runner  # noqa: F401

__all__ = ["vault", "operating_doc", "context", "skills", "runner"]
