"""
Diagnostic add-on: writes a debug script that captures what's happening on the page.
Run via: python -m martin_debug
"""
import asyncio
import sys
from pathlib import Path
from martin_browser import MartinBrowser

async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "home"
    browser = MartinBrowser()
    await browser.start()

    print(f"=== Diagnostic: command '{cmd}' ===")
    print()

    page = browser._page  # access private for debug

    # Capture before
    print(f"Before: url = {page.url}")
    before_title = await page.title()
    print(f"Before: title = {before_title!r}")
    # What sidebar item looks "selected"?
    sel = await page.evaluate("""() => {
        // Find any element with 'active' or 'selected' or 'bg-' class containing text in sidebar
        const items = document.querySelectorAll('nav a, nav button, aside a, aside button');
        return Array.from(items).map(el => ({
            text: (el.innerText || '').trim().slice(0, 40),
            classes: el.className.slice(0, 100),
        })).filter(x => x.text);
    }""")
    print(f"Before: {len(sel)} sidebar items detected")
    for s in sel[:15]:
        print(f"  - {s['text']!r}")
    print()

    if cmd == "ask":
        # Try the new + type + submit flow
        print("Step 1: clicking + New")
        await browser.new_chat()
        await asyncio.sleep(1.5)  # extra wait

        # What does the URL look like now?
        print(f"After new_chat: url = {page.url}")

        # How many tiptap editors are on the page now?
        editors = await page.evaluate("""() => {
            const tts = document.querySelectorAll('div.tiptap, [contenteditable="true"]');
            return Array.from(tts).map(el => ({
                tag: el.tagName,
                placeholder: el.getAttribute('data-placeholder') || el.querySelector('[data-placeholder]')?.getAttribute('data-placeholder') || null,
                classes: el.className.slice(0, 100),
                visible: el.offsetParent !== null,
            }));
        }""")
        print(f"Found {len(editors)} editor candidates:")
        for e in editors:
            print(f"  - {e['tag']}  visible={e['visible']}  placeholder={e['placeholder']!r}")
            print(f"    classes={e['classes']}")
        print()

        # Try typing with extra logging
        print("Step 2: type_prompt 'hello test'")
        try:
            await browser.type_prompt("hello test")
            print("  Type succeeded")
        except Exception as e:
            print(f"  Type FAILED: {e}")

        # Take a screenshot for evidence
        shot = Path.home() / ".config" / "jarvis" / "smartwithmartin" / "debug_after_type.png"
        await page.screenshot(path=str(shot), full_page=False)
        print(f"  Screenshot: {shot}")

    elif cmd in ("home", "views", "metrics", "knowledge", "evaluations", "automations"):
        print(f"Step 1: goto {cmd}")
        await browser.goto(cmd)
        await asyncio.sleep(1.0)
        print(f"After goto: url = {page.url}")
        after_title = await page.title()
        print(f"After: title = {after_title!r}")

        # Capture breadcrumb if any
        breadcrumb = await page.evaluate("""() => {
            const bc = document.querySelector('[aria-label*=readcrumb], nav[class*=readcrumb], .breadcrumb');
            return bc ? bc.innerText.trim() : null;
        }""")
        print(f"After: breadcrumb = {breadcrumb!r}")

        # Take a screenshot
        shot = Path.home() / ".config" / "jarvis" / "smartwithmartin" / f"debug_{cmd}.png"
        await page.screenshot(path=str(shot), full_page=False)
        print(f"  Screenshot: {shot}")

    print()
    print("Leaving browser open for 8 seconds so you can inspect...")
    await asyncio.sleep(8)
    await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
