"""
jarvis_billing.py
-----------------
Scrapes the Anthropic credit balance from platform.claude.com/settings/billing
using Chrome AppleScript — same pattern as jarvis_outlook_web.py.

Used by Jarvis to report balance before and after every Claude Code task.
"""

import subprocess
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.billing")

BILLING_URL = "platform.claude.com/settings/billing"


@dataclass
class BillingInfo:
    balance: float
    balance_str: str
    success: bool
    warning: bool  # True if balance < threshold


class BillingChecker:

    LOW_BALANCE_THRESHOLD = 5.00   # Warn below $5
    CRITICAL_THRESHOLD    = 2.00   # Critical below $2

    def _run_js(self, js: str) -> Optional[str]:
        """Inject JS into the platform.claude.com billing tab."""
        escaped = (
            js
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
        )
        script = f'''
tell application "Google Chrome"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "{BILLING_URL}" then
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
                return None
            return out
        except Exception as e:
            logger.error(f"Billing JS failed: {e}")
            return None

    def _open_billing_page(self):
        """Open billing page in Chrome."""
        script = '''
tell application "Google Chrome"
    set found to false
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "platform.claude.com/settings/billing" then
                set found to true
            end if
        end repeat
    end repeat
    if not found then
        make new tab at end of tabs of window 1
        set URL of active tab of window 1 to "https://platform.claude.com/settings/billing"
    end if
end tell
'''
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        import time
        time.sleep(4)

    def get_balance(self) -> BillingInfo:
        """Scrape current credit balance from billing page."""

        # Try scraping existing tab first
        js = """
(function() {
    var text = document.body.innerText;

    // Look for dollar amount near "Remaining balance"
    var match = text.match(/\\$([\\d,]+\\.\\d{2})\\s*\\n?\\s*Remaining balance/);
    if (match) return match[1];

    // Fallback: find first dollar amount on page
    var allMatches = text.match(/\\$([\\d,]+\\.\\d{2})/g);
    if (allMatches && allMatches.length > 0) return allMatches[0].replace('$', '');

    return "NOT_FOUND";
})()
"""
        result = self._run_js(js)

        if not result or result == "NOT_FOUND":
            # Open the page and try again
            logger.info("Billing tab not found, opening it")
            self._open_billing_page()
            result = self._run_js(js)

        if not result or result == "NOT_FOUND":
            logger.error("Could not scrape balance")
            return BillingInfo(
                balance=0.0,
                balance_str="unknown",
                success=False,
                warning=True,
            )

        try:
            balance = float(result.replace(",", ""))
            warning = balance < self.LOW_BALANCE_THRESHOLD
            logger.info(f"Balance scraped: ${balance:.2f}")
            return BillingInfo(
                balance=balance,
                balance_str=f"${balance:.2f}",
                success=True,
                warning=warning,
            )
        except (ValueError, Exception) as e:
            logger.error(f"Balance parse error: {e} — raw: {result}")
            return BillingInfo(
                balance=0.0,
                balance_str="unknown",
                success=False,
                warning=True,
            )

    def format_for_jarvis(self, info: BillingInfo, context: str = "current") -> str:
        """Format balance as a Jarvis voice line."""
        if not info.success:
            return "I was unable to retrieve the API balance, sir. You may want to check manually."

        if context == "before":
            base = f"Current API balance is {info.balance_str}, sir."
            if info.balance < self.CRITICAL_THRESHOLD:
                return f"{base} I must warn you — that is critically low. We may not complete this task without adding funds."
            elif info.balance < self.LOW_BALANCE_THRESHOLD:
                return f"{base} That is getting low — you may want to top up soon."
            else:
                return f"{base} Proceeding with the task."

        elif context == "after":
            base = f"Task complete, sir. Remaining API balance is {info.balance_str}."
            if info.balance < self.CRITICAL_THRESHOLD:
                return f"{base} That is critically low — I'd strongly recommend adding funds before the next task."
            elif info.balance < self.LOW_BALANCE_THRESHOLD:
                return f"{base} Getting a bit thin — worth topping up soon."
            else:
                return base

        else:
            return f"API balance is {info.balance_str}, sir."


# Singleton
_billing = BillingChecker()


def get_balance() -> BillingInfo:
    return _billing.get_balance()


def format_for_jarvis(info: BillingInfo, context: str = "current") -> str:
    return _billing.format_for_jarvis(info, context)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    info = get_balance()
    print(f"Balance: {info.balance_str}")
    print(f"Warning: {info.warning}")
    print(f"Before: {format_for_jarvis(info, 'before')}")
    print(f"After:  {format_for_jarvis(info, 'after')}")
