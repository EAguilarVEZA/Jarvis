
import subprocess
import logging
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

    def get_unread_count(self) -> int:
        script = """
tell application "Microsoft Outlook"
    set c to 0
    repeat with acct in exchange accounts
        try
            set theInbox to inbox of acct
            repeat with msg in messages of theInbox
                if is read of msg is false then set c to c + 1
            end repeat
        end try
    end repeat
    -- also check default inbox
    try
        repeat with msg in messages of inbox
            if is read of msg is false then set c to c + 1
        end repeat
    end try
    return c
end tell
"""
        try:
            return int(self._run(script) or "0")
        except (TypeError, ValueError):
            return 0

    def get_unread(self, max_count: int = 10) -> list:
        script = f"""
tell application "Microsoft Outlook"
    set output to ""
    set counter to 0

    -- Check all exchange accounts first
    repeat with acct in exchange accounts
        try
            set theInbox to inbox of acct
            set acctName to email address of acct
            repeat with msg in messages of theInbox
                if counter >= {max_count} then exit repeat
                if is read of msg is false then
                    set msgSubject to subject of msg
                    set msgSender to email address of sender of msg
                    set msgTime to time received of msg as string
                    set previewLen to min of 200 and (count characters of plain text content of msg)
                    set msgPreview to text 1 thru previewLen of plain text content of msg
                    set output to output & "---EMAIL---" & return
                    set output to output & "Subject: " & msgSubject & return
                    set output to output & "From: " & msgSender & return
                    set output to output & "Received: " & msgTime & return
                    set output to output & "Account: " & acctName & return
                    set output to output & "Preview: " & msgPreview & return
                    set counter to counter + 1
                end if
            end repeat
        end try
    end repeat

    -- Also check default inbox as fallback
    if counter < {max_count} then
        try
            repeat with msg in messages of inbox
                if counter >= {max_count} then exit repeat
                if is read of msg is false then
                    set msgSubject to subject of msg
                    set msgSender to email address of sender of msg
                    set msgTime to time received of msg as string
                    set previewLen to min of 200 and (count characters of plain text content of msg)
                    set msgPreview to text 1 thru previewLen of plain text content of msg
                    set output to output & "---EMAIL---" & return
                    set output to output & "Subject: " & msgSubject & return
                    set output to output & "From: " & msgSender & return
                    set output to output & "Received: " & msgTime & return
                    set output to output & "Account: default" & return
                    set output to output & "Preview: " & msgPreview & return
                    set counter to counter + 1
                end if
            end repeat
        end try
    end if

    if output is "" then return "NO_RESULTS"
    return output
end tell
"""
        raw = self._run(script)
        if not raw or raw == "NO_RESULTS":
            return []
        return self._parse_emails(raw)

    def get_recent(self, max_count: int = 10) -> list:
        script = f"""
tell application "Microsoft Outlook"
    set output to ""
    set counter to 0
    repeat with acct in exchange accounts
        try
            set theInbox to inbox of acct
            repeat with msg in messages of theInbox
                if counter >= {max_count} then exit repeat
                set msgSubject to subject of msg
                set msgSender to email address of sender of msg
                set msgTime to time received of msg as string
                set msgRead to is read of msg
                set previewLen to min of 200 and (count characters of plain text content of msg)
                set msgPreview to text 1 thru previewLen of plain text content of msg
                set output to output & "---EMAIL---" & return
                set output to output & "Subject: " & msgSubject & return
                set output to output & "From: " & msgSender & return
                set output to output & "Received: " & msgTime & return
                set output to output & "Read: " & msgRead & return
                set output to output & "Preview: " & msgPreview & return
                set counter to counter + 1
            end repeat
        end try
    end repeat
    if output is "" then return "NO_RESULTS"
    return output
end tell
"""
        raw = self._run(script)
        if not raw or raw == "NO_RESULTS":
            return []
        return self._parse_emails(raw)

    def search(self, keyword: str, max_count: int = 5) -> list:
        script = f"""
tell application "Microsoft Outlook"
    set kw to "{keyword.lower()}"
    set output to ""
    set counter to 0
    repeat with acct in exchange accounts
        try
            set theInbox to inbox of acct
            repeat with msg in messages of theInbox
                if counter >= {max_count} then exit repeat
                set msgSubject to (subject of msg) as string
                set msgBody to (plain text content of msg) as string
                if (msgSubject contains kw) or (msgBody contains kw) then
                    set previewLen to min of 200 and (count characters of msgBody)
                    set output to output & "---EMAIL---" & return
                    set output to output & "Subject: " & msgSubject & return
                    set output to output & "From: " & email address of sender of msg & return
                    set output to output & "Received: " & (time received of msg as string) & return
                    set output to output & "Preview: " & text 1 thru previewLen of msgBody & return
                    set counter to counter + 1
                end if
            end repeat
        end try
    end repeat
    if output is "" then return "NO_RESULTS"
    return output
end tell
"""
        raw = self._run(script)
        if not raw or raw == "NO_RESULTS":
            return []
        return self._parse_emails(raw)

    def _parse_emails(self, raw: Optional[str]) -> list:
        if not raw:
            return []
        emails = []
        for block in raw.split("---EMAIL---"):
            block = block.strip()
            if not block:
                continue
            fields = {}
            for line in block.splitlines():
                if ": " in line:
                    k, _, v = line.partition(": ")
                    fields[k.strip()] = v.strip()
            if "Subject" in fields:
                emails.append(OutlookEmail(
                    subject=fields.get("Subject", "(no subject)"),
                    sender=fields.get("From", "Unknown"),
                    received=fields.get("Received", ""),
                    body_preview=fields.get("Preview", ""),
                    is_unread=fields.get("Read", "false").lower() == "false",
                    account=fields.get("Account", "Outlook"),
                ))
        logger.info(f"Parsed {len(emails)} emails from Outlook")
        return emails

    def summarize_for_jarvis(self, emails: list) -> str:
        if not emails:
            return "Your Outlook inbox is clear, sir. No unread messages."
        lines = [f"You have {len(emails)} unread email(s) in Outlook, sir."]
        for i, e in enumerate(emails, 1):
            lines.append(
                f"{i}. From {e.sender} — Subject: {e.subject}. "
                f"Received {e.received}. {e.body_preview[:150]}"
            )
        return " ".join(lines)
