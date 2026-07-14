from pathlib import Path

SERVER_PY = Path.home() / "Jarvis" / "server.py"
src = SERVER_PY.read_text()
original = src
changes = 0

GREEN="\033[92m"; YELLOW="\033[93m"; RESET="\033[0m"
def ok(m):   print(f"{GREEN}✅  {m}{RESET}")
def warn(m): print(f"{YELLOW}⚠️   {m}{RESET}")

# ── Fix 1: Add "check_mail" to fast keyword detector ──────────────────────
# The LLM is routing email requests to "screen" instead of "check_mail"
# We need to add stronger Outlook-specific keywords

OLD_MAIL_DETECT = '''    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}'''

NEW_MAIL_DETECT = '''    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update",
                             "unread messages", "unread on outlook", "messages on outlook",
                             "check outlook", "outlook email", "outlook mail",
                             "outlook messages", "outlook inbox", "messages in outlook",
                             "emails in outlook", "any messages", "new messages"]):
        return {"action": "check_mail"}'''

if "unread messages" in src:
    warn("Mail keywords already expanded — skipping")
elif OLD_MAIL_DETECT not in src:
    warn("Mail detect block not found — skipping")
else:
    src = src.replace(OLD_MAIL_DETECT, NEW_MAIL_DETECT)
    ok("Expanded Outlook mail keywords in fast detector")
    changes += 1

# ── Fix 2: Add "check_calendar" Outlook keywords too ──────────────────────
OLD_CAL_DETECT = '''    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}'''

NEW_CAL_DETECT = '''    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting",
                             "outlook calendar", "calendar in outlook", "check my calendar",
                             "check calendar", "calendar today", "my outlook calendar",
                             "what's on outlook", "whats on outlook"]):
        return {"action": "check_calendar"}'''

if "outlook calendar" in src and "check_calendar" in src:
    warn("Calendar keywords already expanded — skipping")
elif OLD_CAL_DETECT not in src:
    warn("Calendar detect block not found — skipping")
else:
    src = src.replace(OLD_CAL_DETECT, NEW_CAL_DETECT)
    ok("Expanded Outlook calendar keywords in fast detector")
    changes += 1

# ── Fix 3: Stop Outlook from stealing focus every 30s ─────────────────────
# Replace the aggressive ThreadPoolExecutor refresh with a no-op
# The on-demand lookups (_do_mail_lookup, _do_calendar_lookup) work fine
# The background refresh is what's making Outlook pop to front constantly

OLD_REFRESH = '''            if _OUTLOOK_AVAILABLE:
                try:
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                        _u = _pool.submit(lambda: _outlook_email.get_unread(10)).result(timeout=10)
                        _v = _pool.submit(_outlook_calendar.get_today).result(timeout=10)
                    _ctx_cache["mail"]     = _outlook_email.summarize_for_jarvis(_u)
                    _ctx_cache["calendar"] = _outlook_calendar.summarize_for_jarvis(_v)
                except Exception:
                    pass'''

NEW_REFRESH = '''            # Outlook context is fetched on-demand only (no background polling)
            # to prevent Outlook stealing window focus every 30 seconds
            pass  # _OUTLOOK_AVAILABLE — on-demand only'''

if "on-demand only" in src:
    warn("Focus fix already applied — skipping")
elif OLD_REFRESH not in src:
    warn("Background refresh block not found — skipping")
else:
    src = src.replace(OLD_REFRESH, NEW_REFRESH)
    ok("
cat > ~/Jarvis/fix_outlook.py << 'FIXEOF'
from pathlib import Path

SERVER_PY = Path.home() / "Jarvis" / "server.py"
src = SERVER_PY.read_text()
original = src
changes = 0

GREEN="\033[92m"; YELLOW="\033[93m"; RESET="\033[0m"
def ok(m):   print(f"{GREEN}✅  {m}{RESET}")
def warn(m): print(f"{YELLOW}⚠️   {m}{RESET}")

# ── Fix 1: Add "check_mail" to fast keyword detector ──────────────────────
# The LLM is routing email requests to "screen" instead of "check_mail"
# We need to add stronger Outlook-specific keywords

OLD_MAIL_DETECT = '''    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}'''

NEW_MAIL_DETECT = '''    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update",
                             "unread messages", "unread on outlook", "messages on outlook",
                             "check outlook", "outlook email", "outlook mail",
                             "outlook messages", "outlook inbox", "messages in outlook",
                             "emails in outlook", "any messages", "new messages"]):
        return {"action": "check_mail"}'''

if "unread messages" in src:
    warn("Mail keywords already expanded — skipping")
elif OLD_MAIL_DETECT not in src:
    warn("Mail detect block not found — skipping")
else:
    src = src.replace(OLD_MAIL_DETECT, NEW_MAIL_DETECT)
    ok("Expanded Outlook mail keywords in fast detector")
    changes += 1

# ── Fix 2: Add "check_calendar" Outlook keywords too ──────────────────────
OLD_CAL_DETECT = '''    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}'''

NEW_CAL_DETECT = '''    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting",
                             "outlook calendar", "calendar in outlook", "check my calendar",
                             "check calendar", "calendar today", "my outlook calendar",
                             "what's on outlook", "whats on outlook"]):
        return {"action": "check_calendar"}'''

if "outlook calendar" in src and "check_calendar" in src:
    warn("Calendar keywords already expanded — skipping")
elif OLD_CAL_DETECT not in src:
    warn("Calendar detect block not found — skipping")
else:
    src = src.replace(OLD_CAL_DETECT, NEW_CAL_DETECT)
    ok("Expanded Outlook calendar keywords in fast detector")
    changes += 1

# ── Fix 3: Stop Outlook from stealing focus every 30s ─────────────────────
# Replace the aggressive ThreadPoolExecutor refresh with a no-op
# The on-demand lookups (_do_mail_lookup, _do_calendar_lookup) work fine
# The background refresh is what's making Outlook pop to front constantly

OLD_REFRESH = '''            if _OUTLOOK_AVAILABLE:
                try:
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                        _u = _pool.submit(lambda: _outlook_email.get_unread(10)).result(timeout=10)
                        _v = _pool.submit(_outlook_calendar.get_today).result(timeout=10)
                    _ctx_cache["mail"]     = _outlook_email.summarize_for_jarvis(_u)
                    _ctx_cache["calendar"] = _outlook_calendar.summarize_for_jarvis(_v)
                except Exception:
                    pass'''

NEW_REFRESH = '''            # Outlook context is fetched on-demand only (no background polling)
            # to prevent Outlook stealing window focus every 30 seconds
            pass  # _OUTLOOK_AVAILABLE — on-demand only'''

if "on-demand only" in src:
    warn("Focus fix already applied — skipping")
elif OLD_REFRESH not in src:
    warn("Background refresh block not found — skipping")
else:
    src = src.replace(OLD_REFRESH, NEW_REFRESH)
    ok("Disabled background Outlook polling (was stealing focus every 30s)")
    changes += 1

# ── Write ──────────────────────────────────────────────────────────────────
if changes == 0:
    warn("Nothing changed.")
else:
    backup = SERVER_PY.with_suffix(".py.bak2")
    backup.write_text(original)
    SERVER_PY.write_text(src)
    ok(f"server.py updated ({changes} fix(es) applied) — backup → server.py.bak2")

print("""
Now restart Jarvis:
  lsof -ti :8340 | xargs kill -9 2>/dev/null; cd ~/Jarvis && source venv/bin/activate && python3 server.py --ssl

Then say:
  "Jarvis, any unread messages on Outlook?"
  "Jarvis, check my Outlook calendar for today"
""")
