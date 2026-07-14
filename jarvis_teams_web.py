"""
jarvis_teams_web.py
-------------------
Reads Microsoft Teams Web (teams.microsoft.com) via Chrome AppleScript.
Scrapes messages, mentions, channels, and meetings for Jarvis.
Read-only by default. Send messages only with explicit user permission.
"""

import subprocess
import logging
import json
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.teams_web")


@dataclass
class TeamsMessage:
    sender: str
    preview: str
    channel: str
    is_unread: bool
    is_mention: bool
    time_str: str
    is_direct: bool


@dataclass
class TeamsMeeting:
    subject: str
    start: str
    end: str
    organizer: str
    attendee_count: int
    join_url: str


class TeamsWebReader:

    TEAMS_URL = "https://teams.microsoft.com"

    def _run_js(self, js: str) -> Optional[str]:
        """Run JavaScript in the Chrome tab containing teams.microsoft.com."""
        escaped = js.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        script = f'''
tell application "Google Chrome"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "teams.microsoft.com" then
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
                capture_output=True, text=True, timeout=20
            )
            out = result.stdout.strip()
            if out == "NO_TAB":
                logger.warning("No Chrome tab found with teams.microsoft.com")
                return None
            return out
        except Exception as e:
            logger.error(f"Teams JS execution failed: {e}")
            return None

    def _open_teams(self):
        """Open Teams Web in Chrome if not already open."""
        script = f'''
tell application "Google Chrome"
    activate
    open location "{self.TEAMS_URL}"
