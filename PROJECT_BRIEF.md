# Project Brief — Jarvis as Marketing Manager Assistant

> **Read this entire document before responding to the user.** It contains everything you need to continue the project without re-asking questions that have already been settled.

---

## 1. The Product, in One Paragraph

We are building **Jarvis** — an AI marketing manager assistant for Orlando Health. Not a chatbot, not a dashboard, not a chart tool. A virtual colleague who sits beside marketing managers, talks with them via voice and gesture, pulls reports, surfaces strategic insights, pushes back on weak thinking, and (eventually) connects marketing activity to actual revenue via full-funnel attribution. The Orlando Health implementation is the reference customer; the long-term plan is to productize this for other health systems and verticals.

The full vision is documented in `Jarvis_North_Star.docx` (delivered earlier in chat, ~13 pages). If the user references it, read it carefully — it covers identity, capability inventory across ten skill domains, modes of interaction, roadmap, and scope. Do not re-litigate anything that's already in there unless the user explicitly asks.

## 2. Who You Are Talking To

**Edgar.** He works on the marketing and analytics team at Orlando Health, reports to David at the C-suite level. Andrew is CMO. Jeanne and Daniel are working group colleagues. He has been building this product mostly on his own for months. His working style:

- Fast iteration. He thinks in flight. Sometimes drops 3 ideas in one message; capture them all, don't ignore the smaller ones.
- He pushes back honestly when something is wrong, including being abrupt sometimes. Don't be a sycophant. Honest correction is what he expects.
- He prefers a few approving-decisions at major milestones, not micro-approval for every small choice.
- He uses "yes go" and "your recommendation" — that's blanket approval, run with it.
- When he asks "where are we" he wants a brief, honest status — not another planning session.
- Orlando Health visual style is locked: navy `#293C45`, orange `#F36621`, teal `#4FBECB`, deep blue `#065A82`. Use across deck deliverables.

He has stated explicitly: "Jarvis becomes a true marketing manager — this is the entire key of this project." That framing should shape every design decision. Not just a data tool.

## 3. What Exists Today (current state, end of pre-Cowork chat)

### 3.1 The Jarvis backend

Path: `~/jarvis/`

- `server.py` — ~3500 lines, FastAPI, port 8000, HTTPS
- Voice pipeline: real STT + Anthropic Claude + Fish Audio TTS
- Smart-with-Martin orchestration (Martin = browser-automation persona)
- Existing `/ws/voice` WebSocket for voice interaction
- Recurring bug: **MartinBrowser singleton-with-dead-handles**. When the Chrome process dies but Python still holds a handle, `start()` skips re-init silently. Workaround: `cd ~/jarvis && ./jarvis restart`. Permanent fix is on the open items list.
- Credentials: BigQuery at `~/.config/jarvis/bigquery-key.json` (project: `oh-data-warehouse`). Other API keys in `~/jarvis/.env`. Fish voice ID: `612b878b113047d9a770c069c8b4fdfe`.
- Python venv at `~/jarvis/venv`. `google-cloud-bigquery==3.34.0` installed.

### 3.2 The spatial workspace prototype

