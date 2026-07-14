#!/bin/bash
set -e
JARVIS_DIR=~/Jarvis

cat > "$JARVIS_DIR/jarvis_outlook_email.py" << 'PYEOF'
import subprocess, logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.outlook_email")

@dataclass
class OutlookEmail:
    subject: str
    sender: str
    received: str
    body_preview: str
    is_unread: bool
    account: str

class OutlookEmailReader:
    def _run(self, script):
        try:
            r = subprocess.run(["osascript","-e",script], capture_output=True, text=True, timeout=15)
            return r.stdout.strip() if r.returncode == 0 else None
        except: return None

    def _ensure_running(self):
        self._run('tell application "Microsoft Outlook" to activate')

    def get_unread(self, max_count=10):
        self._ensure_running()
        script = f'''tell application "Microsoft Outlook"
    set unreadMessages to {{}}
    repeat with msg in messages of inbox
        if is read of msg is false then set end of unreadMessages to msg
        if (count of unreadMessages) >= {max_count} then exit repeat
    end repeat
    set output to ""
    repeat with msg in unreadMessages
        set previewLen to min of 300 and (count characters of plain text content of msg)
        set output to output & "---EMAIL---" & return
        set output to output & "Subject: " & subject of msg & return
        set output to output & "From: " & email address of sender of msg & return
        set output to output & "Received: " & (time received of msg as string) & return
        set output to output & "Preview: " & text 1 thru previewLen of plain text content of msg & return
    end repeat
    return output
end tell'''
        return self._parse_emails(self._run(script))

    def get_recent(self, max_count=10):
        self._ensure_running()
        script = f'''tell application "Microsoft Outlook"
    set output to ""
    set counter to 0
    repeat with msg in messages of inbox
        if counter >= {max_count} then exit repeat
        set previewLen to min of 300 and (count characters of plain text content of msg)
        set output to output & "---EMAIL---" & return
        set output to output & "Subject: " & subject of msg & return
        set output to output & "From: " & email address of sender of msg & return
        set output to output & "Received: " & (time received of msg as string) & return
        set output to output & "Read: " & (is read of msg) & return
        set output to output & "Preview: " & text 1 thru previewLen of plain text content of msg & return
        set counter to counter + 1
    end repeat
    return output
end tell'''
        return self._parse_emails(self._run(script))

    def search(self, keyword, max_count=5):
        self._ensure_running()
        script = f'''tell application "Microsoft Outlook"
    set kw to "{keyword.lower()}"
    set output to ""
    set counter to 0
    repeat with msg in messages of inbox
        if counter >= {max_count} then exit repeat
        set msgSubject to (subject of msg) as string
        set msgBody to (plain text content of msg) as string
        if (msgSubject contains kw) or (msgBody contains kw) then
            set previewLen to min of 300 and (count characters of msgBody)
            set output to output & "---EMAIL---" & return
            set output to output & "Subject: " & msgSubject & return
            set output to output & "From: " & email address of sender of msg & return
            set output to output & "Received: " & (time received of msg as string) & return
            set output to output & "Preview: " & text 1 thru previewLen of msgBody & return
            set counter to counter + 1
        end if
    end repeat
    if output is "" then return "NO_RESULTS"
    return output
end tell'''
        raw = self._run(script)
        return [] if raw == "NO_RESULTS" else self._parse_emails(raw)

    def get_unread_count(self):
        self._ensure_running()
        script = '''tell application "Microsoft Outlook"
    set c to 0
    repeat with msg in messages of inbox
        if is read of msg is false then set c to c + 1
    end repeat
    return c
end tell'''
        try: return int(self._run(script))
        except: return 0

    def _parse_emails(self, raw):
        if not raw: return []
        emails = []
        for block in raw.split("---EMAIL---"):
            block = block.strip()
            if not block: continue
            fields = {}
            for line in block.splitlines():
                if ": " in line:
                    k,_,v = line.partition(": ")
                    fields[k.strip()] = v.strip()
            if "Subject" in fields:
                emails.append(OutlookEmail(
                    subject=fields.get("Subject","(no subject)"),
                    sender=fields.get("From","Unknown"),
                    received=fields.get("Received",""),
                    body_preview=fields.get("Preview",""),
                    is_unread=fields.get("Read","false").lower()=="false",
                    account="Outlook"))
        return emails

    def summarize_for_jarvis(self, emails):
        if not emails: return "No emails found."
        lines = [f"You have {len(emails)} email(s):\n"]
        for i,e in enumerate(emails,1):
            status = "UNREAD" if e.is_unread else "read"
            lines.append(f"{i}. [{status}] From: {e.sender}\n   Subject: {e.subject}\n   Received: {e.received}\n   Preview: {e.body_preview[:200]}\n")
        return "\n".join(lines)
PYEOF

echo "✅ jarvis_outlook_email.py created"

cat > "$JARVIS_DIR/jarvis_outlook_calendar.py" << 'PYEOF'
import subprocess, logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger("jarvis.outlook_calendar")

@dataclass
class OutlookEvent:
    subject: str
    start: str
    end: str
    location: str
    organizer: str
    is_all_day: bool
    body_preview: str

