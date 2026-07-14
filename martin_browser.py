"""
MartinBrowser — reusable controller for app.smartwithmartin.ai.

One persistent Chrome window per Jarvis runtime. Holds the saved session
cookies from martin_setup.py so we never see your password.

Typical lifecycle:
    browser = MartinBrowser()
    await browser.start()                       # opens Chrome with saved session
    await browser.goto("home")                  # click Home in left sidebar
    await browser.new_chat()                    # click + New
    await browser.type_prompt("how many leads last week?")
    await browser.submit_prompt()
    ...
    await browser.close()                       # shuts Chrome down

If the saved session has expired, methods raise SessionExpiredError —
caller (server.py) should tell the user to re-run `python -m martin_setup`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext, Browser, TimeoutError as PlaywrightTimeout

log = logging.getLogger("jarvis.martin")


# ── Constants ──────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "jarvis" / "smartwithmartin"
STORAGE_FILE = CONFIG_DIR / "storage_state.json"
APP_URL = "https://app.smartwithmartin.ai"

# Visible labels for the left sidebar (case-insensitive lookup).
# Aliases let the user say "automation" or "automations" and have it work.
SIDEBAR_DESTINATIONS = {
    "home": ["Home"],
    "views": ["Views"],
    "automations": ["Automations", "Automation"],
    "metrics": ["Metrics"],
    "knowledge": ["Knowledge"],
    "evaluations": ["Evaluations", "Evaluation"],
}

# Default wait after a navigation click before we trust the page rendered
NAV_SETTLE_MS = 350

# Max time to wait for a page transition
NAV_TIMEOUT_MS = 10_000

# The TipTap editor placeholder — identifies our prompt box even if CSS changes
PROMPT_PLACEHOLDER = "What would you like to know about your company?"


# ── Exceptions ─────────────────────────────────────────────

class MartinError(Exception):
    """Base class for Smart with Martin errors."""


class SessionExpiredError(MartinError):
    """Saved cookies are no longer valid — user must run martin_setup again."""


class NavigationError(MartinError):
    """Couldn't find or click the requested destination."""


class PromptError(MartinError):
    """Couldn't find or interact with the prompt editor."""


# ── The controller ────────────────────────────────────────

