"""
jarvis_outlook_web.py
---------------------
Reads Outlook Web (office365.com) via Chrome AppleScript.
Scrapes email and calendar data for Jarvis to read aloud.
"""

import subprocess
import logging
import json
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.outlook_web")


@dataclass
class WebEmail:
    subject: str
    sender: str
    preview: str
    is_unread: bool
    time_str: str


@dataclass
class WebEvent:
    subject: str
    start: str
    end: str
    location: str
    organizer: str


class OutlookWebReader:

    MAIL_URL     = "https://outlook.office365.com/mail"
    CALENDAR_URL = "https://outlook.office365.com/calendar/view/day"

    def _run_js(self, js: str, url_hint: str = "") -> Optional[str]:
        """Run JavaScript in the Chrome tab that matches url_hint."""
        # Find the right tab
        find_tab = f'''
tell application "Google Chrome"
    set theResult to ""
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "{url_hint}" then
                set theResult to execute t javascript "{{}}"
                return theResult
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''
        # Build the real script with JS injected
        escaped = js.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        script = f'''
tell application "Google Chrome"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "{url_hint}" then
                set r to execute t javascript "{escaped}"
                return r as string
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15
            )
            out = result.stdout.strip()
            if out == "NO_TAB":
                logger.warning(f"No Chrome tab found containing: {url_hint}")
                return None
            return out
        except Exception as e:
            logger.error(f"JS execution failed: {e}")
            return None

    def _open_url(self, url: str):
        """Open a URL in Chrome."""
        script = f'''
tell application "Google Chrome"
    activate
    open location "{url}"
end tell
'''
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)

    def ensure_mail_open(self):
        """Make sure Outlook Web mail is open in Chrome."""
        result = self._run_js("return document.title", "outlook.office")
        if not result:
            logger.info("Opening Outlook Web mail in Chrome")
            self._open_url(self.MAIL_URL)
            import time
            time.sleep(4)  # wait for page load

    def ensure_calendar_open(self):
        """Make sure Outlook Web calendar is open in Chrome."""
        result = self._run_js("return document.title", "outlook.office")
        if not result:
            logger.info("Opening Outlook Web calendar in Chrome")
            self._open_url(self.CALENDAR_URL)
            import time
            time.sleep(4)

    # ── EMAIL ──────────────────────────────────────────────────────────────

    def get_unread_emails(self, max_count: int = 10) -> list[WebEmail]:
        """Scrape unread emails from Outlook Web inbox."""
        self.ensure_mail_open()

        js = """
(function() {
    var results = [];
    
    // Try multiple selectors for different Outlook Web versions
    var rows = document.querySelectorAll('[role="option"]');
    if (!rows || rows.length === 0) {
        rows = document.querySelectorAll('[data-convid]');
    }
    if (!rows || rows.length === 0) {
        rows = document.querySelectorAll('.jGG6V');
    }
    
    var count = 0;
    for (var i = 0; i < rows.length && count < """ + str(max_count) + """; i++) {
        var row = rows[i];
        var text = row.innerText || row.textContent || "";
        
        // Check if unread (bold or unread class)
        var isUnread = row.querySelector('[class*="unread"]') !== null ||
                       row.querySelector('b') !== null ||
                       row.getAttribute('aria-label') && 
                       row.getAttribute('aria-label').toLowerCase().includes('unread');
        
        // Extract subject
        var subjectEl = row.querySelector('[class*="subject"]') || 
                        row.querySelector('[class*="Subject"]') ||
                        row.querySelector('b') ||
                        row.querySelector('[class*="itemSubject"]');
        var subject = subjectEl ? subjectEl.innerText.trim() : "";
        
        // Extract sender
        var senderEl = row.querySelector('[class*="sender"]') ||
                       row.querySelector('[class*="Sender"]') ||
                       row.querySelector('[class*="from"]');
        var sender = senderEl ? senderEl.innerText.trim() : "";
        
        // Extract preview
        var previewEl = row.querySelector('[class*="preview"]') ||
                        row.querySelector('[class*="Preview"]') ||
                        row.querySelector('[class*="body"]');
        var preview = previewEl ? previewEl.innerText.trim().substring(0, 150) : "";
        
        // Extract time
        var timeEl = row.querySelector('time') ||
                     row.querySelector('[class*="time"]') ||
                     row.querySelector('[class*="Time"]');
        var timeStr = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText).trim() : "";
        
        if (subject || sender) {
            results.push({
                subject: subject,
                sender: sender,
                preview: preview,
                isUnread: isUnread,
                time: timeStr
            });
            count++;
        }
    }
    return JSON.stringify(results);
})()
"""
        raw = self._run_js(js, "outlook.office")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            emails = []
            for item in data:
                emails.append(WebEmail(
                    subject=item.get("subject", "(no subject)"),
                    sender=item.get("sender", "Unknown"),
                    preview=item.get("preview", ""),
                    is_unread=item.get("isUnread", False),
                    time_str=item.get("time", ""),
                ))
            logger.info(f"Scraped {len(emails)} emails from Outlook Web")
            return emails
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Email parse error: {e} — raw: {raw[:200]}")
            return []

    def get_unread_count(self) -> int:
        """Get unread email count from page title or badge."""
        self.ensure_mail_open()
        js = """
(function() {
    // Check page title for unread count e.g. "(5) Inbox"
    var title = document.title || "";
    var match = title.match(/\\((\\d+)\\)/);
    if (match) return parseInt(match[1]);
    
    // Check for unread badge
    var badge = document.querySelector('[class*="unreadCount"]') ||
                document.querySelector('[aria-label*="unread"]');
    if (badge) {
        var num = parseInt(badge.innerText);
        if (!isNaN(num)) return num;
    }
    return -1;
})()
"""
        raw = self._run_js(js, "outlook.office")
        try:
            n = int(raw)
            return max(n, 0)
        except (TypeError, ValueError):
            return 0

    def summarize_emails_for_jarvis(self, emails: list[WebEmail]) -> str:
        if not emails:
            return "Your Outlook inbox appears clear, sir. No messages visible."
        unread = [e for e in emails if e.is_unread]
        total = len(emails)
        u_count = len(unread)
        lines = []
        if u_count > 0:
            lines.append(f"You have {u_count} unread email{'s' if u_count != 1 else ''} in Outlook, sir.")
            for i, e in enumerate(unread[:5], 1):
                lines.append(f"{i}. From {e.sender} — {e.subject}.")
                if e.preview:
                    lines.append(f"   Preview: {e.preview[:100]}")
        else:
            lines.append(f"No unread emails, sir. {total} messages in view.")
        return " ".join(lines)

    # ── CALENDAR ───────────────────────────────────────────────────────────

    def get_today_events(self) -> list[WebEvent]:
        """Scrape today's events from Outlook Web calendar."""
        self.ensure_calendar_open()
        import time
        time.sleep(2)

        js = """
(function() {
    var results = [];
    
    // Try multiple selectors for calendar events
    var events = document.querySelectorAll('[class*="calendarEvent"]');
    if (!events || events.length === 0) {
        events = document.querySelectorAll('[class*="CalendarEvent"]');
    }
    if (!events || events.length === 0) {
        events = document.querySelectorAll('[role="button"][class*="event"]');
    }
    if (!events || events.length === 0) {
        events = document.querySelectorAll('[class*="eventItem"]');
    }
    
    for (var i = 0; i < events.length; i++) {
        var evt = events[i];
        var text = evt.innerText || evt.textContent || "";
        var label = evt.getAttribute('aria-label') || text;
        
        // Try to extract subject
        var subjectEl = evt.querySelector('[class*="subject"]') ||
                        evt.querySelector('[class*="Subject"]') ||
                        evt.querySelector('[class*="title"]');
        var subject = subjectEl ? subjectEl.innerText.trim() : label.split("\\n")[0].trim();
        
        // Try to extract time
        var timeEl = evt.querySelector('time') ||
                     evt.querySelector('[class*="time"]');
        var timeStr = timeEl ? timeEl.innerText.trim() : "";
        
        // Try from aria-label which often has full info
        var ariaLabel = evt.getAttribute('aria-label') || "";
        
        if (subject && subject.length > 0) {
            results.push({
                subject: subject,
                start: timeStr,
                end: "",
                location: "",
                organizer: "",
                ariaLabel: ariaLabel.substring(0, 200)
            });
        }
    }
    
    // Deduplicate by subject
    var seen = {};
    var unique = [];
    for (var j = 0; j < results.length; j++) {
        if (!seen[results[j].subject]) {
            seen[results[j].subject] = true;
            unique.push(results[j]);
        }
    }
    return JSON.stringify(unique);
})()
"""
        raw = self._run_js(js, "outlook.office")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            events = []
            for item in data:
                events.append(WebEvent(
                    subject=item.get("subject", "(no subject)"),
                    start=item.get("start", item.get("ariaLabel", "")[:50]),
                    end=item.get("end", ""),
                    location=item.get("location", ""),
                    organizer=item.get("organizer", ""),
                ))
            logger.info(f"Scraped {len(events)} calendar events from Outlook Web")
            return events
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Calendar parse error: {e}")
            return []

    def summarize_calendar_for_jarvis(self, events: list[WebEvent]) -> str:
        if not events:
            return "Your Outlook calendar appears clear today, sir. No events found on screen."
        lines = [f"You have {len(events)} event{'s' if len(events) != 1 else ''} on your calendar today, sir."]
        for i, e in enumerate(events, 1):
            time_str = f" at {e.start}" if e.start else ""
            loc = f" in {e.location}" if e.location else ""
            lines.append(f"{i}. {e.subject}{time_str}{loc}.")
        return " ".join(lines)

    # ── DAILY BRIEFING ─────────────────────────────────────────────────────

    def get_daily_briefing(self) -> str:
        """Full morning briefing — email + calendar combined."""
        from datetime import date
        today = date.today().strftime("%A, %B %d")

        emails  = self.get_unread_emails(max_count=10)
        events  = self.get_today_events()

        email_summary    = self.summarize_emails_for_jarvis(emails)
        calendar_summary = self.summarize_calendar_for_jarvis(events)

        briefing = (
            f"Good morning, sir. Today is {today}. "
            f"{calendar_summary} "
            f"{email_summary} "
            f"Shall I help you prioritise your day?"
        )
        return briefing
