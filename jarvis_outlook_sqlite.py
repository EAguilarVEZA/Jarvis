"""
jarvis_outlook_sqlite.py
Reads Outlook for Mac emails directly from local HxStore.hxd file.
"""
import re, os, logging
from dataclasses import dataclass

logger = logging.getLogger("jarvis.outlook_sqlite")

HX_PATH = os.path.expanduser(
    "~/Library/Group Containers/UBF8T346G9.Office/Outlook/"
    "Outlook 15 Profiles/Main Profile/HxStore.hxd"
)

@dataclass
class LocalEmail:
    subject: str
    sender: str
    preview: str
    received: str
    is_unread: bool

class OutlookLocalReader:

    def get_recent_subjects(self, max_count=15) -> list[str]:
        if not os.path.exists(HX_PATH):
            return []
        try:
            with open(HX_PATH, 'rb') as f:
                data = f.read()

            # UTF-16-LE gives the cleanest results
            text = data.decode('utf-16-le', errors='ignore')

            # Extract subject lines - they appear as Re:/FW:/RE: followed by text
            raw = re.findall(
                r'(?:Re|RE|Fw|FW|Fwd):\s{0,3}([A-Za-z0-9][^\x00-\x1f\x7f-\xff]{3,70})',
                text
            )

            # Clean and deduplicate
            seen = set()
            subjects = []
            for s in raw:
                # Remove noise characters
                clean = re.sub(r'[^\x20-\x7E]', '', s).strip()
                # Remove trailing junk (single chars, numbers)
                clean = re.sub(r'[\s\W]+$', '', clean).strip()
                if len(clean) > 5 and clean not in seen:
                    # Reconstruct full subject
                    prefix_match = re.search(
                        r'((?:Re|RE|Fw|FW|Fwd):\s*)' + re.escape(s[:10]),
                        text
                    )
                    prefix = "Re: " if "Re" in s else "FW: " if "FW" in s or "Fw" in s else ""
                    full = prefix + clean
                    seen.add(clean)
                    subjects.append(full)
                    if len(subjects) >= max_count:
                        break

            # Also grab ASCII subjects
            ascii_strings = re.findall(b'[\x20-\x7E]{20,100}', data)
            for s in ascii_strings:
                text_s = s.decode('ascii', errors='ignore').strip()
                if re.match(r'^(Re|RE|Fw|FW|Fwd):\s+[A-Za-z]', text_s):
                    clean = re.sub(r'[^A-Za-z0-9 :&\-_().,\'"/]+', '', text_s).strip()
                    if len(clean) > 10 and clean not in seen:
                        seen.add(clean)
                        subjects.append(clean)
                        if len(subjects) >= max_count:
                            break

            return subjects[:max_count]
        except Exception as e:
            logger.error(f"HxStore read error: {e}")
            return []

    def get_unread_emails(self, max_count=10) -> list[LocalEmail]:
        subjects = self.get_recent_subjects(max_count)
        return [LocalEmail(
            subject=s,
            sender="Orlando Health",
            preview="",
            received="Recent",
            is_unread=True
        ) for s in subjects]

    def summarize_for_jarvis(self, emails=None) -> str:
        subjects = self.get_recent_subjects(10)
        if not subjects:
            return "Could not read local Outlook email data, sir."
        lines = [f"Your Outlook has {len(subjects)} recent email threads, sir."]
        for i, s in enumerate(subjects[:7], 1):
            lines.append(f"{i}. {s}")
        return " ".join(lines)

    def get_todays_emails_summary(self) -> str:
        return self.summarize_for_jarvis()
