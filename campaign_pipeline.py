"""
campaign_pipeline
─────────────────
Martin-as-manager runs a Customer Value Journey campaign the way the vault's
Jobs describe it: first he plans the funnel (reverse-planned — Profit Maximizer
← Core Offer ← Tripwire ← Lead Magnet ← Content), then he delegates each
buildable piece to the specialist Job, grounded on that Job's own description
from the Obsidian vault (02-Knowledge/Marketing/Jobs).

Every stage is DRAFT-ONLY (never publishes or sends) and carries the healthcare
guardrails from the Jobs: no PHI, no unsupported health claims, consent language
on any form, and activation routes through the compliance gate.
"""
from __future__ import annotations

import os
import re
import asyncio
import logging
import datetime

log = logging.getLogger("campaign_pipeline")


def _mkt_dir():
    import martin_core
    return martin_core._vault() / "02-Knowledge" / "Marketing"


def _jobs_dir():
    return _mkt_dir() / "Jobs"


def _read_job(fname: str, cap: int = 4000) -> str:
    try:
        return _jobs_dir().joinpath(fname).read_text(encoding="utf-8")[:cap]
    except Exception:
        return ""


def _read_skill(fname: str, cap: int = 2600) -> str:
    """Read a jaredrhod skill note from the vault's Marketing folder (the 'boot chain'
    each Job points at) so the specialist actually works from Jared's real playbook."""
    try:
        return _mkt_dir().joinpath(fname).read_text(encoding="utf-8")[:cap]
    except Exception:
        return ""


# The CVJ pipeline: each stage = one Job/specialist. Stage 1 (Plan a Funnel) is
# Martin the manager; its output becomes shared context for the rest.
STAGES = [
    {"key": "funnel", "title": "Funnel plan", "job": "Plan a Funnel.md",
     "role": "Martin, the marketing manager",
     "skills": ["the-fundamentals.md"],
     "task": "Map the complete funnel for this offer, reverse-planned (Profit Maximizer <- Core Offer <- Tripwire <- Lead Magnet <- Content). For each stage give one line: what it is here and the desired feeling/action. Name the key metric per stage (opt-in rate, tripwire conversion, core take-rate, LTV). End with which pieces to build first."},
    {"key": "lead_magnet", "title": "Lead magnet", "job": "Create a Lead Magnet.md",
     "role": "the Content specialist",
     "skills": ["marketing-lead-magnets.md", "marketing-copywriting.md"],
     "task": "Design the lead magnet: the ONE big question it answers, the best fast-to-consume format, the squeeze-page headline + opt-in line (target 35%+ opt-in), a short outline of the deliverable, and the delivery + hand-off into the tripwire."},
    {"key": "tripwire", "title": "Tripwire offer", "job": "Write a Sales Page.md",
     "role": "the Content specialist",
     "skills": ["marketing-sales-letter.md", "marketing-copywriting.md"],
     "task": "Create the tripwire: a low-ticket splinter of the core offer (break-even is fine) that turns a fresh lead into a first-time buyer. Give the offer and price logic, then a short sales page (headline, promise, bullets, the offer, CTA)."},
    {"key": "core_offer", "title": "Core offer", "job": "Write a Sales Page.md",
     "role": "the Content specialist",
     "skills": ["marketing-sales-letter.md", "marketing-copywriting.md"],
     "task": "Define the core offer (the main thing we sell, front door always open) and write its sales page: headline, the transformation, proof, the offer stack, price framing, and CTA."},
    {"key": "maximizer", "title": "Profit maximizer + nurture email", "job": "Write a Marketing Email.md",
     "role": "the Email & CRM specialist",
     "skills": ["marketing-email.md"],
     "task": "Define the profit maximizer (the recurring or add-on that makes the real money) and write ONE nurture/ascension email that moves a core-offer buyer toward it: subject line, preview text, body, and one clear CTA."},
]


