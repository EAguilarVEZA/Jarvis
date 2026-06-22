# Jarvis Expansion — Living Plan & Slide Documentation

> Source: AXIAL STUDIO carousel "Build Jarvis inside Obsidian" (~25–30 slides).
> Purpose: (A) deploy a live website on Vercel, and (B) infuse the existing Jarvis
> app with the "active personal-intelligence operating loop" the deck describes.
> Status: intake in progress — slides 1–18 documented (slide 17 not yet received).

---

## Part 1 — Deck synthesis (the method, slide by slide)

### Concept (slides 1–4)
- **Slide 1 — Build Jarvis inside Obsidian.** Not a voice assistant; a *personal
  intelligence system*. It reads your notes, runs on a schedule, finds patterns,
  gets smarter every week. Foundation = vault + Claude + memory + skills.
- **Slide 2 — Not a voice assistant.** Runs in the background of your life.
  Typical assistant → Jarvis: waits for prompt → reads notes automatically;
  answers a question → tracks projects & decisions; easy to forget → surfaces
  patterns & open loops; → improves every week. "Chat tool → background
  intelligence layer."
- **Slide 3 — Week 1 → Week 12.** Useful becomes indispensable. Wk1 beats a manual
  recap; Wk4 connects last month's decision to today's problem; Wk12 spots a work
  pattern hidden for 3 months. "Compounding context changes output quality."
- **Slide 4 — Passive vs active vault.** Passive: you write notes, they sit, you
  search later. Active: reads notes automatically, generates outputs on schedule,
  surfaces missed connections, applies what it learned last week. "Difference is
  not storage. Difference is execution."

### Architecture & why it compounds (slides 5–6)
- **Slide 5 — Core architecture.** Obsidian Vault + Filesystem MCP + Claude
  (reasoning/planning/execution) + Skills + Memory DB + scheduled Outputs +
  Notifications. "Not a plugin stack. An operating loop."
