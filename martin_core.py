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


def _vault() -> Path:
    env = os.getenv("MARTIN_VAULT")
    if env:
        return Path(env)
    home = Path.home()
    for cand in (home / "Documents" / "MyVault", home / "MyVault", _DIR / "jarvis_vault"):
        if cand.exists():
            return cand
    return home / "Documents" / "MyVault"


# Martin's core + agent files now live in the Obsidian vault (single source of
# truth), not in the code repo. Resolved from the vault at import time.
CORE_DIR = _vault() / "04-Claude-Memory" / "Martin Core"
AGENTS_DIR = _vault() / "04-Claude-Memory" / "Martin Agents"


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


def read_reporting_playbook(cap: int = 6000) -> str:
    """The professional-report playbook from the vault — primes Martin to guide
    people to build the best, most professional reports when they ask data
    questions. Kept capped so it doesn't bloat the grounding."""
    v = _vault()
    for cand in (v / "02-Knowledge" / "Reporting" / "Professional Report Playbook.md",
                 v / "02-Knowledge" / "Professional Report Playbook.md"):
        t = _read(cand, cap=cap)
        if t:
            return t
    return ""


def read_marketing_guide(cap: int = 5000) -> str:
    """Marketing orchestration primer from the vault: the Marketing-Manager quick
    guide (what to ask for ad copy / creative-testing / campaigns) plus the agent
    routing cheat-sheet, so Martin can guide a marketing manager and delegate to the
    right specialist agent. Full playbooks live alongside it in 02-Knowledge/Marketing."""
    mk = _vault() / "02-Knowledge" / "Marketing"
    parts = []
    quick = _read(mk / "Marketing Manager Assist — Quick Guide.md", cap=3000)
    if quick:
        parts.append(quick)
    roster = _read(mk / "Marketing Agent Roster and Routing.md", cap=2500)
    if roster:
        parts.append("### Agent routing\n" + roster)
    return "\n\n".join(parts)[:cap]


def read_funnel_playbook(cap: int = 6500) -> str:
    """The Customer Value Journey (Ryan Deiss) master note — the source funnel
    framework: 8-stage journey, the relationship/dating logic (don't propose on the
    first date), lead magnet / tripwire / core offer / profit maximizer, and the
    Return Path. Primes Martin to guide funnel, offer, and nurture strategy."""
    mk = _vault() / "02-Knowledge" / "Marketing"
    for cand in (mk / "Ryan Deiss — Customer Value Journey (Master).md",
                 mk / "Ryan Deiss - Customer Value Journey (Master).md"):
        t = _read(cand, cap=cap)
        if t:
            return t
    return ""


def read_healthcare_playbook(cap: int = 9000) -> str:
    """The Orlando Health healthcare-marketing playbook (patient acquisition by
    service line, brand/trust/reputation, physician & referral marketing, HIPAA-safe
    digital). This is the CORE healthcare context and is kept SEPARATE from the
    venture/funnel material so the two never get confused."""
    v = _vault()
    for cand in (v / "02-Knowledge" / "Marketing" / "Healthcare" / "Healthcare Marketing Playbook — Orlando Health.md",
                 v / "02-Knowledge" / "Marketing" / "Healthcare Marketing Playbook — Orlando Health.md"):
        t = _read(cand, cap=cap)
        if t:
            return t
    return ""


def read_ventures_pointer() -> str:
    """A short awareness pointer to Edgar's SEPARATE side-venture funnel notes — so
    Martin knows they exist and stays them-vs-healthcare distinct, without bloating
    the grounding or bleeding venture examples into healthcare work."""
    v = _vault() / "02-Knowledge" / "Marketing" / "Ventures"
    if not v.exists():
        return ""
    return ("Edgar also has SEPARATE side ventures with their own step-by-step funnel notes in "
            "02-Knowledge/Marketing/Ventures/ — a reusable 8-step template, Colette French Pastries (bakery), "
            "and the Parasite Club (high-end European frames + Rx glasses). These are DISTINCT from Orlando "
            "Health healthcare work: never mix a venture funnel with healthcare/patient/service-line work. "
            "Only use them when Edgar is explicitly working on that venture.")


