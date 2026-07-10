---
aliases: ["The Agency — Setup Log", "Agency Agents Setup"]
tags: [agent, log, setup]
created: 2026-06-29
---

# The Agency — Setup Log

A record of installing and documenting **The Agency** agent collection ([msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)).

## What we did

1. **Cloned & installed** the repo's agents into `~/.claude/agents/` using its official installer (`install.sh --tool claude-code`). The installer copied **232 agents** (markdown files with frontmatter), skipping ~22 non-agent/template files.

2. **Normalized the names.** Every agent shipped with a display-style `name:` field (e.g. `AI Engineer`), which Claude Code/Cowork won't load as a selectable agent — it requires lowercase-hyphen identifiers. We rewrote each file's frontmatter `name:` to a clean slug (e.g. `ai-engineer`, `backend-architect`). All 232 slugs are unique with zero collisions.

3. **Restarted** the Claude desktop app so the new agents were picked up.

4. **Built reference docs** (in the Cowork outputs folder):
	- `Agency-Agent-Capability-Guide.docx` — full Word guide, all 232 agents by division.
	- `Marketing-Agents-Deck.pptx` — professional 9-slide deck of the 36 marketing agents.

5. **Documented to this vault** — one note per agent, organized by division, plus [[The Agency — Agent Index]].

## How to use any agent

In any Claude session, name the agent by its slug:

> *“Use the `code-reviewer` agent to review this pull request.”*
> *“Act as `frontend-developer` and build a React component.”*

## Scope note — China-market agents excluded

This vault intentionally **omits 21 China-market-specific agents** that don't apply to a US context. For reference, the excluded slugs were:

`wechat-mini-program-developer`, `feishu-integration-developer`, `bilibili-content-strategist`, `wechat-official-account-manager`, `multi-platform-publisher`, `zhihu-strategist`, `china-market-localization-strategist`, `weibo-strategist`, `livestream-commerce-coach`, `kuaishou-strategist`, `baidu-seo-specialist`, `private-domain-operator`, `cross-border-e-commerce-specialist`, `xiaohongshu-specialist`, `podcast-strategist` (Chinese podcast market), `douyin-strategist`, `china-e-commerce-operator`, `recruitment-specialist` (China hiring platforms), `healthcare-marketing-compliance-specialist` (China law), `government-digital-presales-consultant` (China gov), `study-abroad-advisor` (Chinese students).

Two borderline flags were **kept** as false positives: `strategy-duel-agent` (general game theory) and `supply-chain-strategist` (general sourcing). They remain installed in `~/.claude/agents/` — they're just not documented here.

---

Start here → [[The Agency — Agent Index]]
