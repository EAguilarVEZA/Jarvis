"""
martin_cli — test the MartinBrowser without going through Jarvis voice.

Usage examples:

  # Open the browser and navigate around
  python -m martin_cli goto home
  python -m martin_cli goto views
  python -m martin_cli goto metrics

  # Ask a question (starts a new chat by default)
  python -m martin_cli ask "How many leads did we generate last week?"

  # Sanity-check that the saved session still works
  python -m martin_cli check

  # Interactive mode — keep the browser open and accept commands at a prompt
  python -m martin_cli shell
"""

from __future__ import annotations

import asyncio
import sys

from martin_browser import (
    MartinBrowser,
    MartinError,
    NavigationError,
    SessionExpiredError,
    SIDEBAR_DESTINATIONS,
)


HELP = """
Commands:
  goto <destination>     Navigate to: home / views / automations / metrics / knowledge / evaluations
  ask <question>         Start a new chat and ask the question
  new                    Open a new chat (no question)
  check                  Print session status
  help                   Show this help
  quit                   Exit (closes Chrome)

You can also press Ctrl+C at any time to abort.
""".strip()


async def cmd_goto(browser: MartinBrowser, dest: str) -> None:
    canonical = await browser.goto(dest)
    print(f"  → at: {canonical}")


async def cmd_ask(browser: MartinBrowser, question: str) -> None:
    await browser.ask(question, start_new_chat=True)
    print(f"  → asked: {question}")


async def cmd_new(browser: MartinBrowser) -> None:
    await browser.new_chat()
    print("  → new chat opened")


async def cmd_check(browser: MartinBrowser) -> None:
    ready = await browser.is_ready()
    print(f"  Session ready: {ready}")


async def run_once(args: list[str]) -> int:
    if not args:
        print(HELP)
        return 0
    cmd, *rest = args
    browser = MartinBrowser()
    try:
        if cmd == "check":
            await cmd_check(browser)
        elif cmd == "goto" and rest:
            await cmd_goto(browser, rest[0])
        elif cmd == "ask" and rest:
            await cmd_ask(browser, " ".join(rest))
        elif cmd == "new":
            await cmd_new(browser)
        elif cmd in ("help", "-h", "--help"):
            print(HELP)
        elif cmd == "shell":
            return await run_shell(browser)
        else:
            print(f"Unknown command: {cmd!r}")
            print(HELP)
            return 1
        return 0
    except SessionExpiredError as e:
        print(f"  ! Session expired: {e}")
        print(f"  Run: python -m martin_setup")
        return 2
    except MartinError as e:
        print(f"  ! Error: {e}")
        return 1
    finally:
        # In one-shot mode we close the browser. Shell mode keeps it open.
        if cmd != "shell":
            await browser.close()


async def run_shell(browser: MartinBrowser) -> int:
    """Interactive prompt that keeps the browser open between commands."""
    print(HELP)
    print()
    # Lazy start so first error is visible inline
    try:
        await browser.start()
        print("  Browser ready. Type a command, or `help`.")
    except SessionExpiredError as e:
        print(f"  ! Session expired: {e}")
        print(f"  Run: python -m martin_setup")
        return 2

    try:
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, input, "martin> "
                )
            except (EOFError, KeyboardInterrupt):
                print()
                break

            line = line.strip()
            if not line:
                continue
            if line in ("quit", "exit", "q"):
                break
            if line == "help":
                print(HELP)
                continue

            cmd, _, rest = line.partition(" ")
            try:
                if cmd == "goto" and rest:
                    await cmd_goto(browser, rest)
                elif cmd == "ask" and rest:
                    await cmd_ask(browser, rest)
                elif cmd == "new":
                    await cmd_new(browser)
                elif cmd == "check":
                    await cmd_check(browser)
                else:
                    print(f"Unknown: {line!r}. Try `help`.")
            except MartinError as e:
                print(f"  ! {e}")
    finally:
        print("Closing browser...")
        await browser.close()
    return 0


def main() -> int:
    return asyncio.run(run_once(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
