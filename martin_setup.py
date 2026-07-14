"""
Smart with Martin — one-time login setup.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright


CONFIG_DIR = Path.home() / ".config" / "jarvis" / "smartwithmartin"
STORAGE_FILE = CONFIG_DIR / "storage_state.json"
HOME_SCREENSHOT = CONFIG_DIR / "home_after_login.png"
LOGIN_URL = "https://app.smartwithmartin.ai"


async def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("─" * 64)
    print("  Smart with Martin — one-time setup")
    print("─" * 64)
    print()
    print(f"Opening Chrome to: {LOGIN_URL}")
    print()
    print("In the Chrome window that opens:")
    print("  1. Wait for the login page")
    print("  2. Type or autofill your password")
    print("  3. Click Sign In")
    print("  4. Wait until you see the Home page")
    print("  5. Come back to THIS terminal and press Enter")
    print()
    print("(Press Ctrl+C here to abort without saving.)")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print(f"  Chrome window opened. Current URL: {page.url}")
        print()

        await asyncio.get_event_loop().run_in_executor(
            None, input, "  Press Enter once you're logged in and see the Home page... "
        )

        current_url = page.url
        print()
        print(f"  Current URL: {current_url}")

        password_input = await page.query_selector('input[type="password"]')
        if password_input is not None:
            visible = await password_input.is_visible()
            if visible:
                print()
                print("  ! A password field is still visible. Did you log in?")
                print("  ! NOT saving the session. Run this script again when ready.")
                await browser.close()
                return 1

        await context.storage_state(path=str(STORAGE_FILE))
        os.chmod(STORAGE_FILE, 0o600)
        print(f"  Saved session: {STORAGE_FILE}")

        try:
            await page.screenshot(path=str(HOME_SCREENSHOT), full_page=True)
            print(f"  Saved screenshot: {HOME_SCREENSHOT}")
        except Exception as e:
            print(f"  (Screenshot failed, not critical: {e})")

        print()
        print("  ✓ Setup complete. You can close the Chrome window.")
        print()
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