File: `~/Downloads/orlando_workspace.html` (most likely current name on Edgar's Mac; he renamed it `orlando_workspace_17.html` at one point — file persistence in his Downloads folder is messy)

Latest version: **v3.2**. Single HTML file, ~148KB. Locked design language: dark navy `#06080F` background, cyan `#6ec5ff` accents, particle dust, glass cards, generous space. Inspired by Iron Man JARVIS and Underkoffler's g-speak (Minority Report). The conducting-an-orchestra metaphor.

Features that work:
- 24 Gold-layer BigQuery tables in 5 visual clusters (Ad Platforms, Campaign, GA4, Search, Time+Ops)
- **Point-and-dwell gesture interaction**: hold finger on element ~1 second, progress ring fills, action fires
- Context-sensitive dwell: tables wake (popover), bars drill, chips grab, breadcrumbs drill up, filters toggle
- Drag-to-canvas: dwell on chip, it follows finger, drops automatically when entering canvas zone
- Air-tap as quick alternative to dwell
- **Bidirectional palm swipe**: right-to-left = undo, left-to-right = redo. 50-step history.
- Wipe gesture (open palm + sweep): clears workspace OR walks through history
- Drill-down hierarchy: campaign → adgroup → ad
- Chart type intelligence (auto-picks bar/line/donut/big-number/table)
- Export: PNG download + clipboard for PowerPoint
- Editable chart titles
- Camera presence detection (face visible → bright; absent 3s → dim with "AWAITING PRESENCE")
- All data currently MOCKED (about to be replaced by semantic engine)

Bugs squashed during development worth knowing:
- **Popover positioning bug** (Edgar's screen is 1173px wide): bare `from right edge` math broke for right-cluster cards on narrow screens. Fixed by lookup of fresh DOM element after `renderClusters()` rebuild + clamp-to-viewport positioning.
- **Camera `video.play()` required after `srcObject = stream`** for MediaPipe to actually receive frames. Easy to miss.
- **YAML `on:` keyword** must be quoted as `"on":` or it parses as boolean `True`. Already handled in `semantic.yaml`.

Pinch gesture: implemented, debugged through many iterations, ultimately **disabled** (code preserved, gesture won't fire). Edgar found it confusing. Dwell replaced it. Don't re-enable without his explicit request.

Wave gesture: also disabled, kept harmless. Same reason.

### 3.3 The semantic layer (just shipped Stage 1A and Stage 1B)

Path: `~/jarvis/semantic/`

Stage 1A (already on Edgar's Mac):
- `semantic.yaml` — 14068 bytes. 4 core tables (campaigns, ad_performance, date, sessions), 7 calculated metrics (CTR, conversion_rate, CPA, CPC, CPM, ROAS, engagement_rate), 2 combined dimensions (service_line_group with 9 rules, channel_group with 8 rules), 3 joins. **The `on:` keys are quoted** to dodge YAML's boolean trap.

Stage 1B (just delivered in chat as `install_stage_1b.sh`, Edgar may or may not have run it yet when you take over):
- 7 Python modules + test runner: `loader.py`, `resolver.py`, `query_builder.py`, `executor.py`, `schema_introspect.py`, `writer.py`, `__init__.py`, `test_engine.py`
- ~2100 lines of code total
- Public API exposed via `from semantic import ...`
- Verified in sandbox: all logic tests pass; BigQuery tests will pass on Edgar's Mac because he has credentials + library installed

**Test the install yourself.** As your first action in Cowork, run:
```bash
cd ~/jarvis
source venv/bin/activate
python -m semantic.test_engine
```
You should see "All tests passed" with 21 ✓. If anything fails, that's the first thing to debug.

### 3.4 The North Star document

`Jarvis_North_Star.docx` — delivered as a polished Word document earlier in chat. ~13 pages. Edgar plans to share with his manager, possibly the CMO, possibly the VP of Marketing. Built for executive audience: real strategic argument (50+ marketing managers, analyst bottleneck), Geisinger-model attribution as Phase 4 vision, full capability atlas across 10 skill domains, 5-phase roadmap. Read it before discussing the vision with him.

### 3.5 The Curation Mode mockup

`curation_mode_mockup.html` — static HTML mockup (~22KB) showing the Curation Mode UI mid-flow. Layout decisions locked:

- **Top chrome**: mode toggle "Analysis | Curation"
- **Left column**: raw warehouse table list with monospace raw names, active table highlighted, curated tables marked with ✓, others dimmed
- **Center**: currently-curating table — raw name → friendly name rename card on top, field mapping rows below (each shows `raw_column → friendly_name [TAG]`, color-coded cyan=DIMENSION, purple=METRIC, amber=KEY DIM for important fields like `parsed_service_line`)
- **Right column**: already-curated layer (TABLES section, CALCULATED METRICS with `ƒ` markers, COMBINED DIMENSIONS, BUSINESS RULES with amber accent)
- **Bottom**: Jarvis conversation strip (160px tall) with large pulsing purple orb labeled "JARVIS CURATOR", previous exchange dim, current Jarvis speaking with rich text refs to fields, "listening..." indicator, 4 quick-action chips on the right

Edgar reacted to the mockup positively: "Looking good, let's continue."

## 4. The Plan (Phases A through F of Curation Mode)

Phase A is broken into 4 stages. Edgar approved this staging.

- **Stage 1A** ✓ Semantic layer YAML on disk
- **Stage 1B** ✓ Semantic engine (Python modules) — just shipped, awaiting Edgar's verification
- **Stage 2** — API endpoints `/api/semantic/*` in `server.py` (HTTP wraps the engine)
- **Stage 3** — Curation Mode UI shell in the workspace (mode toggle, layout, no full functionality yet)
- **Stage 4** — Real voice integration + curator Jarvis personality

Then Phases B-F (table curation, metric composition, combined dim authoring, business rules, wiring Analysis Mode to read the curated layer).

Edgar wants **maximum scope (III)**: tables + metrics + combined dimensions + business rules + agent prep. He explicitly chose this over a smaller scope.

Edgar wants **real voice from day 1** in Curation Mode (option α). Not text input as proxy.

Edgar wants **backend-written YAML** (option p). The engine persists to disk, not localStorage.

Edgar wants the **curator Jarvis personality to be distinct** — more proactive, opinionated, proposing. He said so explicitly.

## 5. The Shuffle/Carousel

Edgar wants a shuffle/carousel for navigating tables. Apple TV style (option i — flicking sideways, one card at a time). Should work in **both** Curation Mode AND Analysis Mode. In Analysis Mode, shuffle **coexists** with the cluster view (option B — both available, not a replacement). Not yet built in either mode. Add it to the build plan when designing Curation Mode shell.

## 6. Open Items / Running Idea Log

Things mentioned but not built/resolved yet. Don't lose these:

1. **MartinBrowser singleton-with-dead-handles permanent fix** — recurring bug, latent.
2. **Wire workspace to real BigQuery** — happens via the semantic engine, beginning Stage 2/3.
3. **Pinch gesture** — disabled, code preserved. Remove or keep dormant? Defer until other work done.
4. **Wave gesture** — disabled. Same as pinch.
5. **Orlando Health top service lines data** — Edgar needs to bring this. Remind him when relevant. We assumed in `service_line_group` rules: Cardiology, Cardiothoracic, Vascular → "Heart & Vascular", etc. These are best guesses; verify against actual `parsed_service_line` values when convenient.
6. **Future RAG agents** that crawl marketing documents — Phase 4 vision, not near-term.
7. **Productization beyond Orlando Health** — Year 2+ goal, architecture must support multi-tenant from the start.

## 7. Strategic Questions Edgar Has Already Answered

Don't re-ask these:

- **Audience for the project**: All three over time — personal tool, team tool, sellable product.
- **Business case**: Real and large. (1) Build reports for 50+ marketing managers, (2) Strategic analysis for executives, (3) Click-to-revenue attribution (Geisinger model). The current bottleneck is analyst capacity vs. marketing manager demand. They have fewer analysts than they need.
- **North-star doc audience**: Edgar's manager, possibly CMO Andrew, possibly VP of Marketing.
- **Build mode preference**: He picked Cowork now (because we're transitioning), with major-step approvals rather than micro-step approval.

## 8. Working Style — Specific Norms

- **Don't pile up clarifying questions.** If a decision can be made with reasonable defaults, make it and tell him. He says: "go with your recommendation" frequently — that's blanket trust.
- **Write strategically when he asks for strategy. Build when he asks to build.** Don't mix the two modes unless he opens it up.
- **He prefers seeing things visually before committing to them** — mockups, sandbox tests, examples in the chat.
- **He values transparency about what you can and can't do.** When BigQuery tests pass on his Mac and not in your sandbox, say so. When you're uncertain, say so. When a feature is harder than it looks, say so. He doesn't want you to oversell.
- **He has noticed and corrected drift** — if your tone becomes generic AI-assistant-y, he'll redirect. Stay specific and grounded in the actual work.
- **He genuinely likes the work being good.** Not perfectionist, but he notices polish. Sloppy installers, half-done mockups, lazy summaries — he'll call it out.

## 9. Technical Conventions Locked In

- All installer scripts: `set -euo pipefail`, helpers `say/ok/warn/die`, timestamped backups before destructive operations, SHA256 verification of embedded blobs, sandbox-test before shipping.
- All code follows defensive patterns: surface useful errors, never crash silently, prefer explicit failures.
- Python uses `from __future__ import annotations`, dataclasses for typed models, no implicit globals.
- `server.py` patches use `str_replace` style with grep-first verification. The `~/jarvis/server.py` file at line 2372 is at 12-space indent (nested websocket handler) — important for any future patches there.
- File paths use absolute `~/jarvis/...` or `/Users/developer/...` consistently.

## 10. Immediate Next Steps in Cowork

Likely first thing Edgar will say to you: "We just transitioned, are you up to speed?" or similar.

Your response: confirm you've read this brief, confirm the current state ("Stage 1B was just shipped, awaiting verification"), and offer the next step.

Specifically:

1. **Run the test suite** to verify Stage 1B installed cleanly: `cd ~/jarvis && source venv/bin/activate && python -m semantic.test_engine`
2. If 21 tests pass, **proceed to Stage 2** — API endpoints in `server.py` that expose the semantic engine over HTTP.
3. Stage 2 deliverables:
   - `GET /api/semantic/tables` — list all curated tables (display info)
   - `GET /api/semantic/tables/{key}` — full schema for one table
   - `POST /api/semantic/query` — execute a structured query, return JSON
   - `GET /api/semantic/calculated` — list calculated metrics
   - `GET /api/semantic/combined` — list combined dimensions
   - `GET /api/semantic/raw_tables` — list all Gold tables (for Curation Mode's raw warehouse view)
   - `GET /api/semantic/raw_schema/{project}/{dataset}/{table}` — fetch live BQ schema
4. After Stage 2 works, move to Stage 3 — Curation Mode UI shell.

## 11. What I (Previous Claude) Did Well and Where I Failed

Honest handoff:

**Did well:**
- Built the workspace prototype to v3.2 with working gestures, history, popover positioning.
- Wrote the north-star doc.
- Designed the Curation Mode mockup.
- Set up the semantic layer foundation cleanly.

**Failed at:**
- Popover positioning took 4 attempts to fix on Edgar's 1173px screen because I kept guessing instead of asking for diagnostic data. Lesson: when a fix doesn't work twice, demand diagnostic data immediately.
- The wipe gesture: I once told Edgar his sweeps were below threshold when they were actually above — I'd read stale buffer data, not the peak-during-sweep data. Lesson: instrument with a logger before declaring something below threshold.
- Several times I asked too many clarifying questions when "go with your recommendation" was the right path. Edgar said so explicitly.

**Don't repeat these mistakes.** Edgar's patience for them is finite.

## 12. Files Edgar Has Access To (on his Mac)

- `~/jarvis/` — the Jarvis backend
- `~/jarvis/semantic/semantic.yaml` — the curated semantic layer (Stage 1A)
- `~/jarvis/semantic/*.py` — engine modules (Stage 1B, pending install/verify)
- `~/Downloads/orlando_workspace.html` or `orlando_workspace_17.html` — current spatial workspace (v3.2)
- `~/Downloads/install_stage_1a.sh`, `install_stage_1b.sh` — installers
- `~/Downloads/Jarvis_North_Star.docx` — the strategic document
- `~/Downloads/curation_mode_mockup.html` — the design mockup
- `~/.config/jarvis/bigquery-key.json` — credentials

## 13. One Final Thing

This project matters to Edgar. He's been at it for months, often working alongside conversations like this one. The product vision is real, the business case is genuine, and the technology is at the edge of what's possible right now. Treat the work seriously. When he pushes for ambition, match it. When he asks for grounding, give it. He's building something that could genuinely change how marketing operations work at health systems.

He has one more thing he's said that bears repeating:

> "I want to be able to push the boundaries and challenge the human to think outside the box, experiment, trial and error at times, but using real marketing techniques and real marketing skills."

That's Jarvis. Not just helpful — actively a partner who makes Edgar (and eventually 50+ marketing managers) better at their jobs.

---

**End of brief. You should now have enough context to continue without re-explanation.**