class MartinBrowser:
    """
    Persistent browser session. One instance per Jarvis runtime.

    Not thread-safe; serialize all calls from a single asyncio loop.
    """

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()                # serialize concurrent commands

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Launch Chrome, load saved session, navigate to the app."""
        if self._browser is not None:
            return  # already running

        if not STORAGE_FILE.exists():
            raise SessionExpiredError(
                f"No saved session at {STORAGE_FILE}. "
                "Run `python -m martin_setup` first."
            )

        log.info("Launching Chrome for Smart with Martin")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=False)
        self._context = await self._browser.new_context(
            ignore_https_errors=True,
            storage_state=str(STORAGE_FILE),
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        await self._page.goto(APP_URL, wait_until="domcontentloaded")

        # Did the saved session actually log us in?
        if not await self._is_logged_in():
            await self.close()
            raise SessionExpiredError(
                "Saved session looks expired (login page visible). "
                "Run `python -m martin_setup` to refresh."
            )

        # Wait for the sidebar to render before we accept navigation commands.
        # Kaelio's React app needs ~500-1500ms after DOM load to mount the sidebar.
        try:
            await self._page.wait_for_selector(
                'a:has-text("Home"), button:has-text("Home")',
                timeout=5000,
            )
        except PlaywrightTimeout:
            log.warning("Sidebar didn't render within 5s — proceeding anyway")
        await asyncio.sleep(0.3)  # tiny additional buffer

        log.info("Smart with Martin browser ready")
        await self._inject_overlay()

    async def close(self) -> None:
        """Shut down Chrome cleanly."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None

    async def _ensure_started(self) -> None:
        """Lazily start if a command is called before start()."""
        if self._page is None:
            await self.start()

    # ── Status checks ──────────────────────────────────────

    async def _is_logged_in(self) -> bool:
        """Heuristic: a visible password field means we're on the login page."""
        if self._page is None:
            return False
        pw_input = await self._page.query_selector('input[type="password"]')
        if pw_input is None:
            return True
        try:
            visible = await pw_input.is_visible()
            return not visible
        except Exception:
            return True

    async def is_ready(self) -> bool:
        """True if Chrome is open, session valid, and on the app."""
        try:
            await self._ensure_started()
            return await self._is_logged_in()
        except SessionExpiredError:
            return False

    # ── Navigation ─────────────────────────────────────────

    async def goto(self, destination: str) -> str:
        """
        Click a left-sidebar item by name.

        destination: a lowercased key like "home", "views", "metrics",
                     "knowledge", "evaluations", "automations".
                     Aliases ("automation", "evaluation") accepted.

        Returns the canonical destination label that was clicked.
        Raises NavigationError if the item can't be found or clicked.
        """
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None

            key = destination.lower().strip()
            # Resolve aliases
            canonical = None
            labels = None
            for canon, aliases in SIDEBAR_DESTINATIONS.items():
                if key == canon or any(key == a.lower() for a in aliases):
                    canonical = canon
                    labels = aliases
                    break
            if canonical is None:
                raise NavigationError(
                    f"Unknown destination {destination!r}. "
                    f"Valid: {sorted(SIDEBAR_DESTINATIONS.keys())}"
                )

            log.info(f"Navigating to {canonical}")

            # Find the sidebar item by visible text. Try each alias in order.
            clicked = False
            last_error: Optional[Exception] = None
            for label in labels:
                try:
                    candidates = [
                        self._page.get_by_role("link", name=label, exact=True),
                        self._page.get_by_role("button", name=label, exact=True),
                        self._page.locator(f'a:has-text("{label}")').first,
                        self._page.locator(f'button:has-text("{label}")').first,
                    ]
                    for cand in candidates:
                        try:
                            if await cand.count() > 0:
                                await cand.first.click(timeout=NAV_TIMEOUT_MS)
                                clicked = True
                                break
                        except Exception as e:
                            last_error = e
                            continue
                    if clicked:
                        break
                except Exception as e:
                    last_error = e
                    continue

            if not clicked:
                # Maybe we're already at this destination — check for a
                # visible "active"/"selected" indicator with this label.
                try:
                    already_here = await self._page.evaluate(f"""(label) => {{
                        // Look for any sidebar item with text == label that
                        // has visual markers of being currently selected.
                        const all = document.querySelectorAll('a, button');
                        for (const el of all) {{
                            const txt = (el.innerText || '').trim();
                            if (txt !== label) continue;
                            const cls = el.className || '';
                            // Common selected-state markers in Tailwind / shadcn
                            if (/bg-accent|bg-muted|bg-primary|aria-current|selected|active/i.test(cls)) {{
                                return true;
                            }}
                            if (el.getAttribute('aria-current')) return true;
                            if (el.getAttribute('data-active') === 'true') return true;
                        }}
                        return false;
                    }}""", labels[0])
                except Exception:
                    already_here = False

                if already_here:
                    log.info(f"Already on {canonical}; treating as success")
                    return canonical
                raise NavigationError(
                    f"Couldn't click {canonical!r} in the sidebar. "
                    f"Last error: {last_error}"
                )

            # Let the SPA settle. We can't rely on URL change because the app
            # often keeps the same URL when navigating between sections.
            try:
                await self._page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeout:
                log.warning("Network didn't go idle within timeout; proceeding anyway")
            await asyncio.sleep(NAV_SETTLE_MS / 1000)

            return canonical

    # ── Prompt interaction ─────────────────────────────────

    async def new_chat(self) -> None:
        """Click the + New button to start a fresh chat.

        Kaelio's button is a <button> containing a Lucide 'plus' SVG icon
        followed by the text 'New'. After clicking, we verify the chat
        actually reset by checking that the previous response's
        'Start new conversation from here' branch button disappears.
        If the verification fails, we retry with a longer wait.
        """
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None

            log.info("Starting new chat")

            async def _attempt_click() -> bool:
                """Try to click + New. Returns True if click landed somewhere."""
                candidates = [
                    self._page.locator(
                        'button:has(svg.lucide-plus):has-text("New")'
                    ),
                    self._page.locator('button').filter(has_text="New"),
                    self._page.get_by_role("button", name="New", exact=True),
                ]
                for cand in candidates:
                    try:
                        count = await cand.count()
                        if count == 0:
                            continue
                        for i in range(count):
                            item = cand.nth(i)
                            if await item.is_visible():
                                # Scroll into view if needed, then click
                                await item.scroll_into_view_if_needed(timeout=2000)
                                await item.click(timeout=NAV_TIMEOUT_MS)
                                return True
                    except Exception:
                        continue
                return False

            async def _verify_new_chat() -> bool:
                """Check that the chat actually reset.

                Signal: the previous response's 'Start new conversation from
                here' branch button should no longer be present. We also
                check that the prompt editor is empty.
                """
                try:
                    state = await self._page.evaluate("""() => {
                        const branchBtn = document.querySelector(
                            'button[aria-label="Start new conversation from here"]'
                        );
                        const editors = document.querySelectorAll(
                            '[contenteditable="true"], .ProseMirror'
                        );
                        let editorIsEmpty = true;
                        for (const e of editors) {
                            const txt = (e.innerText || '').trim();
                            if (txt.length > 0 && txt !== e.getAttribute('data-placeholder')) {
                                editorIsEmpty = false;
                                break;
                            }
                        }
                        return {
                            branchPresent: branchBtn !== null,
                            editorEmpty: editorIsEmpty,
                        };
                    }""")
                    # Reset succeeded if branch button is gone (was there from prev response)
                    # If branchPresent is False, we definitely reset. If it's True, no reset.
                    return not state["branchPresent"]
                except Exception:
                    return False  # treat verification errors as "not verified"

            # Attempt 1
            if not await _attempt_click():
                raise NavigationError("Couldn't find + New button on first attempt")
            await asyncio.sleep(1.0)  # Give Kaelio time to react

            if await _verify_new_chat():
                log.debug("new_chat verified after attempt 1")
                return

            # Attempt 2 — sometimes Kaelio's React isn't ready for the click
            log.warning("new_chat didn't reset on first try; retrying")
            await asyncio.sleep(0.5)
            if not await _attempt_click():
                raise NavigationError("Couldn't find + New button on retry")
            await asyncio.sleep(1.5)  # Longer wait this time

            if await _verify_new_chat():
                log.debug("new_chat verified after attempt 2")
                return

            # Last resort: even if verification fails, we proceeded; warn and continue
            log.warning("new_chat verification failed after retry; "
                        "chat may not have reset")

    async def _find_prompt_editor(self):
        """Locate the TipTap prompt editor. Returns a Playwright locator.

        Multiple editors may exist on the page (chat history, drafts, etc.).
        We pick the one whose `data-placeholder` matches the known main prompt,
        and prefer a visible one. Falls back to any visible tiptap editor.
        """
        assert self._page is not None

        # Most specific: matches by placeholder text
        by_placeholder = self._page.locator(
            f'[data-placeholder="{PROMPT_PLACEHOLDER}"]'
        )
        count = await by_placeholder.count()
        for i in range(count):
            item = by_placeholder.nth(i)
            if await item.is_visible():
                # The data-placeholder is on a <p> inside the contenteditable div.
                # Walk up to the actual contenteditable container so .click()
                # hits the right thing.
                container = item.locator(
                    'xpath=ancestor::*[@contenteditable="true"][1]'
                )
                if await container.count() > 0:
                    return container.first
                return item

        # Fallback: any visible contenteditable tiptap editor
        by_class = self._page.locator('div.tiptap[contenteditable="true"]')
        count = await by_class.count()
        for i in range(count):
            item = by_class.nth(i)
            if await item.is_visible():
                return item

        raise PromptError(
            "Couldn't find the prompt editor on this page. "
            "Are you on the Home page? Try goto('home') first."
        )

    async def type_prompt(self, text: str) -> None:
        """Type text into the prompt editor.

        TipTap/ProseMirror editors require real keystrokes, not setting value.
        We scroll the editor into view, click it to focus, verify focus took,
        then type with a short delay per key.
        """
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None
            if not text:
                return

            log.info(f"Typing prompt: {text[:60]!r}...")
            editor = await self._find_prompt_editor()

            # Ensure editor is in view and clickable
            try:
                await editor.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass  # not fatal — editor may already be visible

            # Click squarely inside the editor to give it focus
            await editor.click()
            await asyncio.sleep(0.15)

            # Verify focus actually landed in the editor
            focused = await self._page.evaluate("""() => {
                const ae = document.activeElement;
                if (!ae) return null;
                return {
                    tag: ae.tagName,
                    contenteditable: ae.getAttribute('contenteditable'),
                    placeholder: ae.getAttribute('data-placeholder')
                        || ae.querySelector('[data-placeholder]')?.getAttribute('data-placeholder')
                        || null,
                };
            }""")
            log.debug(f"After click, focused element: {focused}")

            # If focus didn't land in a contenteditable, try clicking once more
            if not focused or focused.get("contenteditable") != "true":
                log.warning("Focus didn't land in editor on first click — retrying")
                await editor.click()
                await asyncio.sleep(0.15)

            # Type with a small per-key delay so React/TipTap don't drop input
            await self._page.keyboard.type(text, delay=15)

    async def submit_prompt(self) -> None:
        """Submit the prompt — press Enter inside the editor."""
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None
            log.info("Submitting prompt")

            editor = await self._find_prompt_editor()
            await editor.focus()
            await self._page.keyboard.press("Enter")
            # Wait a moment so the user sees the prompt go through before next command
            await asyncio.sleep(0.5)

    async def ask(self, question: str, start_new_chat: bool = True) -> None:
        """Compose-and-send: optionally + New, then type, then submit."""
        if start_new_chat:
            try:
                await self.new_chat()
            except NavigationError as e:
                log.warning(f"new_chat failed, continuing in current chat: {e}")
        await self.type_prompt(question)
        await self.submit_prompt()


    async def _inject_overlay(self) -> None:
        """Inject the Jarvis presence overlay into the current page.

        Safe to call repeatedly — the overlay removes any previous instance
        before mounting itself. Called automatically after navigation.
        """
        if not self._page:
            return
        overlay_js = "// Jarvis presence overlay \u2014 injected into Smart with Martin via Playwright.\n// Polls https://localhost:8000/api/jarvis_state every 1s and reflects state in UI.\n// Safe to re-inject: removes any existing instance first.\n(function () {\n    const OVERLAY_ID = 'jarvis-overlay-root';\n    const API_URL = 'https://localhost:8000/api/jarvis_state';\n    const POLL_INTERVAL_MS = 1000;\n    const CAPTION_FADE_MS = 5000;  // hide caption 5s after last speech ended\n\n    // \u2500\u2500 Idempotent injection: remove previous instance if present \u2500\u2500\u2500\u2500\n    const existing = document.getElementById(OVERLAY_ID);\n    if (existing) existing.remove();\n    // Also clear any previous polling interval\n    if (window.__jarvisOverlayPollId) {\n        clearInterval(window.__jarvisOverlayPollId);\n        window.__jarvisOverlayPollId = null;\n    }\n\n    // \u2500\u2500 Styles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    const style = document.createElement('style');\n    style.textContent = `\n        #${OVERLAY_ID} {\n            position: fixed;\n            bottom: 16px;\n            right: 16px;\n            z-index: 2147483647;\n            font-family: -apple-system, BlinkMacSystemFont, \"Inter\", \"Helvetica Neue\", sans-serif;\n            font-size: 12px;\n            color: #e4e4e7;\n            pointer-events: auto;\n        }\n        #${OVERLAY_ID} .jarvis-pill {\n            display: flex;\n            flex-direction: column;\n            background: rgba(15, 18, 28, 0.85);\n            backdrop-filter: blur(8px);\n            -webkit-backdrop-filter: blur(8px);\n            border: 1px solid rgba(255, 255, 255, 0.08);\n            border-radius: 999px;\n            padding: 8px 14px 8px 12px;\n            min-width: 140px;\n            max-width: 320px;\n            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);\n            cursor: pointer;\n            transition: all 0.25s ease;\n        }\n        #${OVERLAY_ID} .jarvis-pill.has-caption {\n            border-radius: 14px;\n            padding: 10px 14px 12px 12px;\n        }\n        #${OVERLAY_ID} .jarvis-row {\n            display: flex;\n            align-items: center;\n            gap: 8px;\n        }\n        #${OVERLAY_ID} .jarvis-orb {\n            width: 10px;\n            height: 10px;\n            border-radius: 50%;\n            background: radial-gradient(circle at 30% 30%, #6ec5ff, #1e6fb8 70%);\n            box-shadow: 0 0 8px rgba(110, 197, 255, 0.6);\n            flex-shrink: 0;\n            transition: background 0.3s ease, box-shadow 0.3s ease;\n        }\n        #${OVERLAY_ID} .jarvis-label {\n            font-weight: 500;\n            letter-spacing: 0.3px;\n            white-space: nowrap;\n            overflow: hidden;\n            text-overflow: ellipsis;\n        }\n        #${OVERLAY_ID} .jarvis-caption {\n            margin-top: 8px;\n            padding-top: 8px;\n            border-top: 1px solid rgba(255, 255, 255, 0.06);\n            font-size: 11px;\n            line-height: 1.45;\n            color: #c8c8d2;\n            max-height: 0;\n            overflow: hidden;\n            opacity: 0;\n            transition: max-height 0.3s ease, opacity 0.3s ease, margin-top 0.3s ease, padding-top 0.3s ease;\n        }\n        #${OVERLAY_ID} .jarvis-pill.has-caption .jarvis-caption {\n            max-height: 140px;\n            opacity: 1;\n        }\n        #${OVERLAY_ID} .jarvis-pill:hover {\n            border-color: rgba(255, 255, 255, 0.15);\n            background: rgba(15, 18, 28, 0.92);\n        }\n\n        /* \u2500\u2500 State-specific orb styles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 */\n        #${OVERLAY_ID} .state-idle .jarvis-orb {\n            background: radial-gradient(circle at 30% 30%, #6ec5ff, #1e6fb8 70%);\n            box-shadow: 0 0 8px rgba(110, 197, 255, 0.5);\n            animation: jarvis-pulse-slow 2.5s ease-in-out infinite;\n        }\n        #${OVERLAY_ID} .state-listening .jarvis-orb {\n            background: radial-gradient(circle at 30% 30%, #88d8ff, #1e88e5 70%);\n            box-shadow: 0 0 12px rgba(136, 216, 255, 0.8);\n            animation: jarvis-pulse-fast 1s ease-in-out infinite;\n        }\n        #${OVERLAY_ID} .state-thinking .jarvis-orb {\n            background: radial-gradient(circle at 30% 30%, #80f0e0, #00b3a4 70%);\n            box-shadow: 0 0 10px rgba(128, 240, 224, 0.7);\n            animation: jarvis-spin 1.8s linear infinite;\n        }\n        #${OVERLAY_ID} .state-speaking .jarvis-orb {\n            background: radial-gradient(circle at 30% 30%, #c89bff, #7c3aed 70%);\n            box-shadow: 0 0 14px rgba(200, 155, 255, 0.85);\n            animation: jarvis-pulse-fast 0.6s ease-in-out infinite;\n        }\n        #${OVERLAY_ID} .state-waiting_on_martin .jarvis-orb {\n            background: radial-gradient(circle at 30% 30%, #ffc18a, #e6730e 70%);\n            box-shadow: 0 0 12px rgba(255, 193, 138, 0.8);\n            animation: jarvis-pulse-slow 3s ease-in-out infinite;\n        }\n\n        @keyframes jarvis-pulse-slow {\n            0%, 100% { opacity: 0.7; transform: scale(1); }\n            50%      { opacity: 1;   transform: scale(1.12); }\n        }\n        @keyframes jarvis-pulse-fast {\n            0%, 100% { opacity: 0.85; transform: scale(1); }\n            50%      { opacity: 1;    transform: scale(1.18); }\n        }\n        @keyframes jarvis-spin {\n            from { box-shadow: 0 -10px 12px rgba(128, 240, 224, 0.7); }\n            25%  { box-shadow: 10px 0 12px rgba(128, 240, 224, 0.7); }\n            50%  { box-shadow: 0 10px 12px rgba(128, 240, 224, 0.7); }\n            75%  { box-shadow: -10px 0 12px rgba(128, 240, 224, 0.7); }\n            to   { box-shadow: 0 -10px 12px rgba(128, 240, 224, 0.7); }\n        }\n    `;\n    document.head.appendChild(style);\n\n    // \u2500\u2500 DOM \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    const root = document.createElement('div');\n    root.id = OVERLAY_ID;\n    root.innerHTML = `\n        <div class=\"jarvis-pill state-idle\" title=\"Click to switch to Jarvis\">\n            <div class=\"jarvis-row\">\n                <div class=\"jarvis-orb\"></div>\n                <div class=\"jarvis-label\">JARVIS \u00b7 ready</div>\n            </div>\n            <div class=\"jarvis-caption\"></div>\n        </div>\n    `;\n    document.body.appendChild(root);\n\n    const pill = root.querySelector('.jarvis-pill');\n    const labelEl = root.querySelector('.jarvis-label');\n    const captionEl = root.querySelector('.jarvis-caption');\n\n    // \u2500\u2500 Click \u2192 focus Jarvis tab with graceful fallback \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    pill.addEventListener('click', () => {\n        try {\n            // Named target: focuses existing tab if one exists with this name,\n            // otherwise opens a new one. Common pattern; works in most cases.\n            window.open('https://localhost:5174', 'jarvis_tab');\n        } catch (e) {\n            // Silently fail if blocked by browser policy\n            console.debug('[Jarvis overlay] focus failed:', e);\n        }\n    });\n\n    // \u2500\u2500 State display \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    const STATE_LABELS = {\n        idle:               'ready',\n        listening:          'listening...',\n        thinking:           'thinking...',\n        speaking:           'speaking',\n        waiting_on_martin:  'waiting on Martin...',\n    };\n    const ALL_STATE_CLASSES = Object.keys(STATE_LABELS).map(s => `state-${s}`);\n\n    function applyState(state) {\n        ALL_STATE_CLASSES.forEach(c => pill.classList.remove(c));\n        const cls = STATE_LABELS[state] ? `state-${state}` : 'state-idle';\n        pill.classList.add(cls);\n        labelEl.textContent = `JARVIS \u00b7 ${STATE_LABELS[state] || 'ready'}`;\n    }\n\n    function applyCaption(text, speechStartedAt, now, state) {\n        // Show caption while speaking, or for 5s after speaking ended\n        if (!text) {\n            pill.classList.remove('has-caption');\n            captionEl.textContent = '';\n            return;\n        }\n        const elapsedSinceSpeech = (now - speechStartedAt) * 1000;\n        const shouldShow = state === 'speaking' || elapsedSinceSpeech < CAPTION_FADE_MS;\n        if (shouldShow) {\n            captionEl.textContent = text;\n            pill.classList.add('has-caption');\n        } else {\n            pill.classList.remove('has-caption');\n        }\n    }\n\n    // \u2500\u2500 Polling loop \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    let lastShownState = null;\n    async function poll() {\n        try {\n            const resp = await fetch(API_URL, { method: 'GET', cache: 'no-store' });\n            if (!resp.ok) return;\n            const data = await resp.json();\n            applyState(data.state);\n            applyCaption(data.last_speech, data.speech_started_at, data.now, data.state);\n            lastShownState = data.state;\n        } catch (e) {\n            // Backend unreachable \u2014 show \"offline\" gently after a few failures\n            // For now we just stay on the last known state to avoid flickering\n            console.debug('[Jarvis overlay] poll failed:', e);\n        }\n    }\n\n    // First poll immediately, then on interval\n    poll();\n    window.__jarvisOverlayPollId = setInterval(poll, POLL_INTERVAL_MS);\n\n    console.log('[Jarvis overlay] mounted');\n})();\n"
        try:
            await self._page.evaluate(overlay_js)
        except Exception as e:
            log.debug(f"Overlay injection failed (page may not be ready): {e}")

    # ── View handling ─────────────────────────────────────

    async def list_views(self) -> list[dict]:
        """Return all view cards visible on the current page.

        Each entry: {"title": str, "description": str, "href": str}
        Only works on the Views page; returns empty list elsewhere.
        """
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None
            try:
                items = await self._page.evaluate(r"""() => {
                    // Kaelio view cards are <a href="/view/<uuid>"> elements
                    const anchors = document.querySelectorAll('a[href^="/view/"]');
                    const out = [];
                    for (const a of anchors) {
                        if (a.offsetParent === null) continue;  // skip hidden
                        // Title — first font-semibold span inside the anchor
                        const titleEl = a.querySelector('span.font-semibold')
                                     || a.querySelector('span');
                        const descEl = a.querySelector('p');
                        out.push({
                            title: (titleEl ? titleEl.innerText : '').trim(),
                            description: (descEl ? descEl.innerText : '').trim(),
                            href: a.getAttribute('href') || '',
                        });
                    }
                    return out;
                }""")
            except Exception as e:
                log.exception("list_views failed")
                return []
            log.info(f"Found {len(items)} view cards")
            return items

    async def open_view(self, query: str) -> str:
        """Click the view card whose title best matches `query`.

        Match strategy, in order:
          1. Case-insensitive exact substring on title
          2. Word-overlap score: how many query words appear in title
          3. Description substring (in case title was mangled by STT)
          4. Fuzzy similarity (≥0.6 ratio) as last resort

        Returns the canonical title of the view that was clicked.
        Raises:
          NavigationError if no view matches.
          NavigationError with "AMBIGUOUS: <list>" if multiple match equally.
        """
        async with self._lock:
            await self._ensure_started()
            assert self._page is not None

            views = await self._list_views_unlocked()
            if not views:
                raise NavigationError(
                    "I don't see any views on this page, sir. Try 'Martin go to Views' first."
                )

            match = _best_view_match(query, views)
            if match is None:
                raise NavigationError(
                    f"I couldn't find a view named '{query}', sir."
                )

            chosen, ambiguous = match
            if ambiguous:
                titles = ", ".join(v["title"] for v in ambiguous[:3])
                raise NavigationError(
                    f"AMBIGUOUS: I found multiple views matching '{query}': {titles}. "
                    "Please be more specific."
                )

            log.info(f"Opening view: {chosen['title']!r} ({chosen['href']})")
            # Click the anchor by href — most stable selector
            href = chosen['href']
            try:
                await self._page.locator(f'a[href="{href}"]').first.click(timeout=NAV_TIMEOUT_MS)
            except Exception as e:
                raise NavigationError(f"Couldn't click view {chosen['title']!r}: {e}")
            # Let the view page render
            try:
                await self._page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(NAV_SETTLE_MS / 1000)
            await self._inject_overlay()
            return chosen['title']

    async def _list_views_unlocked(self) -> list[dict]:
        """Internal list_views that assumes the lock is already held."""
        assert self._page is not None
        try:
            items = await self._page.evaluate(r"""() => {
                const anchors = document.querySelectorAll('a[href^="/view/"]');
                const out = [];
                for (const a of anchors) {
                    if (a.offsetParent === null) continue;
                    const titleEl = a.querySelector('span.font-semibold')
                                 || a.querySelector('span');
                    const descEl = a.querySelector('p');
                    out.push({
                        title: (titleEl ? titleEl.innerText : '').trim(),
                        description: (descEl ? descEl.innerText : '').trim(),
                        href: a.getAttribute('href') || '',
                    });
                }
                return out;
            }""")
        except Exception:
            log.exception("_list_views_unlocked failed")
            return []
        return items