- **Slide 6 — Why it gets smarter.** Three compounding mechanisms: (1) accumulated
  memory (every run writes context to persistent memory), (2) skill refinement
  (you fix a weak output; future runs inherit the fix), (3) pattern recognition
  (months of notes reveal themes you can't see manually). "Each run leaves
  residue. Residue improves the next run."

### Build steps (slides 7–14)
- **Slide 7 — Foundation stack.** Install: Claude Desktop (brain/executor),
  Node.js (runtime), Obsidian (knowledge hub), Filesystem MCP (file ops),
  Hermes Agent (orchestration), SQLite memory, Telegram notifications (optional);
  `npx jarvis init`.
- **Slide 8 — Vault structure.** `JARVIS-VAULT/`: `00-INBOX/`, `01-KNOWLEDGE/`,
  `02-PROJECTS/`, `03-DAILY/`, `04-JARVIS-OUTPUTS/` (briefings, connections,
  syntheses, reviews), `05-RESOURCES/` (templates, playbooks, prompts),
  `06-ARCHIVE/`, `07-SYSTEM/` (CLAUDE.md, skills/, memory/). "Outputs + system
  live inside the same vault."
- **Slide 9 — Connect Claude to the vault (Filesystem MCP).** In
  `claude_desktop_config.json`: `mcpServers.jarvis-vault` → `npx -y
  @modelcontextprotocol/server-filesystem /Users/<you>/JARVIS-VAULT`. Test:
  "List all folders in my vault and describe what you see." If Claude sees the
  folder tree, the connection is live.
- **Slide 10 — Automation layer (Hermes Agent).** `git clone .../hermes-agent`,
  `npm install`. Hermes = Scheduler (cron + natural language), Retries (backoff,
  3 attempts), Memory (context across runs), Outputs (structured summaries/
  reports), Telegram (notifications). "Hermes runs skills on schedule."
- **Slide 11 — Configure `.env`.** MODEL (`anthropic`, `claude-opus-4-8`, API key);
  MEMORY (`sqlite`, path `…/07-SYSTEM/memory/jarvis.db`); SCHEDULER & OUTPUTS
  (timezone, output path `…/04-JARVIS-OUTPUTS`); SKILLS (retry enabled, max 3,
  delay 300s); NOTIFICATIONS (telegram gateway, bot token, chat id). "Memory
  backs up with the vault."
- **Slide 12 — Write CLAUDE.md (10 sections).** 1 Identity, 2 How I actually work,
  3 Focus areas, 4 Active projects, 5 Current beliefs, 6 Active questions,
  7 Output standards, 8 Permissions, 9 Memory rules, 10 Update protocol.
  "Every skill reads this first. Best outputs start with the best operating doc."
- **Slide 13 — What goes inside CLAUDE.md.** Concrete cards: Identity (name/role/
  timezone), Work style (best hours, derailers, energizing tasks), Focus areas
  (priorities, goals, boundaries), Active projects (status, next action,
  priority), Beliefs (principles, how you decide), Questions (open items,
  decisions, unknowns), Decisions (recent calls, rationale), Output standards
  (tone/voice, format, quality bar), Permissions (can do, avoid, escalate-when).
  "Write how you *actually* work, not the idealized version."
- **Slide 14 — Boundaries & permissions.** Jarvis CAN: read any vault file, write
  to `04-JARVIS-OUTPUTS`, search relevant external info, update the memory DB.
  REQUIRES APPROVAL: write outside the outputs folder, contact external parties,
  financial/strategic recommendations beyond analysis. "Freedom + guardrails."

### The seven skills (slides 15–18+)
- **Slide 15 — Seven skills overview.** Scheduled systems that make Jarvis
  compound: (1) Morning brief — daily 6AM, (2) Capture processor — daily 8PM,
  (3) Connection finder — nightly 11PM, (4) Weekly synthesis — Sun 7PM,
  (5) Belief tracker — Mon 8PM, (6) Pattern detector — 1st of month,
  (7) Decision intelligence — structure + review decisions. "Each skill reads
  context, writes output, stores memory."
- **Slide 16 — Skill 1: Morning brief (6AM).** Inputs: CLAUDE.md, yesterday's
  daily note, active projects, recent briefs, external intelligence. Output
  sections: THE ONE THING, OPEN LOOPS, PROJECT STATUS, INTELLIGENCE, DECISION
  FLAG. "Under 5 min. Specific. Vault-grounded."
- **Slide 17 — Skill 2: Capture processor (daily 8PM).** Takes today's raw captures
  (ideas, tasks, decisions, references, insights) and routes each: IDEA → permanent
  knowledge, TASK → project or inbox, DECISION → decision note, REFERENCE →
  resources, INSIGHT → insight note. Then updates CLAUDE.md (keep memory/context
  fresh) and prepares tomorrow (review tasks, plan next day). "Nothing stays in
  Captures after processing." (Inbox-zero for thoughts.)
- **Slide 18 — Skill 3: Connection finder (11PM).** Inputs: recent notes (48h),
  vault search (related history), strong-connection test (new insight, not same
  topic). Output — connection note: NOTE A ↔ NOTE B, WHAT THIS REVEALS (pattern/
  relationship/emergent theme), IMPLICATION (why it matters + what to do),
  SURPRISE LEVEL. "Surprising links > obvious overlap."
- **Slide 19 — Skill 4: Weekly synthesis (Sun 7PM).** Inputs: daily notes,
  permanent notes, connection notes, morning briefs, CLAUDE.md. Output: week in
  one honest line, what advanced, what stalled, belief updates, the week's
  pattern, next week's single priority. "Must say what no single note says."
- **Slide 20 — Skill 5: Belief tracker (Mon 8AM).** Inputs: current beliefs + last
  7 days of notes. Classifies evidence as Confirms / Challenges / Complicates /
  Irrelevant. Reports only what matters: confirmed this week, challenged this week
  (+ why), severity, recommended action (adjust/test/stop). "Honest challenge
  beats confirmation bias."
- **Slide 21 — Skill 6: Pattern detector (1st of month).** Scans decisions,
  insights, projects, references, observations, questions for: Recurrence,
  Convergence, Contradiction, Emergence, Blind spot. Requires 4+ independent
  notes; recommends extracting + validating the highest-leverage pattern.
  "One real pattern > ten superficial ones."
- **Slide 22 — Skill 7: Decision intelligence.** (A) Structure a new decision:
  what was decided, critical assumption, what success looks like, early-warning
  signs. (B) Review past decisions: evidence now, status (valid / challenged /
  invalidated), recommended action (continue / pivot / sunset + why). "Make
  assumptions explicit. Review against evidence."

### Why it compounds & the rollout (slides 23–25)
- **Slide 23 — Why it gets smarter every week.** Three compounding loops:
  (1) memory accumulation (more runs = more context), (2) CLAUDE.md calibration
  (weekly updates sharpen outputs), (3) skill refinement (patterns reveal what to
  improve). "More context + better calibration + refined skills."
- **Slide 24 — What Week 12 looks like.** Wk1 useful brief → Wk4 surprising links
  → Wk8 better calibration → Wk12 real pattern recognition. Week-12 system can:
  morning brief (knows real priorities), connection finder (non-obvious links),
  belief tracker (flags stale assumptions), decision reviews (past calls vs
  evidence), pattern detector (recurring dynamics). "It feels like it knows you
  because it has evidence."
- **Slide 25 — Build foundation this weekend.** (1) Sat AM: install tools, create
  vault, connect MCP. (2) Sat PM: write CLAUDE.md, run first morning brief.
  (3) Sat night: build capture processor + connection finder. (4) Sun: add
  remaining skills + scheduler. (5) Week 1: let it run, don't over-tune. (6) End
  of Wk1: review outputs, refine CLAUDE.md + skills. "Useful → trusted →
  indispensable."

### Deck status: COMPLETE (25 slides, all received).

---

## Part 2 — Mapping the method to the existing Jarvis app

The current app already has many of the building blocks, which means most of this
is *assembly + scheduling*, not greenfield:

| Deck concept            | Already in the repo                                  |
|-------------------------|------------------------------------------------------|
| Memory DB (SQLite)      | `memory.py` (SQLite + FTS5 full-text search)         |
| Notes / knowledge       | `notes_access.py`, semantic layer, dashboards        |
| Skills / workflows      | skills concept, `work_mode.py`, `actions.py`         |
| Scheduler               | scheduling capability (cron-style)                   |
| Outputs / briefs        | `brief_engine.py`, `briefs_api.py`, reporting module |
| CLAUDE.md operating doc | `CLAUDE.md` exists at repo root                      |
| External intelligence   | `browser.py`, connectors, calendar/mail              |

So the "operating loop" expansion = add a **scheduled, self-improving layer** that
(1) reads a knowledge store on a schedule, (2) writes structured outputs back,
(3) persists compounding memory, and (4) refines skills over time.

---

## Part 3 — Overall plan (two tracks)

### Track A — Vercel live website
The current app is a **macOS-bound FastAPI backend** (AppleScript for Calendar/
Mail/Notes, local SQLite, a BigQuery service-account key, WebSocket collab, serves
`martin_app.html` from disk). That cannot run on Vercel as-is. Options:
- **A1. Marketing/landing site from these slides** (clean Vercel fit; fast).
- **A2. Public demo of the app UI** (frontend only; data calls won't work without
  the backend).
- **A3. Full app** → needs a real backend host (Render/Fly/Railway/a VM), not
  Vercel; plus secrets management for the BigQuery key and removing macOS-only
  AppleScript paths. (Vercel could still host a marketing front door.)

### Track B — Jarvis "operating loop" expansion (phased)
1. **Foundation** — define the knowledge store (vault-style folders or DB),
   formalize `CLAUDE.md` operating doc, set permissions/guardrails (slide 14).
2. **Scheduler + skill runner** — a loop that runs skills on cron, with retries,
   context injection from memory, and structured outputs (Hermes-equivalent).
3. **The seven skills** — implement Morning brief → Capture processor →
   Connection finder → Weekly synthesis → Belief tracker → Pattern detector →
   Decision intelligence, each: *reads context → writes output → stores memory*.
4. **Compounding memory** — persistent memory writes per run; skill-refinement
   feedback; monthly pattern recognition.
5. **Notifications** — delivery (Telegram / email / in-app) of briefs & flags.
6. **Surface in the app** — show outputs/briefs/connections in the existing UI.

---

## Part 4 — Open decisions (need Edgar's call)
1. **Vercel target:** A1 marketing site, A2 UI demo, or A3 full app (non-Vercel host)?
2. **Expansion home:** build the loop *inside* the existing Jarvis app, or as a
   companion Obsidian vault that talks to it?
3. **Knowledge store:** real Obsidian vault (Markdown files) vs the app's existing
   DB/semantic layer — or a bridge between both?
4. **Notifications channel:** Telegram (as in the deck), email, or in-app only?

---

## Part 5 — Slide intake log
- Received & documented: **1–25 (complete).** Deck is 25 slides, not 30.

---

## Part 6 — Recommendations (my call on the open decisions)
1. **Vercel target → A1 now, A3 later.** Ship a marketing/landing site from the
   deck on Vercel this week (fast win, real URL). Keep the actual Jarvis app on a
   proper backend host (Render/Fly/Railway or a small VM) where AppleScript can be
   replaced and the BigQuery key stays secret. Vercel becomes the public front
   door that links to the app.
2. **Expansion home → inside the existing app, reusing the vault idea as the
   knowledge store.** Build the operating loop into Jarvis (it already has memory,
   briefs, scheduler, connectors) and let it read/write a Markdown "vault" folder
   so Obsidian can sit on top if you want it. Best of both.
3. **Skills → implement in deck order, gated by schedule.** Morning brief first
   (highest daily value), then Capture processor + Connection finder, then the
   weekly/monthly skills. Each skill = read context → write output → store memory.
4. **Notifications → in-app first, email second, Telegram optional.** Lowest
   friction; Telegram can come later as the deck suggests.

## Part 7 — Proposed build sequence (once decisions are confirmed)
- **Phase 0:** Stand up the Vercel marketing site (Track A1).
- **Phase 1:** Knowledge store + `CLAUDE.md` operating doc + permissions/guardrails.
- **Phase 2:** Scheduler + skill-runner harness (retries, memory injection, output
  routing) — reuse existing scheduling + `brief_engine.py`.
- **Phase 3:** Skill 1 Morning brief → Skill 2 Capture processor → Skill 3
  Connection finder.
- **Phase 4:** Skills 4–7 (Weekly synthesis, Belief tracker, Pattern detector,
  Decision intelligence).
- **Phase 5:** Compounding memory + CLAUDE.md auto-calibration + notifications.
- **Phase 6:** Surface outputs (briefs, connections, patterns) in the app UI.
