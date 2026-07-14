"""
martin_voice — voice integration for Smart with Martin.

After Jarvis submits a question to Martin, this module:
  1. Waits for Martin's response to complete (polls DOM for completion signals)
  2. Asks the user how they want the response delivered (headlines / summary / screen)
  3. Executes the chosen preference

The preference state machine lives on the WebSocket session so Jarvis can
route the next utterance to the right handler.

PUBLIC API
  - MartinSession class: per-WebSocket state holder
  - is_martin_trigger(text): does this voice transcript start a Martin command?
  - parse_martin_command(text): returns (verb, args) or None
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

log = logging.getLogger("jarvis.martin_voice")


# ── Trigger / command parsing ───────────────────────────────────

# Phrases that signal "this command targets Martin". User must always
# use "Martin" as a namespace prefix.
_MARTIN_PREFIX = re.compile(
    r"^\s*(?:hey\s+)?martin[,\s]+|^\s*"
    r"(?:open|close|exit|start|log\s+(?:in|into)|go\s+to|ask)\s+martin\b",
    re.IGNORECASE,
)

# Inverse: phrases like "<X> in Martin" — pattern detected separately
_IN_MARTIN_SUFFIX = re.compile(r"\bin\s+martin\b\s*$", re.IGNORECASE)


def is_martin_trigger(text: str) -> bool:
    """True if the utterance is targeted at the Martin module."""
    if not text:
        return False
    if _MARTIN_PREFIX.search(text):
        return True
    if _IN_MARTIN_SUFFIX.search(text):
        return True
    return False


# Six top-level sidebar destinations + the Automations sub-items
_DESTINATIONS = {
    "home": "home",
    "views": "views",
    "automations": "automations",
    "automation": "automations",
    "metrics": "metrics",
    "metric": "metrics",
    "knowledge": "knowledge",
    "evaluations": "evaluations",
    "evaluation": "evaluations",
}

_AUTOMATIONS_SUB = {
    "reports": "automations_reports",
    "report": "automations_reports",
    "actions": "automations_actions",
    "action": "automations_actions",
}


@dataclass
class MartinCommand:
    """Parsed Martin command."""
    verb: str            # "open" | "goto" | "ask" | "close"
    arg: str = ""        # destination key, or question text, etc.


def parse_martin_command(text: str) -> Optional[MartinCommand]:
    """
    Parse a Martin-targeted utterance into a structured command.

    Examples:
      "Martin open"                          → open
      "Open Martin"                          → open
      "Log into Martin"                      → open
      "Martin go to Home"                    → goto("home")
      "Go to Views in Martin"                → goto("views")
      "Martin go to Automations Reports"     → goto("automations_reports")
      "Martin ask how many leads last week"  → ask("how many leads last week")
      "Ask Martin which campaigns won"       → ask("which campaigns won")
      "Martin close"                         → close
    """
    if not text:
        return None
    t = text.strip().rstrip(".?!")
    tl = t.lower()

    # Strip "hey martin," / "hey martin" / "martin," prefixes so the rest can be matched cleanly
    tl_stripped = re.sub(r"^\s*hey\s+martin[,\s]+", "martin ", tl).strip()

    # ── OPEN_VIEW ───────────────────────────────────────────
    # "Martin open <view name>" / "Open <view name> in Martin"
    # NOTE: this is distinct from "Martin open" (no arg = launch browser).
    # We check this BEFORE the plain "open" branch so that having an argument
    # is treated as a view-open, not a browser-open.
    m = re.match(r"^martin[,\s]+open\s+(.+)$", tl_stripped)
    if m:
        view_name = m.group(1).strip().rstrip("?.!")
        if view_name:
            return MartinCommand(verb="open_view", arg=view_name)
    m = re.match(r"^open\s+(.+?)\s+in\s+martin\b", tl_stripped)
    if m:
        view_name = m.group(1).strip()
        if view_name:
            return MartinCommand(verb="open_view", arg=view_name)

    # ── OPEN (browser) ──────────────────────────────────────
    open_patterns = [
        r"^martin[,\s]+open\b$",   # exactly "Martin open" with nothing after
        r"^open\s+martin\b",
        r"^log\s+(?:in|into)\s+martin\b",
        r"^start\s+martin\b",
        r"^launch\s+martin\b",
        r"^let'?s?\s+(?:go\s+to|open|use)\s+martin\b",
    ]
    for pat in open_patterns:
        if re.search(pat, tl_stripped):
            return MartinCommand(verb="open")

    # ── CLOSE ───────────────────────────────────────────────
    # Patterns cover natural phrasings + common STT mistranscriptions
    # (clothes/closed/closes are what STT often produces for "close")
    close_patterns = [
        r"^martin[,\s]+(?:close|exit|quit|stop|closed|closes|clothes|cloth|shut\s+down|shut)\b",
        r"^(?:close|exit|quit|stop|closed|closes|clothes|cloth)\s+martin\b",
        r"^shut\s+martin\s+down\b",
        r"^we'?re\s+done\s+(?:with\s+)?martin\b",
        r"^martin[,\s]+we'?re\s+done\b",
    ]
    for pat in close_patterns:
        if re.search(pat, tl_stripped):
            return MartinCommand(verb="close")

    # ── ASK ────────────────────────────────────────────────
    # "Martin ask <question>" or "Ask Martin <question>"
    m = re.match(r"^martin[,\s]+ask\s+(.+)$", tl_stripped)
    if m:
        return MartinCommand(verb="ask", arg=m.group(1).strip())
    m = re.match(r"^ask\s+martin[,\s]+(.+)$", tl_stripped)
    if m:
        return MartinCommand(verb="ask", arg=m.group(1).strip())

    # ── GOTO ───────────────────────────────────────────────
    # Many phrasings:
    #   "Martin go to home" / "Martin go home" / "Martin home"
    #   "Martin take me to views" / "Go to views in Martin" / "Views in Martin"

    # Strip trailing "in Martin"
    candidate = re.sub(r"\bin\s+martin\b\s*$", "", tl_stripped).strip()
    # Strip leading "martin <verb>" — verb is optional
    m = re.match(r"^martin[,\s]+(?:go(?:\s+to)?|take\s+me\s+to|navigate\s+to|show\s+me|open|switch\s+to)\s+(.+)$", candidate)
    if m:
        candidate = m.group(1).strip()
    else:
        m = re.match(r"^(?:go(?:\s+to)?|take\s+me\s+to|navigate\s+to|show\s+me|switch\s+to)\s+(.+)$", candidate)
        if m:
            candidate = m.group(1).strip()
        else:
            # Bare "martin <destination>" — strip the martin prefix
            m = re.match(r"^martin[,\s]+(.+)$", candidate)
            if m:
                candidate = m.group(1).strip()

    # Now `candidate` should be just the destination name(s)
    candidate = candidate.strip().rstrip("?.!")

    # Automations sub-item: "automations reports", "automations actions"
    m = re.match(r"^automations?\s+(reports?|actions?)\b", candidate)
    if m:
        return MartinCommand(verb="goto", arg=_AUTOMATIONS_SUB[m.group(1).lower()])

    # Top-level destination
    for word, key in _DESTINATIONS.items():
        # Match as whole word at start
        if re.match(rf"^{word}\b", candidate):
            return MartinCommand(verb="goto", arg=key)

    # Couldn't parse — caller may treat as unknown
    return None


# ── Preference parsing ─────────────────────────────────────────

class ResponsePref(Enum):
    HEADLINES = "headlines"
    SUMMARY = "summary"
    SCREEN = "screen"


_PREF_PATTERNS = {
    ResponsePref.HEADLINES: [
        r"^\s*(?:headlines?|the headlines?|titles?|heads up)\s*$",
        r"\bhead(?:line)?s?\b",
        r"^\s*b\.?\s*$",  # letter shortcut
    ],
    ResponsePref.SUMMARY: [
        r"^\s*(?:summary|summarize|short|brief|short version|quick version)\s*$",
        r"\bsummar(?:y|ize|ies)\b",
        r"\bbrief(?:ly)?\b",
        r"\bshort(?:\s+version)?\b",
        r"^\s*a\.?\s*$",  # letter shortcut
    ],
    ResponsePref.SCREEN: [
        r"^\s*(?:screen|on\s+screen|i'?ll\s+read|i'?ll\s+look|show\s+me|read\s+it\s+myself)\s*$",
        r"\bscreen\b",
        r"\bmyself\b",
        r"^\s*c\.?\s*$",  # letter shortcut
    ],
}


def parse_response_pref(text: str) -> Optional[ResponsePref]:
    """Parse user's choice of how to receive Martin's response."""
    if not text:
        return None
    t = text.lower().strip()
    for pref, patterns in _PREF_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, t):
                return pref
    return None


# ── Response detection in the page ─────────────────────────────

# A Martin response is "complete" when one of these conditions holds:
#  1. The "8 tools, N thinking steps" badge appears at the top
#  2. The text in the response container hasn't changed for ~2.5 seconds
#  3. A heading like "Key Insights and Patterns" exists in the response

# Default timing: poll every 1s, declare done after 2.5s of stability,
# absolute max wait of 180s.

POLL_INTERVAL_S = 1.0
STABILITY_REQUIRED_S = 2.5
MAX_WAIT_S = 180.0


async def wait_for_response_complete(page, state_callback=None) -> bool:
    """
    Wait for Martin's response to fully render.

    Detection signal: Martin renders a "Start new conversation from here"
    button at the end of every completed response. The button has
    aria-label="Start new conversation from here", which is:
      - Universal (appears for all response types, not just metric queries)
      - Stable (aria-labels rarely change across UI refactors)
      - Unambiguous (no false matches anywhere else on the page)

    This replaces the previous timer/stability-based heuristic which was
    fragile against pre-streaming gaps and quiet moments between paragraphs.

    Returns True if the button appeared (response complete), False on timeout.
    """
    log.info("Waiting for Martin response to complete...")
    if state_callback:
        try:
            state_callback("waiting_on_martin")
        except Exception:
            pass
    start = asyncio.get_event_loop().time()
    last_log_t = 0.0

    # Snapshot how many branch buttons exist BEFORE this wait.
    # The detector below requires the count to grow before declaring "done" —
    # this prevents false positives when the previous response's branch
    # button is still on the page (e.g., when new_chat didn't fully reset).
    try:
        initial_count = await page.evaluate('''() => {
            return document.querySelectorAll(
                'button[aria-label="Start new conversation from here"]'
            ).length;
        }''')
    except Exception:
        initial_count = 0
    log.debug(f"Initial branch button count: {initial_count}")

    # Wait at least MIN_WAIT_S before the detector can fire, so a fast-but-
    # legitimate response also doesn't trip on initial render of stale UI.
    MIN_WAIT_S = 2.0

    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > MAX_WAIT_S:
            log.warning(f"Response wait timeout after {elapsed:.1f}s")
            return False

        try:
            current_count = await page.evaluate('''() => {
                const buttons = document.querySelectorAll(
                    'button[aria-label="Start new conversation from here"]'
                );
                let visible = 0;
                for (const b of buttons) {
                    if (b.offsetParent !== null) visible++;
                }
                return visible;
            }''')
        except Exception as e:
            log.debug(f"Polling error: {e}")
            current_count = initial_count

        # Done when:
        #   - we've waited at least MIN_WAIT_S
        #   - AND we see more branch buttons than at the start (new response complete)
        if elapsed >= MIN_WAIT_S and current_count > initial_count:
            log.info(f"Response complete after {elapsed:.1f}s "
                     f"(branch button count: {initial_count} -> {current_count})")
            return True

        # Light progress logging — every ~10s so we can see polling is alive
        if elapsed - last_log_t > 10:
            log.info(f"Still waiting for Martin response ({elapsed:.0f}s elapsed)")
            last_log_t = elapsed

        await asyncio.sleep(POLL_INTERVAL_S)


# ── Content extraction ────────────────────────────────────────

async def extract_headlines(page) -> dict:
    """
    Extract the section headings + Key Insights bullet list.

    Uses [data-streamdown] anchors (added by Vercel's Streamdown library)
    rather than raw <h1>/<h2> tags, so we survive any class-name changes
    Kaelio makes to their Tailwind classes.

    Returns dict:
      {
        "title": "Lead Source Performance: ...",
        "sections": ["Top Lead Sources...", "Revenue Generated...", ...],
        "insights": ["Outbound SDR generates the most leads but...", ...],
      }
    """
    try:
        result = await page.evaluate(r"""() => {
            const out = {title: null, sections: [], insights: []};

            // Title — first heading-1 streamed
            const h1 = document.querySelector('[data-streamdown="heading-1"]');
            if (h1) out.title = (h1.innerText || '').trim();

            // Section headings — heading-2 + heading-3 from Streamdown
            const headings = document.querySelectorAll(
                '[data-streamdown="heading-2"], [data-streamdown="heading-3"]'
            );
            for (const h of headings) {
                const t = (h.innerText || '').trim();
                if (t && t.length < 200) out.sections.push(t);
            }

            // Key Insights bullets — find the heading containing "Insight",
            // "Pattern", "Implication", or "Strategic", then collect the
            // list items that follow until the next same-or-higher heading.
            for (const h of headings) {
                const txt = (h.innerText || '').toLowerCase();
                const triggers = ['insight', 'pattern', 'implication', 'strategic', 'takeaway', 'recommendation'];
                if (!triggers.some(t => txt.includes(t))) continue;

                // Walk forward through siblings
                let node = h.nextElementSibling;
                while (node) {
                    const ds = node.getAttribute('data-streamdown');
                    if (ds === 'heading-2' || ds === 'heading-1') break;
                    // Streamdown wraps list items with data-streamdown="list-item"
                    if (ds === 'list-item' || node.tagName === 'LI') {
                        const t = (node.innerText || '').trim();
                        if (t) out.insights.push(t);
                    } else {
                        // Sometimes lists are wrapped in ul/ol
                        const lis = node.querySelectorAll('[data-streamdown="list-item"], li');
                        for (const li of lis) {
                            const t = (li.innerText || '').trim();
                            if (t) out.insights.push(t);
                        }
                        // Also support numbered <p> like "1. Double down on Events:..."
                        if (node.tagName === 'P' && /^\d+\.\s/.test((node.innerText || '').trim())) {
                            out.insights.push((node.innerText || '').trim());
                        }
                    }
                    node = node.nextElementSibling;
                }
                if (out.insights.length) break;
            }
            return out;
        }""")
    except Exception as e:
        log.exception("extract_headlines failed")
        return {"title": None, "sections": [], "insights": []}

    log.info(f"Extracted headlines: title={result.get('title')!r}, "
             f"{len(result.get('sections') or [])} sections, "
             f"{len(result.get('insights') or [])} insights")
    return result


async def extract_full_text(page) -> str:
    """Extract the entire narrative body for sending to Claude for summary.

    Concatenates all [data-streamdown]-tagged elements which gives us
    exactly the rendered markdown without the chat history sidebar or
    other surrounding UI.
    """
    try:
        text = await page.evaluate(r"""() => {
            // Pull every Streamdown-rendered element's innerText in DOM order.
            // Each one represents a markdown node (heading, paragraph, list-item, etc.)
            const els = document.querySelectorAll('[data-streamdown]');
            const parts = [];
            for (const el of els) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                // Avoid duplication: streamdown nests sometimes (a list-item inside a list).
                // Only include "leaf" elements — ones with no child streamdown nodes.
                if (el.querySelector('[data-streamdown]')) continue;
                parts.push(t);
            }
            return parts.join('\n');
        }""")
    except Exception:
        text = ""
    return text or ""


def format_headlines_for_voice(headlines: dict) -> str:
    """Turn the extracted headlines into a spoken paragraph."""
    parts = []
    title = headlines.get("title")
    sections = headlines.get("sections") or []
    insights = headlines.get("insights") or []

    if title:
        parts.append(f"Martin's report: {title}.")

    if sections:
        # Filter out the title from sections list (it sometimes duplicates)
        clean_sections = [s for s in sections if s != title][:5]
        if clean_sections:
            joined = "; ".join(clean_sections)
            parts.append(f"It covers: {joined}.")

    if insights:
        parts.append("Key insights:")
        # Cap at 4 insights — even 4 is a lot of audio
        for i, ins in enumerate(insights[:4], 1):
            # Strip lead bullets / numbers
            cleaned = re.sub(r"^\d+\.\s*", "", ins).strip()
            # Truncate any one insight to 200 chars to keep the readback bounded
            if len(cleaned) > 200:
                cleaned = cleaned[:200] + "..."
            parts.append(cleaned)

    if not parts:
        return "Martin's response is ready, but I couldn't extract a clean summary. Take a look on screen."

    return " ".join(parts)


# ── Per-WebSocket state machine ────────────────────────────────

class MartinPhase(Enum):
    """Per-WebSocket Martin phase.

    Flow (non-modal):
        IDLE → WAITING_FOR_RESPONSE → READY → IDLE
                                       ↑       ↓
                                       └── user can run preference commands while READY,
                                           then return to IDLE after each one
    """
    IDLE = "idle"
    WAITING_FOR_RESPONSE = "waiting"
    READY = "ready"      # response complete; user can ask "summarize" / "headlines" / "read"


@dataclass
class MartinSession:
    """
    Per-WebSocket state for Martin voice interactions.

    READY phase persists until the user explicitly closes Martin, asks a new
    question (which resets to WAITING), or until a long timeout. The user
    can issue follow-up commands like "Martin summarize" while READY.
    """
    phase: MartinPhase = MartinPhase.IDLE
    last_question: str = ""
    headlines: dict = field(default_factory=dict)
    full_text: str = ""

    def reset(self) -> None:
        self.phase = MartinPhase.IDLE
        self.last_question = ""
        self.headlines = {}
        self.full_text = ""

    @property
    def has_response(self) -> bool:
        """True if a response is ready to be referenced.
        We don't gate on full_text being non-empty — let followups try and
        return their own 'couldn't extract' message if the page is genuinely
        empty. This way the user always gets a clear answer, not 'unparseable'.
        """
        return self.phase == MartinPhase.READY


# ── Additional command verbs for the READY phase ──────────────

# After Martin's response is ready, these are the follow-up commands users
# can issue. They're independent voice triggers — the user must explicitly
# say "Martin <verb>" to use them.

_FOLLOWUP_PATTERNS = {
    "summarize": [
        r"\bsummar(?:y|ize|ies)\b",
        r"\bshort(?:\s+version)?\b",
        r"\bbrief(?:ly)?\b",
        r"\bgist\b",
        r"\boverview\b",
        r"\bdigest\b",
    ],
    "headlines": [
        r"\bhead(?:line)?s?\b",
        r"\btitles?\b",
        r"\btop\s+points?\b",
        r"\bsections?\b",
        r"\bkey\s+points?\b",
    ],
}


def parse_followup_command(text: str) -> Optional[str]:
    """
    Parse a 'Martin <verb>' utterance for response-handling verbs.
    Returns one of: "summarize" | "headlines" | None.

    Only called when the dispatcher already knows this is a Martin command
    AND the session is in READY phase.
    """
    if not text:
        return None
    t = text.lower().strip()
    # Strip the "martin" prefix so we can match the verb
    t = re.sub(r"^\s*(?:hey\s+)?martin[,\s]+", "", t).strip()
    for verb, patterns in _FOLLOWUP_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, t):
                return verb
    return None


# ── Summarization via Claude ───────────────────────────────────

async def summarize_via_claude(text: str, claude_client, model: str = "claude-sonnet-4-5") -> str:
    """
    Send the full response text to Claude for a digestible verbal summary.

    Target ~80-120 words across 2 short paragraphs — enough to actually
    capture the findings without becoming a slog to listen to.

    claude_client is the existing Anthropic AsyncAnthropic instance from
    server.py — its messages.create is already a coroutine, just await it.
    """
    if not text or len(text) < 50:
        return "Martin's response is too short to summarize, sir. Please read it on screen."

    # Bound the input — Martin reports can be long but we don't need the full doc
    if len(text) > 12000:
        text = text[:12000] + "\n\n[truncated]"

    prompt = f"""You're summarizing a research report for an executive to hear via voice assistant.

Constraints:
- 80-120 words total, split into 2 short paragraphs
- Plain prose only — no markdown, no bullets, no headers
- Active voice; concrete numbers when they appear in the source
- First paragraph: the main finding(s)
- Second paragraph: the most important recommendation or implication
- Address the listener as "sir" once at the beginning, then drop honorifics

Write the summary now, no preamble:

REPORT:
{text}
"""

    try:
        # claude_client is AsyncAnthropic — messages.create is a coroutine,
        # so we await it directly (NOT asyncio.to_thread which is for sync code)
        response = await claude_client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = ""
        for block in response.content:
            if hasattr(block, "text"):
                summary += block.text
        return summary.strip() or "I couldn't generate a summary, sir. Please read on screen."
    except Exception as e:
        log.exception("Claude summary failed")
        return f"I couldn't generate a summary, sir ({type(e).__name__}). Please read on screen."
