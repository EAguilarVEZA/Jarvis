"""Knowledge store ("vault") — the folders Jarvis reads from and writes to.

Defaults to <repo>/jarvis_vault, but set JARVIS_VAULT to point at a real
Obsidian vault on disk and everything (including outputs) lives there instead,
so Obsidian can sit on top of the exact same files.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
VAULT_ROOT = Path(os.getenv("JARVIS_VAULT", str(_REPO / "jarvis_vault")))

FOLDERS = {
    "inbox":     "00-INBOX",
    "knowledge": "01-KNOWLEDGE",
    "projects":  "02-PROJECTS",
    "daily":     "03-DAILY",
    "outputs":   "04-JARVIS-OUTPUTS",
    "resources": "05-RESOURCES",
    "archive":   "06-ARCHIVE",
    "system":    "07-SYSTEM",
}

# Where each skill routes its output.
OUTPUT_SUBDIRS = [
    "briefings", "captures", "connections", "syntheses",
    "beliefs", "patterns", "decisions", "reviews",
]


def folder(key: str) -> Path:
    return VAULT_ROOT / FOLDERS[key]


def ensure_structure() -> None:
    """Create the vault skeleton if it doesn't exist yet (idempotent)."""
    for name in FOLDERS.values():
        (VAULT_ROOT / name).mkdir(parents=True, exist_ok=True)
    out = folder("outputs")
    for sub in OUTPUT_SUBDIRS:
        (out / sub).mkdir(parents=True, exist_ok=True)
    (folder("system") / "memory").mkdir(parents=True, exist_ok=True)
    (folder("system") / "skills").mkdir(parents=True, exist_ok=True)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "output"


def write_output(subdir: str, title: str, body: str) -> Path:
    """Write a skill output as a timestamped Markdown file; return its path."""
    ensure_structure()
    d = folder("outputs") / subdir
    d.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    p = d / f"{ts}_{_slug(title)}.md"
    p.write_text(body, encoding="utf-8")
    return p


def list_outputs(limit: int = 25) -> list[dict]:
    ensure_structure()
    files: list[Path] = []
    for sub in OUTPUT_SUBDIRS:
        d = folder("outputs") / sub
        if d.exists():
            files.extend(d.glob("*.md"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{
        "id": str(p.relative_to(VAULT_ROOT)),
        "title": p.stem,
        "subdir": p.parent.name,
        "modified": p.stat().st_mtime,
    } for p in files[:limit]]


def read_output(rel_id: str) -> str:
    p = (VAULT_ROOT / rel_id).resolve()
    if not str(p).startswith(str(VAULT_ROOT.resolve())):
        raise ValueError("path escapes vault")
    return p.read_text(encoding="utf-8")


def latest_output(subdir: str) -> str | None:
    d = folder("outputs") / subdir
    if not d.exists():
        return None
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].read_text(encoding="utf-8") if files else None


def read_recent(key: str, limit: int = 6) -> list[tuple[str, str]]:
    """Return [(filename, text), ...] for the most recently modified notes in a folder."""
    d = folder(key)
    if not d.exists():
        return []
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def latest_daily_note() -> str | None:
    notes = read_recent("daily", limit=1)
    return notes[0][1] if notes else None


def capture(text: str, source: str = "voice") -> Path:
    """Append a raw capture to today's inbox note (the Capture processor sorts it later)."""
    ensure_structure()
    day = _dt.date.today().isoformat()
    p = folder("inbox") / f"{day}.md"
    stamp = _dt.datetime.now().strftime("%H:%M")
    line = f"- {stamp} ({source}) {text.strip()}\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return p
