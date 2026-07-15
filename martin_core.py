"""Martin core — the identity + memory layer that makes agents trustworthy and
portable (the "core files" the agent crash-course says everyone skips).

Two ideas:
  1. **Core files** (plain markdown, model-agnostic): who Martin is (identity),
     how he thinks (soul), and what he may do (boundaries: draft/recommend/act).
     Plus a per-specialist identity file. These travel across models/machines —
     swap the engine underneath and Martin is still Martin.
  2. **Second brain**: Martin reads persistent memory from the Obsidian vault
     (About Me, Decisions Log, recent daily notes) so agents stop starting from
     scratch, and writes learnings back to it.

`assemble_system_prompt(agent_id, task)` composes all of this into the grounding
that any LLM call (planner, specialist) runs on. No hand-wiring per capability —
context comes from files + the vault.
"""
from __future__ import annotations

import os
import json
import time
import glob
import hashlib
from pathlib import Path

_DIR = Path(__file__).resolve().parent
CORE_DIR = _DIR / "martin" / "core"
AGENTS_DIR = _DIR / "martin" / "agents"


def _vault() -> Path:
    env = os.getenv("MARTIN_VAULT")
    if env:
        return Path(env)
    home = Path.home()
    for cand in (home / "Documents" / "MyVault", home / "MyVault", _DIR / "jarvis_vault"):
        if cand.exists():
            return cand
    return home / "Documents" / "MyVault"


def _read(p: Path, cap: int = 8000) -> str:
    try:
        return p.read_text(encoding="utf-8")[:cap]
    except Exception:
        return ""


def read_core() -> dict:
    return {"identity": _read(CORE_DIR / "identity.md"),
            "soul": _read(CORE_DIR / "soul.md"),
            "boundaries": _read(CORE_DIR / "boundaries.md"),
            "rules": _read(CORE_DIR / "rules.md")}


def agent_identity(agent_id: str) -> str:
    if not agent_id:
        return ""
    return _read(AGENTS_DIR / f"{agent_id}.md", cap=4000)


def read_memory(daily_notes: int = 3) -> str:
    """Persistent context from the Obsidian second brain: About Me + Decisions Log
    + the most recent daily notes (capped)."""
    v = _vault()
    parts = []
    mem_dir = v / "04-Claude-Memory"
    for name in ("About Me.md", "Decisions Log.md"):
        t = _read(mem_dir / name, cap=4000)
        if t:
            parts.append(f"### {name}\n{t}")
    # newest daily notes
    try:
        notes = sorted(glob.glob(str(v / "01-Daily" / "*.md")), reverse=True)
        for n in notes[:daily_notes]:
            t = _read(Path(n), cap=2500)
            if t:
                parts.append(f"### Daily {Path(n).stem}\n{t}")
    except Exception:
        pass
    return "\n\n".join(parts)


def assemble_system_prompt(agent_id: str = None, task: str = "") -> str:
    """Compose the full grounding for a Martin agent: core files + specialist
    identity + second-brain memory + the task."""
    c = read_core()
    blocks = []
    if c["identity"]:
        blocks.append(c["identity"])
    if c["soul"]:
        blocks.append(c["soul"])
    if c["boundaries"]:
        blocks.append(c["boundaries"])
    if c.get("rules"):
        blocks.append(c["rules"])
    ai = agent_identity(agent_id)
    if ai:
        blocks.append("## Your role right now\n" + ai)
    mem = read_memory()
    if mem:
        blocks.append("## Business context & memory (from the second brain)\n" + mem)
    if task:
        blocks.append("## Current task\n" + task)
    return "\n\n---\n\n".join(blocks)


def append_memory(title: str, text: str) -> bool:
    """Write a learning back to the second brain (append to Agent Memory.md)."""
    try:
        mem_dir = _vault() / "04-Claude-Memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        f = mem_dir / "Agent Memory.md"
        stamp = time.strftime("%Y-%m-%d %H:%M")
        header = "" if f.exists() else "---\ntitle: Agent Memory\ntype: memory\n---\n\n# Agent Memory\n\nLearnings Martin's agents captured while working.\n"
        with open(f, "a", encoding="utf-8") as fh:
            if header:
                fh.write(header)
            fh.write(f"\n- **{stamp} · {title}** — {text}")
        return True
    except Exception:
        return False


def _append_file(path: Path, text: str, header_if_new: str = "") -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if new and header_if_new:
                f.write(header_if_new)
            f.write(text)
        return True
    except Exception:
        return False


def add_priority(text: str) -> bool:
    """Append an open task to Active Priorities.md (the vault's queue of open work)."""
    f = _vault() / "Active Priorities.md"
    stamp = time.strftime("%Y-%m-%d")
    header = "---\nstatus: active\nproject: meta\ntype: plan\n---\n# Active Priorities\n\nThe single queue of open work across everything.\n\n### Open Tasks\n"
    return _append_file(f, f"\n- [ ] {text.strip()}  _(added {stamp} via Martin)_", header)


def add_rule(text: str) -> bool:
    """Append a house rule to the vault (Martin follows these alongside the 11 rules)."""
    f = _vault() / "02-Knowledge" / "House Rules (captured).md"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    header = "---\nstatus: active\nproject: meta\ntype: reference\n---\n# House Rules (captured)\n\nStanding rules added by voice. Martin follows these alongside [[AI Agent Rules — The 11 Rules]].\n"
    return _append_file(f, f"\n- {text.strip()}  _(added {stamp})_", header)