async def _run_stage(prov, stage: dict, brief: str, funnel_plan: str, fundamentals: str, principles: str) -> dict:
    import llm_router
    job = _read_job(stage["job"])
    # Load the 'boot chain' — the actual jaredrhod skill notes this Job points at,
    # so the specialist works from Jared's real playbook, not just the procedure.
    skills = "\n\n".join(filter(None, (_read_skill(f) for f in stage.get("skills", []))))
    system = (
        f"You are {stage['role']} on Martin's marketing team, executing the Job below exactly. "
        "Follow its procedure and hit its quality bar. Write in a warm, direct, plain voice with NO em-dashes and no hype. "
        "This is a DRAFT for review: do not publish or send anything. "
        "Healthcare compliance: collect no PHI, make no unsupported health claims, include consent language wherever a form is involved, "
        "and note that every activation stage routes through the compliance gate before going live.\n\n"
        f"## How Jared actually thinks about marketing (apply this voice + judgment)\n{(principles or '')[:2200]}\n\n"
        f"## Your Job (from the playbook)\n{job}\n\n"
        f"## Your skill playbook (jaredrhod)\n{skills or '(none loaded)'}\n\n"
        f"## The funnel framework (Ryan Deiss CVJ)\n{(fundamentals or '')[:2200]}"
    )
    user = f"## The offer / business brief\n{brief}\n\n"
    if funnel_plan and stage["key"] != "funnel":
        user += f"## The funnel plan (from Martin, build against this)\n{funnel_plan[:1800]}\n\n"
    user += f"## Your task\n{stage['task']}\n\nProduce the deliverable now, in clean markdown. Be concrete and specific to THIS offer."
    try:
        r = await llm_router.complete(prov, system, [{"role": "user", "content": user}], max_tokens=900)
        if r.get("error"):
            return {"key": stage["key"], "title": stage["title"], "content": f"_(couldn't generate this stage: {r['error']})_", "error": True}
        return {"key": stage["key"], "title": stage["title"], "content": (r.get("output") or "").strip()}
    except Exception as e:
        log.warning("stage %s failed: %s", stage["key"], e)
        return {"key": stage["key"], "title": stage["title"], "content": f"_(this stage errored: {e})_", "error": True}


async def run_campaign(brief: str) -> dict:
    """Plan the funnel, then delegate the four pieces to their specialist Jobs."""
    import system_llm, martin_core
    system_llm.ensure_seed()
    prov = system_llm.resolve(
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "") or None,
        anthropic_model=os.getenv("JARVIS_MARTIN_MODEL", "claude-sonnet-5"),
        name="Claude")
    if not prov:
        return {"ok": False, "error": "No model available — pick one in Admin, System model."}
    fundamentals = ""
    try:
        fundamentals = martin_core.read_funnel_playbook() or ""
    except Exception:
        pass
    principles = _read_skill("jareds-takes.md", cap=2600)   # Jared's real, run-for-money takes
    # Stage 1: the funnel plan (manager). Then delegate the rest concurrently.
    funnel = await _run_stage(prov, STAGES[0], brief, "", fundamentals, principles)
    plan_text = funnel.get("content", "")
    rest = await asyncio.gather(*[_run_stage(prov, s, brief, plan_text, fundamentals, principles) for s in STAGES[1:]])
    return {"ok": True, "brief": brief, "stages": [funnel] + list(rest)}


def save_campaign(brief: str, stages: list) -> str:
    """Write the assembled campaign to the Obsidian vault (03-Projects/Campaigns)."""
    import martin_core
    v = martin_core._vault()
    d = v / "03-Projects" / "Campaigns"
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (brief or "campaign").lower()).strip("-")[:40] or "campaign"
    fn = d / (datetime.date.today().isoformat() + "-" + slug + ".md")
    body = (f"# Campaign — {brief}\n\n"
            "_Drafted by Martin's marketing team along the Customer Value Journey. "
            "Draft only — review before anything is published or sent._\n\n")
    for s in (stages or []):
        body += f"## {s.get('title', '')}\n\n{s.get('content', '')}\n\n"
    fn.write_text(body, encoding="utf-8")
    return str(fn)
