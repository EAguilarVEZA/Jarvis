"""
jarvis_copilot.py
-----------------
Drives Microsoft Copilot (copilot.microsoft.com) via Chrome AppleScript
to answer email and calendar queries on behalf of Jarvis.
"""

import subprocess
import logging
import time
import json
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.copilot")

COPILOT_URLS = [
    "copilot.microsoft.com",
    "m365.cloud.microsoft/chat",
]

RESPONSE_WAIT = 18
POLL_INTERVAL = 1.5


@dataclass
class CopilotResponse:
    query: str
    answer: str
    success: bool
    source_url: str = ""


class CopilotClient:

    def _run_js(self, js: str, url_hint: str) -> Optional[str]:
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
                capture_output=True, text=True, timeout=20
            )
            out = result.stdout.strip()
            if out == "NO_TAB":
                return None
            return out
        except Exception as e:
            logger.error(f"JS execution failed: {e}")
            return None

    def _open_url(self, url: str):
        script = f'''
tell application "Google Chrome"
    activate
    open location "https://{url}"
end tell
'''
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)

    def _find_active_url(self) -> Optional[str]:
        for url_hint in COPILOT_URLS:
            result = self._run_js("return document.title", url_hint)
            if result is not None:
                logger.info(f"Found Copilot tab at: {url_hint}")
                return url_hint
        return None

    def ensure_open(self) -> Optional[str]:
        url_hint = self._find_active_url()
        if url_hint:
            return url_hint
        logger.info("No Copilot tab found, opening copilot.microsoft.com")
        self._open_url(COPILOT_URLS[0])
        time.sleep(5)
        url_hint = self._find_active_url()
        if not url_hint:
            logger.error("Could not open Copilot tab")
        return url_hint

    def _clear_and_type(self, url_hint: str, query: str) -> bool:
        escaped_query = query.replace("'", "\\'").replace('"', '\\"')
        js = f"""
(function() {{
    var box = document.querySelector('textarea[placeholder]') ||
              document.querySelector('[contenteditable="true"]') ||
              document.querySelector('[role="textbox"]') ||
              document.querySelector('textarea') ||
              document.querySelector('[aria-label*="message" i]') ||
              document.querySelector('[aria-label*="Ask" i]') ||
              document.querySelector('[aria-label*="chat" i]');
    if (!box) return 'INPUT_NOT_FOUND';
    box.focus();
    if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') {{
        box.value = '';
        box.dispatchEvent(new Event('input', {{bubbles: true}}));
        box.value = '{escaped_query}';
        box.dispatchEvent(new Event('input', {{bubbles: true}}));
        box.dispatchEvent(new Event('change', {{bubbles: true}}));
    }} else {{
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        document.execCommand('insertText', false, '{escaped_query}');
        box.dispatchEvent(new Event('input', {{bubbles: true}}));
    }}
    return 'TYPED';
}})()
"""
        result = self._run_js(js, url_hint)
        if result != "TYPED":
            logger.warning(f"Could not type into Copilot input: {result}")
            return False
        return True

    def _submit_query(self, url_hint: str) -> bool:
        js = """
(function() {
    var sendBtn = document.querySelector('button[type="submit"]') ||
                  document.querySelector('[aria-label*="Send" i]') ||
                  document.querySelector('[aria-label*="submit" i]') ||
                  document.querySelector('[data-testid*="send"]') ||
                  document.querySelector('[class*="send"][role="button"]');
    if (sendBtn && !sendBtn.disabled) {
        sendBtn.click();
        return 'CLICKED_SEND';
    }
    var box = document.querySelector('textarea') ||
              document.querySelector('[contenteditable="true"]') ||
              document.querySelector('[role="textbox"]');
    if (box) {
        var evt = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', keyCode: 13,
            bubbles: true, cancelable: true
        });
        box.dispatchEvent(evt);
        return 'PRESSED_ENTER';
    }
    return 'SUBMIT_FAILED';
})()
"""
        result = self._run_js(js, url_hint)
        logger.info(f"Submit result: {result}")
        return result in ("CLICKED_SEND", "PRESSED_ENTER")

    def _wait_for_response(self, url_hint: str) -> Optional[str]:
        logger.info(f"Waiting up to {RESPONSE_WAIT}s for Copilot response...")
        last_text = ""
        stable_count = 0
        elapsed = 0

        while elapsed < RESPONSE_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            js = """
(function() {
    var stopBtn = document.querySelector('[aria-label*="Stop" i]') ||
                  document.querySelector('[aria-label*="stop generating" i]') ||
                  document.querySelector('[data-testid*="stop"]');
    var isGenerating = stopBtn !== null;

    var messages = document.querySelectorAll('[class*="message"][class*="assistant" i]');
    if (!messages.length) messages = document.querySelectorAll('[data-testid*="response"]');
    if (!messages.length) messages = document.querySelectorAll('[class*="response"]');
    if (!messages.length) messages = document.querySelectorAll('[class*="bot-message"]');
    if (!messages.length) {
        var allMsgs = document.querySelectorAll('[class*="message"]');
        if (allMsgs.length) {
            messages = Array.from(allMsgs).filter(function(m) {
                var cls = m.className || '';
                return !cls.includes('user') && !cls.includes('human') && !cls.includes('input');
            });
        }
    }

    var lastText = '';
    if (messages && messages.length > 0) {
        lastText = messages[messages.length - 1].innerText || '';
        lastText = lastText.trim();
    }

    return JSON.stringify({
        generating: isGenerating,
        text: lastText.substring(0, 3000)
    });
})()
"""
            raw = self._run_js(js, url_hint)
            if not raw:
                continue

            try:
                data = json.loads(raw)
                current_text = data.get("text", "")
                is_generating = data.get("generating", True)

                if current_text and current_text == last_text:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_text = current_text

                if not is_generating and stable_count >= 2 and last_text:
                    logger.info(f"Copilot response ready ({len(last_text)} chars)")
                    return last_text

                if stable_count >= 4 and last_text:
                    logger.info(f"Copilot response stable ({len(last_text)} chars)")
                    return last_text

            except Exception as e:
                logger.warning(f"Poll parse error: {e}")
                continue

        if last_text:
            logger.warning("Copilot response timed out, returning partial text")
            return last_text

        logger.error("Copilot response timed out with no text")
        return None

    def ask(self, query: str) -> CopilotResponse:
        url_hint = self.ensure_open()
        if not url_hint:
            return CopilotResponse(
                query=query,
                answer="Could not open Copilot, sir. Please ensure you are signed into copilot.microsoft.com in Chrome.",
                success=False,
            )

        time.sleep(1)

        if not self._clear_and_type(url_hint, query):
            return CopilotResponse(
                query=query,
                answer="Could not find the Copilot input box, sir.",
                success=False,
                source_url=url_hint,
            )

        time.sleep(0.5)

        if not self._submit_query(url_hint):
            return CopilotResponse(
                query=query,
                answer="Could not submit the query to Copilot, sir.",
                success=False,
                source_url=url_hint,
            )

        answer = self._wait_for_response(url_hint)
        if not answer:
            return CopilotResponse(
                query=query,
                answer="Copilot did not respond in time, sir.",
                success=False,
                source_url=url_hint,
            )

        return CopilotResponse(
            query=query,
            answer=answer,
            success=True,
            source_url=url_hint,
        )

    def get_unread_emails(self, max_count: int = 10) -> CopilotResponse:
        return self.ask(f"Please check my Outlook inbox and list my {max_count} most recent unread emails. For each one include: sender name, subject, and a one-sentence summary. Be concise.")

    def get_todays_calendar(self) -> CopilotResponse:
        return self.ask("Please check my Outlook calendar and list all my events for today. For each event include: time, title, and location if available. Be concise.")

    def get_daily_briefing(self) -> CopilotResponse:
        return self.ask("Give me a brief morning briefing covering: 1) My calendar events for today (time and title), 2) Any urgent or important unread emails (sender and subject). Keep it concise.")

    def search_emails(self, keyword: str) -> CopilotResponse:
        return self.ask(f"Search my Outlook inbox for emails related to '{keyword}'. List the most relevant ones with sender, subject, and date. Be concise.")

    def get_upcoming_meetings(self, days: int = 3) -> CopilotResponse:
        return self.ask(f"What meetings or calendar events do I have in the next {days} days? List them with date, time, and title. Be concise.")

    def summarize_for_jarvis(self, response: CopilotResponse) -> str:
        if not response.success:
            return response.answer
        import re
        text = response.answer
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'^#{1,3}\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*[-•]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '. ', text)
        text = re.sub(r'\n', ', ', text)
        text = re.sub(r'\s{2,}', ' ', text)
        text = text.strip()
        if len(text) > 600:
            text = text[:600].rsplit('.', 1)[0] + '.'
        return text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = CopilotClient()
    print("Testing Copilot daily briefing...")
    response = client.get_daily_briefing()
    print(f"\nSuccess: {response.success}")
    print(f"Source: {response.source_url}")
    print(f"\nRaw answer:\n{response.answer}")
    print(f"\nVoice-ready:\n{client.summarize_for_jarvis(response)}")