def add_knowledge(title: str, text: str) -> bool:
    """Save a piece of knowledge/context to the second brain."""
    return append_memory(title or "Note", text)


def _people_dir() -> Path:
    return _vault() / "04-Claude-Memory" / "People"


def _person_file(name: str) -> Path:
    safe = "".join(c for c in (name or "").strip() if c.isalnum() or c in " -_'").strip() or "Unknown"
    return _people_dir() / (safe + ".md")


def get_person(name: str) -> dict:
    f = _person_file(name)
    if f.exists():
        return {"found": True, "name": name, "profile": _read(f, cap=4000), "has_code": has_code(name)}
    return {"found": False, "name": name, "profile": "", "has_code": has_code(name)}


# ── Secret code (second factor) — stored HASHED only, never plaintext ─────────
def _codes_file() -> Path:
    return _vault() / "04-Claude-Memory" / "access_codes.json"


def _load_codes() -> dict:
    f = _codes_file()
    try:
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _norm_code(c: str) -> str:
    return "".join(ch for ch in (c or "").lower() if ch.isalnum())


def set_code(name: str, code: str) -> bool:
    c = _norm_code(code)
    if not name or len(c) < 2:
        return False
    data = _load_codes()
    salt = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    data[name.lower()] = {"hash": hashlib.sha256((salt + c).encode()).hexdigest(), "salt": salt}
    try:
        f = _codes_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
        try: os.chmod(f, 0o600)
        except OSError: pass
        return True
    except Exception:
        return False


def verify_code(name: str, code: str) -> bool:
    rec = _load_codes().get((name or "").lower())
    if not rec:
        return False
    c = _norm_code(code)
    return hashlib.sha256((rec.get("salt", "") + c).encode()).hexdigest() == rec.get("hash")


def has_code(name: str) -> bool:
    return (name or "").lower() in _load_codes()


def save_person(name: str, fields: dict) -> bool:
    """Write a person's profile note (role, service line, priorities, etc.)."""
    fields = fields or {}
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines = ["---", "status: active", "project: meta", "type: reference",
             "person: " + (name or ""), "---", "", "# " + (name or "Unknown"), "",
             "_Profile captured " + stamp + " via Martin._", ""]
    order = [("role", "Role"), ("service_line", "Service line"), ("priorities", "Priorities"),
             ("automations", "Automations wanted"), ("context", "Service-line context")]
    seen = set()
    for k, label in order:
        if fields.get(k):
            lines.append("**" + label + ":** " + str(fields[k])); lines.append(""); seen.add(k)
    for k, v in fields.items():
        if k not in seen and v:
            lines.append("**" + str(k) + ":** " + str(v)); lines.append("")
    lines.append("## Sessions")
    try:
        _people_dir().mkdir(parents=True, exist_ok=True)
        _person_file(name).write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def add_session_note(name: str, text: str) -> bool:
    """Log the latest session to the person's note + today's daily note."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    ok = _append_file(_person_file(name), "\n- **" + stamp + "** — " + (text or "").strip(),
                      "---\nstatus: active\nproject: meta\ntype: reference\n---\n# " + (name or "Unknown") + "\n\n## Sessions\n")
    try:
        daily = _vault() / "01-Daily" / (time.strftime("%Y-%m-%d") + ".md")
        _append_file(daily, "\n- **Martin session · " + (name or "") + ":** " + (text or "").strip(),
                     "# " + time.strftime("%A, %B %d, %Y") + "\n")
    except Exception:
        pass
    return ok


def _voiceprints_file() -> Path:
    return _vault() / "04-Claude-Memory" / "voiceprints.json"


def get_voiceprints() -> dict:
    f = _voiceprints_file()
    try:
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"prints": []}


def save_voiceprint(name: str, sig: list) -> bool:
    """Store/refresh a per-person voice signature (rolling average). Local only."""
    if not name or not sig:
        return False
    import math
    sig = [float(x) for x in sig][:64]
    data = get_voiceprints()
    prints = data.get("prints", [])
    found = next((p for p in prints if str(p.get("name", "")).lower() == name.lower()), None)
    if found and isinstance(found.get("sig"), list) and len(found["sig"]) == len(sig):
        n = int(found.get("n", 1))
        avg = [(found["sig"][i] * n + sig[i]) / (n + 1) for i in range(len(sig))]
        nrm = math.sqrt(sum(v * v for v in avg)) or 1.0
        found["sig"] = [v / nrm for v in avg]
        found["n"] = n + 1
    elif found:
        found["sig"] = sig
        found["n"] = 1
    else:
        prints.append({"name": name, "sig": sig, "n": 1})
    data["prints"] = prints
    try:
        f = _voiceprints_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
        return True
    except Exception:
        return False


def capture(kind: str, text: str, title: str = "") -> bool:
    """Dispatch a voice capture to the right vault file: goal | rule | knowledge."""
    k = (kind or "").lower().strip()
    if not (text or "").strip():
        return False
    if k in ("goal", "priority", "task"):
        return add_priority(text)
    if k == "rule":
        return add_rule(text)
    if k in ("knowledge", "note", "memory"):
        return add_knowledge(title, text)
    return add_knowledge(title or "Note", text)


def status() -> dict:
    v = _vault()
    return {"core_files": {k: bool(_read(CORE_DIR / f"{k}.md")) for k in ("identity", "soul", "boundaries", "rules")},
            "agent_files": [Path(p).stem for p in glob.glob(str(AGENTS_DIR / "*.md"))],
            "vault": str(v), "vault_found": v.exists(),
            "memory_chars": len(read_memory())}