end tell
'''
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        import time
        time.sleep(4)

    def ensure_open(self):
        """Make sure Teams Web is open in Chrome."""
        result = self._run_js("return document.title")
        if not result:
            logger.info("Opening Teams Web in Chrome")
            self._open_teams()

    # ── UNREAD MESSAGES ────────────────────────────────────────────────────

    def get_unread_messages(self, max_count: int = 10) -> list[TeamsMessage]:
        """Scrape unread messages and mentions from Teams Web."""
        self.ensure_open()

        js = """
(function() {
    var results = [];

    // --- Direct Messages / Chat ---
    var chatItems = document.querySelectorAll('[data-tid="chat-list-item"]');
    if (!chatItems.length) chatItems = document.querySelectorAll('[class*="chatListItem"]');
    if (!chatItems.length) chatItems = document.querySelectorAll('[role="listitem"]');

    chatItems.forEach(function(item) {
        try {
            var label = item.getAttribute('aria-label') || '';
            var isUnread = item.querySelector('[class*="unread"]') !== null ||
                           item.querySelector('[class*="badge"]') !== null ||
                           label.toLowerCase().includes('unread') ||
                           label.toLowerCase().includes('new message');

            var senderEl = item.querySelector('[class*="sender"]') ||
                           item.querySelector('[class*="title"]') ||
                           item.querySelector('span[class*="name"]');
            var sender = senderEl ? senderEl.innerText.trim() : '';

            var previewEl = item.querySelector('[class*="preview"]') ||
                            item.querySelector('[class*="subtitle"]') ||
                            item.querySelector('[class*="lastMessage"]');
            var preview = previewEl ? previewEl.innerText.trim().substring(0, 150) : '';

            var timeEl = item.querySelector('time') ||
                         item.querySelector('[class*="time"]') ||
                         item.querySelector('[class*="timestamp"]');
            var timeStr = timeEl ? timeEl.innerText.trim() : '';

            var isMention = label.toLowerCase().includes('mention') ||
                            label.toLowerCase().includes('@') ||
                            (preview && preview.includes('@'));

            if (sender && (isUnread || isMention)) {
                results.push({
                    sender: sender,
                    preview: preview,
                    channel: 'Direct Message',
                    isUnread: isUnread,
                    isMention: isMention,
                    time: timeStr,
                    isDirect: true
                });
            }
        } catch(e) {}
    });

    // --- Channel Messages ---
    var channelItems = document.querySelectorAll('[data-tid="channel-list-item"]');
    if (!channelItems.length) channelItems = document.querySelectorAll('[class*="channelItem"]');

    channelItems.forEach(function(item) {
        try {
            var label = item.getAttribute('aria-label') || '';
            var isUnread = item.querySelector('[class*="unread"]') !== null ||
                           item.querySelector('[class*="badge"]') !== null ||
                           label.toLowerCase().includes('unread');
            var isMention = label.toLowerCase().includes('mention') ||
                            label.toLowerCase().includes('@');

            var nameEl = item.querySelector('[class*="channelName"]') ||
                         item.querySelector('[class*="name"]') ||
                         item.querySelector('span');
            var channelName = nameEl ? nameEl.innerText.trim() : label;

            var badgeEl = item.querySelector('[class*="badge"]') ||
                          item.querySelector('[class*="count"]');
            var badgeCount = badgeEl ? badgeEl.innerText.trim() : '';

            if (channelName && (isUnread || isMention)) {
                results.push({
                    sender: 'Channel',
                    preview: badgeCount ? badgeCount + ' new messages' : 'New activity',
                    channel: channelName,
                    isUnread: isUnread,
                    isMention: isMention,
                    time: '',
                    isDirect: false
                });
            }
        } catch(e) {}
    });

    // --- Activity Feed (mentions & replies) ---
    var activityItems = document.querySelectorAll('[data-tid="activity-item"]');
    if (!activityItems.length) activityItems = document.querySelectorAll('[class*="activityItem"]');

    activityItems.forEach(function(item) {
        try {
            var label = item.getAttribute('aria-label') || item.innerText || '';
            var isMention = label.toLowerCase().includes('mention') ||
                            label.toLowerCase().includes('@');
            var isReply = label.toLowerCase().includes('replied');

            if (isMention || isReply) {
                var lines = label.split('\\n').filter(function(l) { return l.trim(); });
                results.push({
                    sender: lines[0] || 'Someone',
                    preview: lines.slice(1).join(' ').substring(0, 150),
                    channel: 'Activity',
                    isUnread: true,
                    isMention: isMention,
                    time: '',
                    isDirect: false
                });
            }
        } catch(e) {}
    });

    // Deduplicate by sender+preview
    var seen = {};
    var unique = [];
    results.forEach(function(r) {
        var key = r.sender + r.preview;
        if (!seen[key]) {
            seen[key] = true;
            unique.push(r);
        }
    });

    return JSON.stringify(unique.slice(0, """ + str(max_count) + """));
})()
"""
        raw = self._run_js(js)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            messages = []
            for item in data:
                messages.append(TeamsMessage(
                    sender=item.get("sender", "Unknown"),
                    preview=item.get("preview", ""),
                    channel=item.get("channel", ""),
                    is_unread=item.get("isUnread", False),
                    is_mention=item.get("isMention", False),
                    time_str=item.get("time", ""),
                    is_direct=item.get("isDirect", False),
                ))
            logger.info(f"Scraped {len(messages)} Teams messages")
            return messages
        except Exception as e:
            logger.error(f"Teams message parse error: {e}")
            return []

    def get_unread_count(self) -> dict:
        """Get unread counts — total, mentions, DMs, channels."""
        self.ensure_open()
        js = """
(function() {
    var total = 0;
    var mentions = 0;
    var dms = 0;
    var channels = 0;

    // Check page title for badge count
    var title = document.title || '';
    var match = title.match(/(\\d+)/);
    if (match) total = parseInt(match[1]);

    // Count unread DMs
    var dmItems = document.querySelectorAll('[data-tid="chat-list-item"]');
    dmItems.forEach(function(item) {
        if (item.querySelector('[class*="unread"]') || item.querySelector('[class*="badge"]')) {
            dms++;
            total = Math.max(total, dms);
        }
    });

    // Count unread channels
    var chItems = document.querySelectorAll('[data-tid="channel-list-item"]');
    chItems.forEach(function(item) {
        if (item.querySelector('[class*="unread"]') || item.querySelector('[class*="badge"]')) {
            channels++;
        }
        if (item.querySelector('[class*="mention"]')) mentions++;
    });

    return JSON.stringify({total: total, mentions: mentions, dms: dms, channels: channels});
})()
"""
        raw = self._run_js(js)
        try:
            return json.loads(raw)
        except Exception:
            return {"total": 0, "mentions": 0, "dms": 0, "channels": 0}

    # ── MEETINGS ───────────────────────────────────────────────────────────

    def get_todays_meetings(self) -> list[TeamsMeeting]:
        """Scrape today's meetings from Teams calendar."""
        self.ensure_open()

        # Navigate to calendar view
        nav_js = """
(function() {
    // Click calendar icon in left nav
    var calBtn = document.querySelector('[data-tid="app-bar-calendar"]') ||
                 document.querySelector('[aria-label*="Calendar"]') ||
                 document.querySelector('[aria-label*="calendar"]') ||
                 document.querySelector('[class*="calendar"][role="button"]');
    if (calBtn) { calBtn.click(); return 'clicked'; }
    return 'not_found';
})()
"""
        self._run_js(nav_js)
        import time
        time.sleep(2)

        js = """
(function() {
    var results = [];

    // Look for meeting/event items in calendar view
    var events = document.querySelectorAll('[class*="calendarEvent"]');
    if (!events.length) events = document.querySelectorAll('[class*="event"][role="button"]');
    if (!events.length) events = document.querySelectorAll('[class*="meetingItem"]');
    if (!events.length) events = document.querySelectorAll('[data-tid*="meeting"]');

    events.forEach(function(evt) {
        try {
            var label = evt.getAttribute('aria-label') || evt.innerText || '';
            var lines = label.split('\\n').filter(function(l) { return l.trim(); });

            var subject = lines[0] || '(no title)';
            var timeStr = lines[1] || '';
            var organizer = '';
            var joinUrl = '';

            // Look for join button
            var joinBtn = evt.querySelector('[class*="join"]') ||
                          evt.querySelector('[aria-label*="Join"]');
            if (joinBtn) joinUrl = 'available';

            if (subject) {
                results.push({
                    subject: subject,
                    start: timeStr,
                    end: '',
                    organizer: organizer,
                    attendeeCount: 0,
                    joinUrl: joinUrl,
                    ariaLabel: label.substring(0, 300)
                });
            }
        } catch(e) {}
    });

    // Also check for upcoming meeting banner
    var banner = document.querySelector('[class*="upcomingMeeting"]') ||
                 document.querySelector('[class*="joinBanner"]') ||
                 document.querySelector('[data-tid*="upcoming"]');
    if (banner) {
        var bannerText = banner.innerText || '';
        if (bannerText) {
            results.unshift({
                subject: bannerText.split('\\n')[0] || 'Upcoming Meeting',
                start: 'Starting soon',
                end: '',
                organizer: '',
                attendeeCount: 0,
                joinUrl: 'available',
                ariaLabel: bannerText
            });
        }
    }

    return JSON.stringify(results);
})()
"""
        raw = self._run_js(js)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            meetings = []
            for item in data:
                meetings.append(TeamsMeeting(
                    subject=item.get("subject", "(no title)"),
                    start=item.get("start", ""),
                    end=item.get("end", ""),
                    organizer=item.get("organizer", ""),
                    attendee_count=item.get("attendeeCount", 0),
                    join_url=item.get("joinUrl", ""),
                ))
            logger.info(f"Scraped {len(meetings)} Teams meetings")
            return meetings
        except Exception as e:
            logger.error(f"Teams meeting parse error: {e}")
            return []

    # ── STATUS ─────────────────────────────────────────────────────────────

    def set_status(self, status: str) -> bool:
        """
        Set Teams presence status.
        status options: Available, Busy, DoNotDisturb, BeRightBack, Away, Offline
        """
        self.ensure_open()
        js = f"""
(function() {{
    // Click avatar/profile to open status menu
    var avatar = document.querySelector('[data-tid="me-control-avatar"]') ||
                 document.querySelector('[aria-label*="Profile"]') ||
                 document.querySelector('[class*="profilePicture"]');
    if (!avatar) return 'avatar_not_found';
    avatar.click();
    return 'avatar_clicked';
}})()
"""
        result = self._run_js(js)
        if result != 'avatar_clicked':
            return False

        import time
        time.sleep(1)

        status_map = {
            "available":     "Available",
            "busy":          "Busy",
            "donotdisturb":  "Do not disturb",
            "beRightBack":   "Be right back",
            "away":          "Away",
            "offline":       "Appear offline",
        }
        display_status = status_map.get(status.lower().replace(" ", ""), status)

        js2 = f"""
(function() {{
    var items = document.querySelectorAll('[role="menuitem"]');
    for (var i = 0; i < items.length; i++) {{
        if (items[i].innerText.toLowerCase().includes('{display_status.lower()}')) {{
            items[i].click();
            return 'status_set';
        }}
    }}
    return 'status_not_found';
}})()
"""
        result2 = self._run_js(js2)
        return result2 == 'status_set'

    # ── SEND MESSAGE (requires explicit user permission) ───────────────────

    def send_direct_message(self, recipient: str, message: str) -> bool:
        """
        Send a direct message in Teams Web.
        REQUIRES explicit user confirmation before calling.
        """
        self.ensure_open()

        # Click New Chat
        js = """
(function() {
    var newChat = document.querySelector('[data-tid="new-chat-button"]') ||
                  document.querySelector('[aria-label*="New chat"]') ||
                  document.querySelector('[class*="newChat"]');
    if (newChat) { newChat.click(); return 'clicked'; }
    return 'not_found';
})()
"""
        result = self._run_js(js)
        if result != 'clicked':
            return False

        import time
        time.sleep(1)

        # Type recipient name
        escaped_recipient = recipient.replace("'", "\\'")
        js2 = f"""
(function() {{
    var searchBox = document.querySelector('[data-tid="new-chat-to-field"]') ||
                    document.querySelector('[aria-label*="To"]') ||
                    document.querySelector('[placeholder*="name"]');
    if (!searchBox) return 'search_not_found';
    searchBox.focus();
    searchBox.value = '{escaped_recipient}';
    searchBox.dispatchEvent(new Event('input', {{bubbles: true}}));
    return 'typed';
}})()
"""
        self._run_js(js2)
        time.sleep(2)

        # Select first result
        js3 = """
(function() {
    var results = document.querySelectorAll('[data-tid="suggest-list-item"]');
    if (!results.length) results = document.querySelectorAll('[class*="suggestionItem"]');
    if (results.length > 0) { results[0].click(); return 'selected'; }
    return 'no_results';
})()
"""
        result3 = self._run_js(js3)
        if result3 != 'selected':
            return False

        time.sleep(1)

        # Type and send message
        escaped_msg = message.replace("'", "\\'").replace("\\n", " ")
        js4 = f"""
(function() {{
    var box = document.querySelector('[data-tid="ckeditor"]') ||
              document.querySelector('[aria-label*="message"]') ||
              document.querySelector('[class*="messageInput"]') ||
              document.querySelector('[contenteditable="true"]');
    if (!box) return 'box_not_found';
    box.focus();
    document.execCommand('insertText', false, '{escaped_msg}');
    box.dispatchEvent(new Event('input', {{bubbles: true}}));
    return 'typed';
}})()
"""
        result4 = self._run_js(js4)
        if result4 != 'typed':
            return False

        time.sleep(0.5)

        # Hit Enter to send
        js5 = """
(function() {
    var box = document.querySelector('[data-tid="ckeditor"]') ||
              document.querySelector('[contenteditable="true"]');
    if (!box) return 'not_found';
    var evt = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true});
    box.dispatchEvent(evt);
    return 'sent';
})()
"""
        result5 = self._run_js(js5)
        logger.info(f"Teams DM send result: {result5}")
        return result5 == 'sent'

    # ── SUMMARIES FOR JARVIS ───────────────────────────────────────────────

    def summarize_messages_for_jarvis(self, messages: list[TeamsMessage]) -> str:
        if not messages:
            return "No unread messages in Teams, sir."

        mentions = [m for m in messages if m.is_mention]
        dms = [m for m in messages if m.is_direct]
        channels = [m for m in messages if not m.is_direct and not m.is_mention]

        parts = [f"You have {len(messages)} unread item(s) in Teams, sir."]

        if mentions:
            parts.append(f"{len(mentions)} mention(s):")
            for m in mentions[:3]:
                parts.append(f"{m.sender} mentioned you in {m.channel}. {m.preview[:80]}")

        if dms:
            parts.append(f"{len(dms)} direct message(s):")
            for m in dms[:3]:
                parts.append(f"{m.sender} sent you a message. {m.preview[:80]}")

        if channels:
            ch_names = list(set(m.channel for m in channels))[:3]
            parts.append(f"New activity in: {', '.join(ch_names)}.")

        return " ".join(parts)

    def summarize_meetings_for_jarvis(self, meetings: list[TeamsMeeting]) -> str:
        if not meetings:
            return "No Teams meetings found today, sir."
        parts = [f"You have {len(meetings)} Teams meeting(s) today, sir."]
        for i, m in enumerate(meetings, 1):
            time_str = f" at {m.start}" if m.start else ""
            join_str = " A join button is available." if m.join_url else ""
            parts.append(f"{i}. {m.subject}{time_str}.{join_str}")
        return " ".join(parts)

    def get_full_briefing(self) -> str:
        """Full Teams briefing — messages + meetings."""
        messages = self.get_unread_messages(max_count=10)
        meetings = self.get_todays_meetings()
        msg_summary = self.summarize_messages_for_jarvis(messages)
        meet_summary = self.summarize_meetings_for_jarvis(meetings)
        return f"{msg_summary} {meet_summary}"
