#!/usr/bin/env python3
import re
from pathlib import Path

JARVIS_DIR = Path.home() / "Jarvis"
SERVER_PY  = JARVIS_DIR / "server.py"

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; RESET="\033[0m"
def ok(m):   print(f"{GREEN}✅  {m}{RESET}")
def warn(m): print(f"{YELLOW}⚠️   {m}{RESET}")
def err(m):  print(f"{RED}❌  {m}{RESET}")

src = SERVER_PY.read_text()
original = src
changes = 0

print("\n" + "="*54)
print("  JARVIS — Outlook Integration Patcher")
print("="*54)

# ── 1. Imports ──────────────────────────────────────────────
IMPORT_MARKER = 'log = logging.getLogger("jarvis.actions")'
IMPORT_BLOCK = """
try:
    from jarvis_outlook_email    import OutlookEmailReader
    from jarvis_outlook_calendar import OutlookCalendarReader
    _outlook_email    = OutlookEmailReader()
    _outlook_calendar = OutlookCalendarReader()
    _OUTLOOK_AVAILABLE = True
except ImportError:
    _OUTLOOK_AVAILABLE = False
"""
if "_OUTLOOK_AVAILABLE" in src:
    warn("Outlook imports already present — skipping")
elif IMPORT_MARKER not in src:
    warn("Import marker not found — skipping")
else:
    src = src.replace(IMPORT_MARKER, IMPORT_MARKER + "\n" + IMPORT_BLOCK)
    ok("Injected Outlook imports"); changes += 1

# ── 2. Calendar lookup ──────────────────────────────────────
OLD_CAL = '''async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)'''

NEW_CAL = '''async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — Outlook first, Mac Calendar fallback."""
    if _OUTLOOK_AVAILABLE:
        try:
            import asyncio as _a
            events = await _a.get_event_loop().run_in_executor(None, _outlook_calendar.get_today)
            summary = _outlook_calendar.summarize_for_jarvis(events)
            _ctx_cache["calendar"] = summary
            log.info(f"[outlook_cal] {len(events)} events today")
            return "Your Outlook calendar is clear today, sir." if not events else summary
        except Exception as _e:
            log.warning(f"[outlook_cal] fallback: {_e}")
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)'''

if "_outlook_calendar.get_today" in src:
    warn("Calendar lookup already patched — skipping")
elif OLD_CAL not in src:
    warn("_do_calendar_lookup not found — skipping")
else:
    src = src.replace(OLD_CAL, NEW_CAL)
    ok("Patched _do_calendar_lookup → Outlook first"); changes += 1

# ── 3. Mail lookup ──────────────────────────────────────────
OLD_MAIL = '''async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn\'t reach Mail at the moment, sir."'''

NEW_MAIL = '''async def _do_mail_lookup() -> str:
    """Slow mail fetch — Outlook first, Mac Mail fallback."""
    if _OUTLOOK_AVAILABLE:
        try:
            import asyncio as _a
            unread = await _a.get_event_loop().run_in_executor(None, lambda: _outlook_email.get_unread(10))
            _ctx_cache["mail"] = _outlook_email.summarize_for_jarvis(unread)
            log.info(f"[outlook_mail] {len(unread)} unread")
            if not unread:
                return "Your Outlook inbox is clear, sir. No unread messages."
            top = unread[:3]
            details = ". ".join(f"{_short_sender(e.sender)} regarding {e.subject}" for e in top)
            return f"You have {len(unread)} unread email{'s' if len(unread)!=1 else ''} in Outlook, sir. Most recent: {details}."
        except Exception as _e:
            log.warning(f"[outlook_mail] fallback: {_e}")
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(f"{_short_sender(m['sender'])} regarding {m['subject']}" for m in top)
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn\'t reach Mail at the moment, sir."'''

if "_outlook_email.get_unread" in src:
    warn("Mail lookup already patched — skipping")
elif OLD_MAIL not in src:
    warn("_do_mail_lookup not found — skipping")
else:
    src = src.replace(OLD_MAIL, NEW_MAIL)
    ok("Patched _do_mail_lookup → Outlook first"); changes += 1

# ── 4. Background context refresh ──────────────────────────
REFRESH_MARKER = "time.sleep(30)"
OUTLOOK_REFRESH = """
            if _OUTLOOK_AVAILABLE:
                try:
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                        _u = _pool.submit(lambda: _outlook_email.get_unread(10)).result(timeout=10)
                        _v = _pool.submit(_outlook_calendar.get_today).result(timeout=10)
                    _ctx_cache["mail"]     = _outlook_email.summarize_for_jarvis(_u)
                    _ctx_cache["calendar"] = _outlook_calendar.summarize_for_jarvis(_v)
                except Exception:
                    pass
"""
if "# ── Outlook email" in src:
    warn("Context refresh already patched — skipping")
elif REFRESH_MARKER not in src:
    warn("time.sleep(30) not found — skipping refresh patch")
else:
    src = src.replace(REFRESH_MARKER, OUTLOOK_REFRESH + "\n            " + REFRESH_MARKER, 1)
    ok("Injected Outlook background refresh every 30s"); changes += 1

# ── Write ───────────────────────────────────────────────────
if changes == 0:
    warn("Nothing to change — already up to date.")
else:
    backup = SERVER_PY.with_suffix(".py.bak")
    backup.write_text(original)
    ok(f"Backup saved → server.py.bak")
    SERVER_PY.write_text(src)
    ok(f"server.py updated with {changes} change(s)")

import subprocess, sys
r = subprocess.run([sys.executable, "-c",
    "from jarvis_outlook_email import OutlookEmailReader;"
    "from jarvis_outlook_calendar import OutlookCalendarReader; print('OK')"],
    capture_output=True, text=True, cwd=str(JARVIS_DIR))
print()
if "OK" in r.stdout:
    ok("Module import check passed")
else:
    err(f"Import check failed: {r.stderr}")

print("""
======================================================
  ✅  Done! Now restart Jarvis with:

  lsof -ti :8340 | xargs kill -9 2>/dev/null; cd ~/Jarvis && source venv/bin/activate && python3 server.py --ssl

  Then say:
    "Jarvis, do I have any unread emails?"
    "Jarvis, what's on my Outlook calendar today?"
======================================================
""")
