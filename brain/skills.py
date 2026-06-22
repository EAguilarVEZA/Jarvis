"""The seven scheduled skills.

Each skill follows the same loop: **read context → think with Claude → write a
Markdown output → store a short residue in memory**. The residue is what makes
the system compound: next run (and the live voice assistant) sees it.

Skills are pure-ish: they take an Anthropic async client and return a SkillResult.
The runner handles scheduling, output routing, memory writes and retries.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass

from . import context

log = logging.getLogger("jarvis.brain.skills")

BRAIN_MODEL = os.getenv("JARVIS_BRAIN_MODEL", "claude-sonnet-4-6")

_BASE_SYSTEM = (
    "You are JARVIS, a personal intelligence system running as a background loop "
    "for one person. Ground everything in their OPERATING DOC and notes — never "
    "invent facts. Be specific, honest and concise. Prefer signal over volume. "
    "Write clean Markdown with short sections. If the inputs are thin, say so "
    "plainly and keep the output short rather than padding it."
)


@dataclass
class SkillResult:
    title: str
    body: str
    memory_note: str = ""
    importance: int = 5


def _block(label: str, text: str, cap: int = 1500) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return f"\n\n## {label}\n{text[:cap]}"


def _mem_lines(mems: list[dict], cap: int = 10) -> str:
    out = []
    for m in mems[:cap]:
        t = m.get("type") or m.get("mem_type") or "note"
        out.append(f"- [{t}] {m.get('content','')}")
    return "\n".join(out)


def _task_lines(tasks: list[dict], cap: int = 12) -> str:
    out = []
    for t in tasks[:cap]:
        due = f" (due {t['due_date']})" if t.get("due_date") else ""
        out.append(f"- [{t.get('priority','med')}] {t.get('title','')}{due}")
    return "\n".join(out)


def _notes_text(notes: list[tuple[str, str]], cap: int = 700) -> str:
    return "\n\n".join(f"### {n}\n{txt.strip()[:cap]}" for n, txt in notes)


async def _call(client, system: str, user: str, max_tokens: int = 1600) -> str:
    resp = await client.messages.create(
        model=BRAIN_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


class Skill:
    name = "skill"
    label = "Skill"
    cron = "0 6 * * *"
    subdir = "briefings"
    description = ""
    importance = 5
    max_tokens = 1600

    def build_prompt(self, ctx: dict) -> str:
        raise NotImplementedError

    async def run(self, client) -> SkillResult:
        ctx = context.gather_inputs()
        user = self.build_prompt(ctx)
        body = await _call(client, _BASE_SYSTEM, user, self.max_tokens)
        title = f"{self.label} — {_dt.date.today().isoformat()}"
        note = (body.strip().splitlines() or [""])[0].lstrip("# ").strip()
        return SkillResult(title=title, body=body, memory_note=f"{self.label}: {note}"[:280],
                           importance=self.importance)


# ── Skill 1 — Morning brief (daily 6AM) ──────────────────────────────────
class MorningBrief(Skill):
    name, label, cron, subdir = "morning_brief", "Morning Brief", "0 6 * * *", "briefings"
    description = "Daily 6AM mission control: the one thing, open loops, project status, intel, decision flags."
    importance = 6

    def build_prompt(self, c):
        return (
            "Write today's MORNING BRIEF. Keep it under ~250 words, scannable, vault-grounded.\n"
            "Sections, in this order:\n"
            "**THE ONE THING** — the single highest-impact priority today.\n"
            "**OPEN LOOPS** — commitments/tasks/decisions pending.\n"
            "**PROJECT STATUS** — progress, blockers, next steps.\n"
            "**INTELLIGENCE** — signals worth knowing (only if present in the notes).\n"
            "**DECISION FLAG** — anything that needs the user's choice today.\n"
            + _block("OPERATING DOC", c["operating_doc"])
            + _block("YESTERDAY'S DAILY NOTE", c["daily_note"])
            + _block("OPEN TASKS", _task_lines(c["open_tasks"]))
            + _block("ACTIVE PROJECTS", _notes_text(c["projects"]))
            + _block("RECENT MEMORY", _mem_lines(c["important_memories"]))
            + _block("PREVIOUS BRIEF", c["last_brief"], cap=600)
        )


# ── Skill 2 — Capture processor (daily 8PM) ──────────────────────────────
class CaptureProcessor(Skill):
    name, label, cron, subdir = "capture_processor", "Capture Processor", "0 20 * * *", "captures"
    description = "Daily 8PM: sorts raw captures into idea/task/decision/reference/insight and preps tomorrow."

    def build_prompt(self, c):
        return (
            "Process today's raw captures. For EACH item, classify and route it:\n"
            "IDEA → permanent knowledge · TASK → project/inbox · DECISION → decision note · "
            "REFERENCE → resources · INSIGHT → insight note.\n"
            "Output a table (Capture | Type | Where it goes | One-line why), then:\n"
            "**CLAUDE.md updates** — any facts/preferences worth adding to the operating doc.\n"
            "**Prepare tomorrow** — 3 bullets: top tasks + one thing to watch.\n"
            "Nothing should stay unprocessed.\n"
            + _block("TODAY'S CAPTURES (inbox)", _notes_text(c["inbox"]))
            + _block("OPERATING DOC", c["operating_doc"], cap=900)
            + _block("OPEN TASKS", _task_lines(c["open_tasks"]))
        )


# ── Skill 3 — Connection finder (nightly 11PM) ───────────────────────────
class ConnectionFinder(Skill):
    name, label, cron, subdir = "connection_finder", "Connection Finder", "0 23 * * *", "connections"
    description = "Nightly 11PM: finds a non-obvious link between recent notes and older history."
    importance = 6

    def build_prompt(self, c):
        return (
            "Find ONE genuinely non-obvious connection between something from the last few "
            "days and something older. Skip obvious same-topic overlaps — surprising links only.\n"
            "Output:\n**NOTE A ↔ NOTE B** — the two things being connected.\n"
            "**WHAT THIS REVEALS** — the pattern, relationship or emergent theme.\n"
            "**IMPLICATION** — why it matters and what to do with it.\n"
            "**SURPRISE LEVEL** — 1–5 and one line on why.\n"
            "If nothing rises above 'obvious', say so in one line instead of forcing it.\n"
            + _block("RECENT NOTES (last few days)", _notes_text(c["recent_daily"]))
            + _block("KNOWLEDGE / OLDER NOTES", _notes_text(c["knowledge"]))
            + _block("MEMORY", _mem_lines(c["recent_memories"]))
        )


# ── Skill 4 — Weekly synthesis (Sunday 7PM) ──────────────────────────────
class WeeklySynthesis(Skill):
    name, label, cron, subdir = "weekly_synthesis", "Weekly Synthesis", "0 19 * * 0", "syntheses"
    description = "Sunday 7PM: the week understood — what advanced, what stalled, belief updates, next priority."
    importance = 7
    max_tokens = 1900

    def build_prompt(self, c):
        return (
            "Write this week's SYNTHESIS — say what no single note says. Sections:\n"
            "**The week in one honest line.**\n**What advanced** — wins/progress.\n"
            "**What stalled** — blockers & friction.\n**Belief updates** — what changed and why.\n"
            "**The week's pattern** — the recurring theme.\n"
            "**Next week's single priority** — the one thing that moves everything.\n"
            + _block("OPERATING DOC", c["operating_doc"])
            + _block("DAILY NOTES THIS WEEK", _notes_text(c["recent_daily"]))
            + _block("PROJECTS", _notes_text(c["projects"]))
            + _block("MORNING BRIEFS / MEMORY", _mem_lines(c["important_memories"]))
            + _block("LAST WEEK'S SYNTHESIS", c["last_synthesis"], cap=700)
        )


# ── Skill 5 — Belief tracker (Monday 8AM) ────────────────────────────────
class BeliefTracker(Skill):
    name, label, cron, subdir = "belief_tracker", "Belief Tracker", "0 8 * * 1", "beliefs"
    description = "Monday 8AM: tests current beliefs against the last 7 days of evidence."
    importance = 6

    def build_prompt(self, c):
        return (
            "Audit the user's stated beliefs (from the operating doc) against the last 7 days "
            "of notes. For each relevant belief decide: CONFIRMS / CHALLENGES / COMPLICATES / "
            "IRRELEVANT. Report only what matters:\n"
            "**Confirmed this week** — held up under evidence.\n"
            "**Challenged this week** — didn't hold up, and why.\n"
            "**Severity** — how much this should move the belief (low/med/high).\n"
            "**Recommended action** — adjust / test / stop. Honest challenge beats confirmation bias.\n"
            + _block("OPERATING DOC (beliefs in §5)", c["operating_doc"])
            + _block("LAST 7 DAYS OF NOTES", _notes_text(c["recent_daily"]))
            + _block("MEMORY", _mem_lines(c["recent_memories"]))
        )


# ── Skill 6 — Pattern detector (1st of month) ────────────────────────────
class PatternDetector(Skill):
    name, label, cron, subdir = "pattern_detector", "Pattern Detector", "0 9 1 * *", "patterns"
    description = "Monthly: surfaces recurrence, convergence, contradiction, emergence and blind spots."
    importance = 7
    max_tokens = 1900

    def build_prompt(self, c):
        return (
            "Scan a month of decisions, insights, projects and observations for ONE real pattern "
            "(one real pattern beats ten superficial ones). Consider these lenses:\n"
            "RECURRENCE (keeps showing up) · CONVERGENCE (threads coming together) · "
            "CONTRADICTION (data vs belief) · EMERGENCE (new theme forming) · BLIND SPOT (what's avoided/missing).\n"
            "Only claim a pattern if 4+ independent notes support it. Output:\n"
            "**Pattern** · **Type** · **Evidence (the notes)** · **What it means** · "
            "**Recommended action** (the single highest-leverage move).\n"
            + _block("OPERATING DOC", c["operating_doc"], cap=900)
            + _block("PROJECTS", _notes_text(c["projects"]))
            + _block("KNOWLEDGE", _notes_text(c["knowledge"]))
            + _block("MEMORY (decisions, insights)", _mem_lines(c["important_memories"], cap=14))
        )


# ── Skill 7 — Decision intelligence (weekly review, Fri 5PM) ──────────────
class DecisionIntelligence(Skill):
    name, label, cron, subdir = "decision_intelligence", "Decision Intelligence", "0 17 * * 5", "decisions"
    description = "Weekly: structures new decisions and reviews past ones against today's evidence."

    def build_prompt(self, c):
        return (
            "Two jobs. (A) STRUCTURE any new decision in the recent notes: what was decided, the "
            "critical assumption that must be true, what success looks like, and early-warning signs. "
            "(B) REVIEW past decisions in memory against evidence now: status (valid / challenged / "
            "invalidated) and recommended action (continue / pivot / sunset) with a one-line why. "
            "Make assumptions explicit; review against evidence.\n"
            + _block("OPERATING DOC", c["operating_doc"], cap=900)
            + _block("RECENT NOTES", _notes_text(c["recent_daily"]))
            + _block("PAST DECISIONS / MEMORY", _mem_lines(c["important_memories"], cap=14))
        )


# Registry — order = display order.
SKILLS: list[Skill] = [
    MorningBrief(), CaptureProcessor(), ConnectionFinder(), WeeklySynthesis(),
    BeliefTracker(), PatternDetector(), DecisionIntelligence(),
]
BY_NAME: dict[str, Skill] = {s.name: s for s in SKILLS}


def get(name: str) -> Skill | None:
    return BY_NAME.get(name)