class OutlookCalendarReader:
    def _run(self, script):
        try:
            r = subprocess.run(["osascript","-e",script], capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None
        except: return None

    def _ensure_running(self):
        self._run('tell application "Microsoft Outlook" to activate')

    def get_today(self):
        self._ensure_running()
        today = date.today().strftime("%B %d, %Y")
        script = f'''tell application "Microsoft Outlook"
    set targetDate to date "{today}"
    set endOfDay to targetDate + (23 * hours) + (59 * minutes) + 59
    set matchingEvents to calendar events whose start time >= targetDate and start time <= endOfDay
    set output to ""
    repeat with evt in matchingEvents
        set evtOrganizer to ""
        try
            set evtOrganizer to email address of organizer of evt
        end try
        set evtPreview to ""
        try
            set evtPreview to text 1 thru (min of 200 and (count characters of plain text content of evt)) of plain text content of evt
        end try
        set output to output & "---EVENT---" & return
        set output to output & "Subject: " & subject of evt & return
        set output to output & "Start: " & (start time of evt as string) & return
        set output to output & "End: " & (end time of evt as string) & return
        set output to output & "Location: " & location of evt & return
        set output to output & "AllDay: " & (is all day event of evt) & return
        set output to output & "Organizer: " & evtOrganizer & return
        set output to output & "Notes: " & evtPreview & return
    end repeat
    if output is "" then return "NO_EVENTS"
    return output
end tell'''
        raw = self._run(script)
        return [] if raw == "NO_EVENTS" else self._parse_events(raw)

    def get_upcoming(self, days=7):
        self._ensure_running()
        today = date.today().strftime("%B %d, %Y")
        future = (date.today() + timedelta(days=days)).strftime("%B %d, %Y")
        script = f'''tell application "Microsoft Outlook"
    set startDate to date "{today}"
    set endDate to date "{future}"
    set matchingEvents to calendar events whose start time >= startDate and start time <= endDate
    set output to ""
    repeat with evt in matchingEvents
        set evtOrganizer to ""
        try
            set evtOrganizer to email address of organizer of evt
        end try
        set output to output & "---EVENT---" & return
        set output to output & "Subject: " & subject of evt & return
        set output to output & "Start: " & (start time of evt as string) & return
        set output to output & "End: " & (end time of evt as string) & return
        set output to output & "Location: " & location of evt & return
        set output to output & "AllDay: " & (is all day event of evt) & return
        set output to output & "Organizer: " & evtOrganizer & return
    end repeat
    if output is "" then return "NO_EVENTS"
    return output
end tell'''
        raw = self._run(script)
        return [] if raw == "NO_EVENTS" else self._parse_events(raw)

    def _parse_events(self, raw):
        if not raw: return []
        events = []
        for block in raw.split("---EVENT---"):
            block = block.strip()
            if not block: continue
            fields = {}
            for line in block.splitlines():
                if ": " in line:
                    k,_,v = line.partition(": ")
                    fields[k.strip()] = v.strip()
            if "Subject" in fields:
                events.append(OutlookEvent(
                    subject=fields.get("Subject","(no subject)"),
                    start=fields.get("Start",""),
                    end=fields.get("End",""),
                    location=fields.get("Location",""),
                    organizer=fields.get("Organizer",""),
                    is_all_day=fields.get("AllDay","false").lower()=="true",
                    body_preview=fields.get("Notes","")))
        return events

    def summarize_for_jarvis(self, events):
        if not events: return "No calendar events found."
        lines = [f"You have {len(events)} calendar event(s):\n"]
        for i,e in enumerate(events,1):
            day_tag = " (All Day)" if e.is_all_day else ""
            loc = f" @ {e.location}" if e.location else ""
            org = f" (Organizer: {e.organizer})" if e.organizer else ""
            lines.append(f"{i}. {e.subject}{day_tag}\n   {e.start} → {e.end}{loc}{org}\n")
        return "\n".join(lines)
PYEOF

echo "✅ jarvis_outlook_calendar.py created"

echo ""
echo "🔍 Testing Outlook connection..."
TEST=$(osascript -e 'tell application "Microsoft Outlook" to return name' 2>&1)
echo "   Outlook says: $TEST"

echo ""
echo "📬 Checking unread count..."
UNREAD=$(osascript -e 'tell application "Microsoft Outlook"
    set c to 0
    repeat with msg in messages of inbox
        if is read of msg is false then set c to c + 1
    end repeat
    return c
end tell' 2>&1)
echo "   Unread emails: $UNREAD"

echo ""
echo "📅 Checking today's calendar events..."
TODAY=$(date "+%B %d, %Y")
CAL=$(osascript -e "tell application \"Microsoft Outlook\"
    set targetDate to date \"$TODAY\"
    set endOfDay to targetDate + (23 * hours) + (59 * minutes) + 59
    set matchingEvents to calendar events whose start time >= targetDate and start time <= endOfDay
    return count of matchingEvents
end tell" 2>&1)
echo "   Events today: $CAL"

echo ""
echo "=============================================="
echo "  ✅ All done! Files created in ~/Jarvis/"
echo "  Share the output above and next steps"
echo "  will be to patch actions.py and server.py"
echo "=============================================="