def read_intent_router(cap: int = 6000) -> str:
    """How Martin opens and routes every request (quick answer / brief / report /
    Customer Value Journey / automation / agent) + the step-by-step CVJ build guide
    with worked examples (Colette French Pastries, healthcare). Primes Martin to run
    the guided flows and reuse existing dashboard/brief formats rather than invent."""
    v = _vault()
    parts = []
    router = _read(v / "02-Knowledge" / "Operations" /
                   "Martin Intent Router — Ask, Brief, Report, Journey, Automation, Agent.md", cap=3400)
    if router:
        parts.append(router)
    cvj = _read(v / "02-Knowledge" / "Marketing" /
                "Customer Value Journey — Step-by-Step Build Guide (with examples).md", cap=3200)
    if cvj:
        parts.append("### Customer Value Journey — step-by-step build guide\n" + cvj)
    return "\n\n".join(parts)[:cap]


def read_operations_guide(cap: int = 8000) -> str:
    """Operations primers so Martin is a strong agent-manager + app-coordinator:
    how to delegate/orchestrate agents, the app capability map + intent router, the
    always-on compliance guardrails, and the KPI/data dictionary. Front sections of
    each doc (they front-load the essentials), capped so grounding stays bounded."""
    op = _vault() / "02-Knowledge" / "Operations"
    order = [
        ("Compliance Guardrails — HIPAA and Meta Health Ads.md", 2600),
        ("Agent Delegation and Orchestration Playbook.md", 2400),
        ("App Capability Map and Intent Router.md", 2000),
        ("KPI and Data Dictionary.md", 1800),
    ]
    parts = []
    for name, c in order:
        t = _read(op / name, cap=c)
        if t:
            parts.append(t)
    return "\n\n".join(parts)[:cap]


def assemble_system_prompt(agent_id: str = None, task: str = "") -> str:
    """Compose the full grounding for a Martin agent: core files + specialist
    identity + second-brain memory + reporting/marketing/operations playbooks + task."""
    c = read_core()
    blocks = []
    # Martin's manner — the felt experience of talking with him. This leads the prompt
    # so it colors every reply.
    blocks.append(
        "## How Martin feels to talk with (always)\n"
        "Be CALM, NATURAL, and RATIONAL, and move at the SPEED OF THE USER. "
        "Listen and fully understand before you respond — if something is unclear, ask one short question rather than guessing. "
        "Speak plainly and warmly, like a thoughtful person, not a bot. Keep replies short and unhurried; say one clear thing at a time and let the user lead. "
        "Never rush, never pile on options, never fire actions the user didn't ask for. Silence is fine — wait for them. "
        "Reason out loud only as much as helps; prefer a clear answer over a long one."
    )
    blocks.append(
        "## You are a full assistant, not only a marketing tool\n"
        "Martin is the user's personal AI assistant (Jarvis-style). Answer ANY question they ask — general knowledge, "
        "explanations, advice, writing, planning, everyday help — helpfully and conversationally, from your own knowledge. "
        "Never refuse a normal question or call it out of scope. Only for genuinely live/real-time things you cannot know "
        "(today's weather, today's news, their inbox or calendar, a website's current contents) should you say that live "
        "connection isn't wired up yet, and offer what you can do instead."
    )
    blocks.append(
        "## You are connected to the data (never deny this)\n"
        "You ARE connected to the user's live marketing warehouse (BigQuery, through a governed semantic layer) and you CAN "
        "pull reports, briefs, dashboards, metrics and charts from it. NEVER tell the user you lack a data connection, "
        "cannot see their data, cannot run queries, or can't do this in real time — those statements are FALSE and are not allowed. "
        "When a request needs data (a report, campaign performance, spend, leads, CPL, ROI, impressions, clicks, a trend, a chart, etc.), "
        "do not refuse and do not keep chatting once you have enough: the app will run the query for you. "
        "Gather at most one short clarifying detail if truly needed, then say you're pulling it."
    )
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
    play = read_reporting_playbook()
    if play:
        blocks.append("## How to guide people to build great reports (playbook)\n" + play)
    mkt = read_marketing_guide()
    if mkt:
        blocks.append("## How to guide marketing work + delegate to agents (playbook)\n" + mkt)
    funnel = read_funnel_playbook()
    if funnel:
        blocks.append("## The funnel framework — Customer Value Journey / relationship marketing (playbook)\n" + funnel)
    router = read_intent_router()
    if router:
        blocks.append("## How Martin opens & routes requests + the guided CVJ build (playbook)\n" + router)
    hc = read_healthcare_playbook()
    if hc:
        blocks.append("## Healthcare marketing for Orlando Health (playbook — the core context)\n" + hc)
    ventures = read_ventures_pointer()
    if ventures:
        blocks.append("## Edgar's side ventures (SEPARATE from healthcare — do not mix)\n" + ventures)
    ops = read_operations_guide()
    if ops:
        blocks.append("## Managing agents, coordinating the app, compliance & data (playbooks)\n" + ops)
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