# ── View matching helpers ─────────────────────────────────

def _normalize_for_match(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    import re
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _word_overlap_score(query_words: set[str], title_words: set[str]) -> float:
    """Fraction of query words that appear in the title."""
    if not query_words:
        return 0.0
    common = query_words & title_words
    return len(common) / len(query_words)


def _fuzzy_ratio(a: str, b: str) -> float:
    """Simple character-based similarity (longest common substring / max length).

    Not Levenshtein — much cheaper, good enough for our use.
    """
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    # Simple approach: how many characters of a appear in b in order?
    m = len(a)
    n = len(b)
    if m == 0 or n == 0:
        return 0.0
    # Count matching characters position-independent
    matched = 0
    b_chars = list(b)
    for ch in a:
        if ch in b_chars:
            b_chars.remove(ch)
            matched += 1
    return matched / max(m, n)


def _best_view_match(query: str, views: list[dict]) -> Optional[tuple]:
    """Find the best matching view for `query`.

    Returns either:
      (chosen_view, None) if there's a clear winner
      (chosen_view, [other_ambiguous_views]) if multiple match equally well
      None if no view matches at all
    """
    if not views:
        return None

    qnorm = _normalize_for_match(query)
    qwords = set(qnorm.split())
    if not qwords:
        return None

    scored = []
    for v in views:
        title_norm = _normalize_for_match(v["title"])
        desc_norm = _normalize_for_match(v.get("description", ""))
        title_words = set(title_norm.split())

        # Tier 1: exact substring on title
        if qnorm in title_norm:
            score = 1.0 + len(qnorm) / max(len(title_norm), 1)  # boost: shorter title = better fit
        # Tier 2: all query words present in title
        elif qwords.issubset(title_words):
            score = 0.95
        # Tier 3: most query words in title (≥75%)
        elif _word_overlap_score(qwords, title_words) >= 0.5:
            score = 0.6 + _word_overlap_score(qwords, title_words) * 0.3
        # Tier 4: substring in description
        elif qnorm in desc_norm:
            score = 0.55
        # Tier 5: fuzzy ratio
        else:
            fr = _fuzzy_ratio(qnorm, title_norm)
            if fr >= 0.6:
                score = 0.4 + fr * 0.1
            else:
                score = 0.0

        if score > 0:
            scored.append((score, v))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score = scored[0][0]
    top_view = scored[0][1]

    # Ambiguity check: are there other views very close to the top score?
    AMBIGUITY_THRESHOLD = 0.10
    rivals = [v for (s, v) in scored[1:] if top_score - s < AMBIGUITY_THRESHOLD]
    if rivals:
        return (top_view, [top_view] + rivals)

    return (top_view, None)
