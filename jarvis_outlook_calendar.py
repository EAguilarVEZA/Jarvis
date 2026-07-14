
import subprocess
import logging
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

    def _run(self, script: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                logger.warning(f"AppleScript error: {result.stderr.strip()}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("AppleScript timed out")
            return None
        except Exception as e:
            logger.error(f"AppleScript failed: {e}")
            return None

    def get_today(self) -> list:
        today = date.today().strftime("%B %d, %Y")
        script = f"""
tell application "Microsoft Outlook"
    set targetDate to date "{today}"
    set endOfDay to targetDate + (23 * hours) + (59 * minutes) + 59
    set output to ""

    -- Check all calendars across all accounts
    repeat with cal in calendars
        try
            set calEvents to (calendar events of cal) whose start time >= targetDate and start time <= endOfDay
            repeat with evt in calEvents
                set evtOrganizer to ""
                try
                    set evtOrganizer to email address of organizer of evt
                end try
                set evtPreview to ""
                try
                    set evtPreview to text 1 thru (min of 150 and (count characters of plain text content of evt)) of plain text content of evt
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
        end try
    end repeat

    if output is "" then return "NO_EVENTS"
    return output
end tell
"""
        raw = self._run(script)
        if not raw or raw == "NO_EVENTS":
            return []
        return self._parse_events(raw)

    def get_upcoming(self, days: int = 7) -> list:
        today = date.today().strftime("%B %d, %Y")
        future = (date.today() + timedelta(days=days)).strftime("%B %d, %Y")
        script = f"""
tell application "Microsoft Outlook"
    set startDate to date "{today}"
    set endDate to date "{future}"
    set output to ""
    repeat with cal in calendars
        try
            set calEvents to (calendar events of cal) whose start time >= startDate and start time <= endDate
            repeat with evt in calEvents
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
        end try
    end repeat
    if output is "" then return "NO_EVENTS"
    return output
end tell
"""
        raw = self._run(script)
        if not raw or raw == "NO_EVENTS":
            return []
        return self._parse_events(raw)

    def _parse_events(self, raw: Optional[str]) -> list:
        if not raw:
            return []
        events = []
        for block in raw.split("---EVENT---"):
            block = block.strip()
            if not block:
                continue
            fields = {}
            for line in block.splitlines():
                if ": " in line:
                    k, _, v = line.partition(": ")
                    fields[k.strip()] = v.strip()
            if "Subject" in fields:
                events.append(OutlookEvent(
                    subject=fields.get("Subject", "(no subject)"),
                    start=fields.get("Start", ""),
                    end=fields.get("End", ""),
                    location=fields.get("Location", ""),
                    organizer=fields.get("Organizer", ""),
                    is_all_day=fields.get("AllDay", "false").lower() == "true",
                    body_preview=fields.get("Notes", ""),
                ))
        logger.info(f"Parsed {len(events)} Outlook calendar events")
        return events

    def summarize_for_jarvis(self, events: list) -> str:
        if not events:
            return "Your Outlook calendar is clear today, sir. No events scheduled."
        lines = [f"You have {len(events)} event(s) on your Outlook calendar today, sir."]
        for i, e in enumerate(events, 1):
            day_tag = " — all day" if e.is_all_day else f" from {e.start} to {e.end}"
            loc = f" at {e.location}" if e.location else ""
            org = f", organised by {e.organizer}" if e.organizer else ""
            lines.append(f"{i}. {e.subject}{day_tag}{loc}{org}.")
        return " ".join(lines)
