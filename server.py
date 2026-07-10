"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from fastapi import UploadFile, File, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from actions import execute_action, monitor_build, open_terminal, open_browser, open_claude_in_project, _generate_project_name, prompt_existing_terminal
from work_mode import WorkSession, is_casual_question
from screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from mail_access import get_unread_count, get_unread_messages, get_recent_messages, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from reporting_module import router as reporting_router, detect_report_trigger, build_voice_summary
from semantic_api import router as semantic_router
from views_api import router as views_router
from briefs_api import router as briefs_router
from queries_api import router as queries_router
from reports_api import router as reports_router
from goals_api import router as goals_router
from connections_api import router as connections_router
from knowledge_api import router as knowledge_router
from hierarchies_api import router as hierarchies_router
from automations_api import router as automations_router
from users_api import router as users_router
from ask_history_api import router as ask_history_router
from airbyte_api import router as airbyte_router
from experiments_api import router as experiments_router
from assistant_api import router as assistant_router
from agents_api import router as agents_router
from workflows_api import router as workflows_router

# Phase 3 — interactive report builder
from reporting.catalog import WarehouseCatalog
from reporting.bq_client import BQClient as _BuilderBQClient
from reporting.builder_session import BuilderSession
from reporting.intent_extractor import is_report_trigger as is_builder_trigger

# Singleton catalog — loaded once at import. ~30MB inventory.json + curated.yaml
_builder_catalog = WarehouseCatalog.load()
_builder_bq = _BuilderBQClient()

# Phase 4 - Smart with Martin voice integration
from martin_browser import MartinBrowser, MartinError, SessionExpiredError
from martin_voice import (
    MartinSession,
    MartinPhase,
    is_martin_trigger,
    parse_martin_command,
    parse_followup_command,
    wait_for_response_complete as _martin_wait_for_response,
    extract_headlines as _martin_extract_headlines,
    extract_full_text as _martin_extract_full_text,
    format_headlines_for_voice as _martin_format_headlines,
    summarize_via_claude as _martin_summarize,
)

# Singleton browser - lazily started on first Martin command.
_martin_browser = None
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

try:
    from jarvis_outlook_sqlite import OutlookLocalReader
    _outlook_local = OutlookLocalReader()
    _OUTLOOK_LOCAL = True
except ImportError:
    _OUTLOOK_LOCAL = False

try:
    from jarvis_outlook_web import OutlookWebReader
    _outlook_web = OutlookWebReader()
    _OUTLOOK_WEB = True
except ImportError:
    _OUTLOOK_WEB = False

try:
    from jarvis_billing import get_balance, format_for_jarvis as format_balance
    _BILLING_AVAILABLE = True
except ImportError:
    _BILLING_AVAILABLE = False
    def get_balance(): return None
    def format_balance(info, context="current"): return ""

try:
    from jarvis_teams_web import TeamsWebReader
    _teams = TeamsWebReader()
    _TEAMS_AVAILABLE = True
except ImportError:
    _TEAMS_AVAILABLE = False

try:
    from jarvis_outlook_email import OutlookEmailReader
    from jarvis_outlook_calendar import OutlookCalendarReader
    _outlook_email = OutlookEmailReader()
    _outlook_calendar = OutlookCalendarReader()
    _OUTLOOK_AVAILABLE = True
except ImportError:
    _OUTLOOK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
FISH_API_URL = "https://api.fish.audio/v1/tts"
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humor is observational, never jokes: state facts and let implications land
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, Fish Audio TTS, Anthropic API). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Google Chrome and browse any URL or search query
- You CAN spawn Claude Code in a Terminal window for coding tasks
- You CAN create project folders on the Desktop
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- The Orlando Health BigQuery data warehouse is CONNECTED (oh-data-warehouse). You have live access to data_mart_cardiology, data_mart_cancer, data_mart_orthopedics, data_mart_womens, data_mart_neuroscience, data_mart_bariatrics, and ambulatory datasets. Never ask for credentials or say you don't have access — you do.
- You CAN read {user_name}'s calendar — today's events, upcoming meetings, schedule overview
- You CAN read {user_name}'s email (READ-ONLY) — unread count, recent messages, search by sender/subject. You CANNOT send, delete, or modify emails.
- You CAN read Apple Notes and create NEW notes — but you CANNOT edit or delete existing notes
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Chrome, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Claude Code is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): contains Settings, Restart Server, and Fix Yourself options
- **Settings panel**: Opens from the menu. Users can enter API keys (Anthropic, Fish Audio), test connections, set their name and preferences, and see system status (calendar, mail, notes connectivity). Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens Claude Code in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, sir."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = "Claude Code" or "Claude"
- "Travis" = "JARVIS"
- "clock code" = "Claude Code"

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. Claude Code does the work.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Chrome
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Claude Code will browse the web, find real listings/data, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Claude Code terminal with no specific project
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to Claude Code in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.

You use Claude Code as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Claude Code did X" or "Claude Code is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Claude Code is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: Optional[str] = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"})
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to a temp file so we can pipe it to claude
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt)

        # Open Terminal.app with claude running in the project directory
        applescript = f'''
        tell application "Terminal"
            activate
            set newTab to do script "cd {work_dir} && cat .jarvis_prompt.md | claude -p --dangerously-skip-permissions | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        '''

        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: anthropic.AsyncAnthropic) -> dict:
    """Classify every user message using Haiku LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                "- Open Terminal and run Claude Code (coding AI tool)\n"
                "- Open Chrome browser for web searches and URLs\n"
                "- Build software projects via Claude Code in Terminal\n"
                "- Research topics by opening Chrome search\n\n"
                "Note: speech-to-text may produce errors like \"Cloud\" for \"Claude\", "
                "\"Travis\" for \"JARVIS\", \"clock code\" for \"Claude Code\".\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "open_terminal = user wants to open terminal or launch Claude Code\n"
                "browse = user wants to search the web, look something up, visit a URL\n"
                "build = user wants to create/build a software project\n"
                "chat = just conversation, questions, or anything else\n"
                "If unclear, default to \"chat\"."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if not target.startswith("http") and ("." in target.split()[0]):
            target = "https://" + target
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research via claude -p in background. Opens report and speaks when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started via claude -p in {path}")

        process = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "text", "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=900,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = f"Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."})
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = project_name.replace('"', '\\"')
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Prepend BigQuery + Orlando Health context so Claude Code knows what it has access to
        BQ_CONTEXT = """You are operating as JARVIS's execution engine for Orlando Health.

ORLANDO HEALTH DATA WAREHOUSE - ALWAYS AVAILABLE:
- GCP Project ID: oh-data-warehouse
- Credentials: ~/.config/jarvis/bigquery-key.json (already configured, do not ask for it)
- Key datasets: data_mart_cancer, data_mart_cardiology, data_mart_orthopedics, data_mart_womens, data_mart_neuroscience, data_mart_bariatrics, ambulatory
- All data_mart tables have: digital_ads_w_leads with columns: date DATE, ad_source STRING, cost NUMERIC, leads INT64
- For Executive Health use: ambulatory.executive_health_program
- Use string date literals like '2026-01-01', never CURRENT_DATE()
- You have full read access. Use google.cloud.bigquery directly.

TASK:
"""
        enriched_prompt = BQ_CONTEXT + prompt

        # Run claude -p in background
        full_response = await dispatch.send(enriched_prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
        else:
            # Check for any HTML file created in the project dir and open it
            import glob
            html_files = sorted(glob.glob(f"{project_dir}/*.html"), key=lambda f: __import__('os').path.getmtime(f), reverse=True)
            if html_files:
                asyncio.create_task(_execute_browse(f"file://{html_files[0]}"))
                log.info(f"Auto-opening dashboard: {html_files[0]}")
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via Haiku — don't read word for word
            if anthropic_client:
                try:
                    summary = await anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Start with 'Sir, ' to get the user's attention. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. NEVER say 'Claude Code'. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                        messages=[{"role": "user", "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}"}],
                    )
                    msg = summary.content[0].text
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run claude -p in background and notify via voice when done."""

    # Wire up heartbeat so Jarvis speaks progress every 60s
    async def _heartbeat(msg: str, elapsed: int):
        try:
            minutes = elapsed // 60
            spoken = f"{msg} — {minutes} minute{'s' if minutes != 1 else ''} elapsed."
            audio = await synthesize_speech(spoken)
            if audio and ws:
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": spoken})
            log.info(f"Heartbeat ({elapsed}s): {spoken}")
        except Exception as e:
            log.debug(f"Heartbeat failed: {e}")

    session.set_heartbeat(_heartbeat)

    # ── Balance check BEFORE task ──
    if _BILLING_AVAILABLE and ws:
        try:
            import asyncio as _asyncio
            pre_info = await _asyncio.get_event_loop().run_in_executor(None, get_balance)
            pre_msg = format_balance(pre_info, "before")
            pre_audio = await synthesize_speech(pre_msg)
            if pre_audio:
                await ws.send_json({"type": "audio", "data": base64.b64encode(pre_audio).decode(), "text": pre_msg})
            log.info(f"JARVIS (billing before): {pre_msg}")
        except Exception as e:
            log.debug(f"Pre-task balance check failed: {e}")

    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if anthropic_client and full_response:
            try:
                summary = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never say 'Claude Code'.",
                    messages=[{"role": "user", "content": f"Claude Code completed:\n{full_response[:2000]}"}],
                )
                msg = summary.content[0].text
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass

        # ── Balance check AFTER task ──
        if _BILLING_AVAILABLE and ws:
            try:
                import asyncio as _asyncio
                post_info = await _asyncio.get_event_loop().run_in_executor(None, get_balance)
                post_msg = format_balance(post_info, "after")
                post_audio = await synthesize_speech(post_msg)
                if post_audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(post_audio).decode(), "text": post_msg})
                log.info(f"JARVIS (billing after): {post_msg}")
            except Exception as e:
                log.debug(f"Post-task balance check failed: {e}")

    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Fish Audio)
# ---------------------------------------------------------------------------

# ── Jarvis state tracking (for Martin overlay + future UI) ────────
# Updated at key transition points; exposed via /api/jarvis_state.
_jarvis_state = "idle"
# state ∈ {"idle", "listening", "thinking", "speaking", "waiting_on_martin"}
_jarvis_last_speech = ""
_jarvis_speech_at = 0.0


def _set_jarvis_state(state: str, last_speech: Optional[str] = None) -> None:
    """Update the current Jarvis state. Optionally set the most recent speech."""
    global _jarvis_state, _jarvis_last_speech, _jarvis_speech_at
    _jarvis_state = state
    if last_speech is not None:
        _jarvis_last_speech = last_speech
        _jarvis_speech_at = time.time()


async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio from text using Fish Audio TTS."""
    _set_jarvis_state("speaking", last_speech=text)
    if not FISH_API_KEY:
        log.warning("FISH_API_KEY not set, skipping TTS")
        _set_jarvis_state("idle")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers={
                    "Authorization": f"Bearer {FISH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "reference_id": FISH_VOICE_ID,
                    "format": "mp3",
                },
            )
            if response.status_code == 200:
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts")
                _set_jarvis_state("idle")
                return response.content
            else:
                log.error(f"TTS error: {response.status_code}")
                _set_jarvis_state("idle")
                return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        _set_jarvis_state("idle")
        return None


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using Anthropic API."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Brain — the compounding operating context (operating doc + latest morning
    # brief + open loops + last synthesis). This is what makes the assistant get
    # smarter every week instead of answering cold.
    try:
        from brain.context import build_brain_context
        brain_ctx = build_brain_context()
        if brain_ctx:
            system += ("\n\nJARVIS BRAIN — your accumulated operating context. "
                       "Answer grounded in this; it reflects who the user is and what's live:\n"
                       + brain_ctx)
    except Exception as _brain_exc:
        log.debug("brain context unavailable: %s", _brain_exc)

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Use conversation history — keep the last 20 messages for context
    # (older conversation is captured in session_summary)
    messages = conversation_history[-20:]
    # If the last message isn't the current user text, add it
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,  # Extra room for [ACTION:X] tags
            system=system,
            messages=messages,
        )
        track_usage(response)
        return response.content[0].text
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "Apologies, sir. I'm having trouble connecting to my language systems."


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
anthropic_client: Optional[anthropic.AsyncAnthropic] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * 0.80 + (output_t / 1_000_000) * 4.00


def track_usage(response):
    """Track token usage from an Anthropic API response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        ["osascript", "-e", '''
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
'''],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — detect location by IP, then fetch current conditions
            try:
                import urllib.request, json as _json
                # IP geolocation — detects current city dynamically (works in Atlanta, Orlando, etc.)
                with urllib.request.urlopen("http://ip-api.com/json/?fields=lat,lon,city,regionName", timeout=3) as geo_resp:
                    geo = _json.loads(geo_resp.read())
                lat = geo.get("lat", 33.749)
                lon = geo.get("lon", -84.388)
                city = geo.get("city", "Unknown")
                region = geo.get("regionName", "")
                location_name = f"{city}, {region}" if region else city
                url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    _ctx_cache["weather"] = f"Current weather in {location_name}: {temp}°F"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
def _bq_startup_refresh():
    """Test BigQuery at startup and log total leads — runs in thread."""
    import time, threading
    def _run():
        time.sleep(3)
        try:
            from reporting_module import DEMO_MODE
            if DEMO_MODE:
                return
            from google.cloud import bigquery
            import os
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                os.path.expanduser("~/.config/jarvis/bigquery-key.json"))
            bq = bigquery.Client(project="oh-data-warehouse")
            from datetime import datetime, timedelta
            since = (datetime.today() - timedelta(days=90)).strftime("%Y-%m-%d")
            total = 0
            datasets = ["data_mart_cardiology","data_mart_cancer","data_mart_orthopedics",
                        "data_mart_womens","data_mart_neuroscience","data_mart_bariatrics"]
            for ds in datasets:
                try:
                    rows = list(bq.query(
                        f"SELECT SUM(leads) as t FROM `oh-data-warehouse.{ds}.digital_ads_w_leads` WHERE date >= '{since}'"
                    ).result())
                    total += int(rows[0].t or 0)
                except Exception:
                    pass
            log.info(f"[OK] BigQuery — oh-data-warehouse connected")
            log.info(f"[OK] BigQuery — {total:,} total leads across all service lines (last 90 days)")
        except Exception as e:
            log.warning(f"[FAIL] BigQuery startup fetch failed: {e}")
    threading.Thread(target=_run, daemon=True).start()

async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — LLM features disabled")
    cached_projects = []

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()
    log.info("JARVIS server starting")
    log.info("=" * 50)
    log.info("SYSTEM STARTUP CHECKS")
    log.info("=" * 50)

    # Log BigQuery status and fetch startup summary in background thread
    try:
        from reporting_module import DEMO_MODE, PROJECT
        if not DEMO_MODE:
            log.info(f"[OK] BigQuery — credentials found, connecting...")
            _bq_startup_refresh()
        else:
            log.warning("[WARN] BigQuery — key not found, running in DEMO mode")
    except Exception as e:
        log.warning(f"BigQuery status check: {e}")

    # Start the brain operating loop — scheduled skills (morning brief, capture
    # processor, connection finder, weekly synthesis, belief tracker, pattern
    # detector, decision intelligence). Reads/writes the vault, compounds memory.
    try:
        from brain import runner as brain_runner
        brain_runner.start(lambda: anthropic_client)
        log.info("[OK] Brain operating loop started (7 scheduled skills)")
    except Exception as e:
        log.warning(f"Brain loop not started: {e}")

    yield

    # (shutdown cleanup would go here if needed)


# ─── Kaelio lockdown audit — runs once at process start ─────────────
# When KAELIO_LOCKDOWN=1 is set, this refuses to start the server if any
# Kaelio or PostHog domain is still reachable. In dev (env var unset), it
# logs an informational summary and continues.
try:
    from kaelio_audit import audit_or_die
    audit_or_die()
except Exception as _audit_exc:   # never let an audit-import bug stop the dev server
    import logging as _lg
    _lg.getLogger("kaelio_audit").warning("audit skipped: %s", _audit_exc)

app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)
app.include_router(reporting_router)
app.include_router(semantic_router)
app.include_router(views_router)
app.include_router(briefs_router)
app.include_router(queries_router)
app.include_router(reports_router)
app.include_router(goals_router)
app.include_router(connections_router)
app.include_router(knowledge_router)
app.include_router(hierarchies_router)
app.include_router(automations_router)
app.include_router(users_router)

# ─── SQL governance — row-level security ({{user.x}}) + policy enforcement ───
try:
    from sql_security import router as security_router
    app.include_router(security_router)
    log.info("[OK] SQL security (governance) mounted at /api/admin/security")
except Exception as _sec_mount_exc:
    log.warning(f"SQL security not mounted: {_sec_mount_exc}")

try:
    from sql_reviews import router as sql_reviews_router
    app.include_router(sql_reviews_router)
    log.info("[OK] SQL reviews mounted at /api/admin/sql-reviews")
except Exception as _rev_mount_exc:
    log.warning(f"SQL reviews not mounted: {_rev_mount_exc}")

try:
    from evaluations import router as evaluations_router
    app.include_router(evaluations_router)
    log.info("[OK] Evaluations mounted at /api/evaluations")
except Exception as _eval_mount_exc:
    log.warning(f"Evaluations not mounted: {_eval_mount_exc}")

try:
    from settings_api import router as settings_router
    app.include_router(settings_router)
    log.info("[OK] Settings mounted at /api/admin/settings")
except Exception as _set_mount_exc:
    log.warning(f"Settings not mounted: {_set_mount_exc}")

try:
    from jobs import router as jobs_router
    if jobs_router is not None:
        app.include_router(jobs_router)
        log.info("[OK] Jobs mounted at /api/admin/jobs")
except Exception as _jobs_mount_exc:
    log.warning(f"Jobs not mounted: {_jobs_mount_exc}")

# ─── Auth — login + sessions; opt-in gate for hosting (JARVIS_REQUIRE_AUTH) ───
try:
    import auth as _auth
    app.include_router(_auth.router)
    if _auth.REQUIRED:
        @app.middleware("http")
        async def _auth_gate(request, call_next):
            try:
                if _auth.is_public_path(request.url.path, request.method):
                    return await call_next(request)
                ident = _auth.identify(request)
                if not ident:
                    from fastapi.responses import JSONResponse as _JR
                    return _JR(status_code=401, content={"error": "authentication required"})
                # Trust the verified identity for ownership scoping — overwrite any
                # client-claimed X-Jarvis-User so it can't be spoofed once auth is on.
                if "@" in ident:
                    hdrs = [(k, v) for (k, v) in request.scope.get("headers", []) if k != b"x-jarvis-user"]
                    hdrs.append((b"x-jarvis-user", ident.encode()))
                    request.scope["headers"] = hdrs
                return await call_next(request)
            except Exception:
                return await call_next(request)
        log.info("[OK] Auth gate ENABLED (JARVIS_REQUIRE_AUTH) — /api/* requires a session or API key")
    else:
        log.info("[OK] Auth mounted at /api/auth (gate off — set JARVIS_REQUIRE_AUTH=1 to enforce)")
except Exception as _auth_mount_exc:
    log.warning(f"Auth not mounted: {_auth_mount_exc}")

# ─── Brain — the active operating loop (vault + scheduled skills + memory) ───
try:
    from brain.api import router as brain_router, set_client_getter as _brain_set_client
    app.include_router(brain_router)
    _brain_set_client(lambda: anthropic_client)
    log.info("[OK] Brain API mounted at /api/brain")
except Exception as _brain_mount_exc:
    log.warning(f"Brain API not mounted: {_brain_mount_exc}")
app.include_router(ask_history_router)
app.include_router(airbyte_router)
app.include_router(experiments_router)
app.include_router(assistant_router)
app.include_router(agents_router)
app.include_router(workflows_router)

# Scheduled email delivery of Designer reports (inert until SMTP is configured).
try:
    from report_email import router as report_email_router
    app.include_router(report_email_router)
    log.info("[OK] Report email schedules mounted at /api/reports/email")
except Exception as _re_exc:
    log.warning(f"Report email schedules not mounted: {_re_exc}")

# Metric threshold alerts (inert until SMTP is configured).
try:
    from alerts import router as alerts_router
    app.include_router(alerts_router)
    log.info("[OK] Metric alerts mounted at /api/alerts")
except Exception as _al_exc:
    log.warning(f"Metric alerts not mounted: {_al_exc}")

# Cross-cutting activity feed (comments + alert fires + email deliveries).
try:
    from activity_api import router as activity_router
    app.include_router(activity_router)
    log.info("[OK] Activity feed mounted at /api/activity")
except Exception as _act_exc:
    log.warning(f"Activity feed not mounted: {_act_exc}")

# Global search (command palette).
try:
    from search_api import router as search_router
    app.include_router(search_router)
    log.info("[OK] Global search mounted at /api/search")
except Exception as _srch_exc:
    log.warning(f"Global search not mounted: {_srch_exc}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


# ─── Creative Image Proxy ──────────────────────────────────────────
# The Snapchat / Fivetran media_history.download_link points at private
# GCS buckets (`storage.googleapis.com/ad-manager-creatives-production-us/...`)
# that require Google auth. Browsers can't fetch them directly — they get a 403
# / opaque CORS response. This proxy uses our BQ service account credentials
# to authenticate the GCS request, then streams the bytes back to the browser.
# If our service account lacks bucket permission, the response is 403 and the
# adpreview widget falls back to a placeholder card.
from fastapi.responses import StreamingResponse, Response
from fastapi import Query as _Query, HTTPException as _HTTPException

@app.get("/api/creative_proxy")
async def creative_proxy(url: str = _Query(..., description="Full GCS URL to proxy")):
    """Authenticate against GCS with our BQ service account, fetch the URL,
    stream bytes back to the browser with a 7-day cache header.
    """
    # Whitelist: only allow storage.googleapis.com and well-known ad CDNs to
    # avoid being a generic open-proxy.
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise _HTTPException(400, "Only https URLs allowed")
    allowed_hosts = {
        "storage.googleapis.com",
        "media.licdn.com",          # LinkedIn CDN — actually publicly accessible
        "scontent.cdninstagram.com",
        "video.tiktokcdn.com",
        "i.pinimg.com",
    }
    if parsed.netloc not in allowed_hosts:
        raise _HTTPException(400, f"Host '{parsed.netloc}' not in allowed list")

    # Build auth headers from the service account
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as _GReq
        import httpx
    except Exception as e:
        raise _HTTPException(500, f"Auth libs unavailable: {e}")
    from pathlib import Path as _P
    creds_path = _P.home() / ".config" / "jarvis" / "bigquery-key.json"
    headers = {}
    if creds_path.exists() and parsed.netloc == "storage.googleapis.com":
        try:
            creds = service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
            )
            creds.refresh(_GReq())
            headers["Authorization"] = f"Bearer {creds.token}"
        except Exception as e:
            # Continue without auth — public buckets still work
            pass
    # Fetch (no streaming generator since httpx async stream + StreamingResponse
    # adds complexity; for typical ad thumbnails <2MB the in-memory hop is fine)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise _HTTPException(502, f"Upstream fetch failed: {e}")
    if r.status_code >= 400:
        raise _HTTPException(r.status_code, f"Upstream returned {r.status_code}")
    # Determine content-type. GCS sometimes returns multipart/form-data for raw
    # uploads, which prevents the browser from rendering them. Override based
    # on file extension when the upstream type is unhelpful.
    upstream_ct = (r.headers.get("content-type") or "").lower()
    ext = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else ""
    ext_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
        "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    }
    # If upstream gave us something useless (multipart/form-data, octet-stream,
    # or empty), fall back to the extension-derived type.
    if upstream_ct in ("", "application/octet-stream", "multipart/form-data") or "form-data" in upstream_ct:
        media_type = ext_map.get(ext, "application/octet-stream")
    elif upstream_ct.startswith(("image/", "video/", "audio/")):
        media_type = upstream_ct
    else:
        media_type = ext_map.get(ext, upstream_ct or "application/octet-stream")
    return Response(
        content=r.content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=604800, immutable",  # 7 days
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/jarvis_state")
async def get_jarvis_state():
    """Current Jarvis state for the Martin overlay to poll."""
    return {
        "state": _jarvis_state,
        "last_speech": _jarvis_last_speech,
        "speech_started_at": _jarvis_speech_at,
        "now": time.time(),
    }


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — checked before LLM."""
    t = text.lower().strip()

    # ── OHNI report ───────────────────────────────────────────────────────
    if any(p in t for p in [
        "ohni report", "open the ohni", "show me ohni",
        "neuroscience report", "neuroscience intelligence",
        "oil change and i", "all the eye", "o h n i",
        "marketing intelligence report", "full report",
        "open the full report", "show me the full report",
        "two tab", "interactive report",
    ]):
        return {"action": "show_ohni"}

    # ── Stroke report ──────────────────────────────────────────────────────
    if any(p in t for p in [
        "stroke report", "stroke dashboard", "stroke campaign",
        "show me stroke", "open stroke", "open the stroke",
        "stroke performance", "stroke page",
    ]):
        return {"action": "show_stroke"}

    # ── Self restart ──────────────────────────────────────────────────────
    if any(p in t for p in [
        "restart yourself", "restart jarvis", "reboot yourself",
        "reboot jarvis", "restart the server", "reload yourself",
        "reload jarvis", "restart your server", "restart the system",
    ]):
        return {"action": "self_restart"}

    # ── Dashboard build ───────────────────────────────────────────────────
    _dash_kw = ["build me a dashboard", "create a dashboard", "build a dashboard",
                "make a dashboard", "build the dashboard", "executive health dashboard",
                "executive health program dashboard", "build me the"]
    if any(p in t for p in _dash_kw):
        return {"action": "prompt_project", "target": f"orlando-health-dashboard ||| {text}"}

    # ── BigQuery data query ────────────────────────────────────────────────
    _svc = ["cardiology", "orthopedics", "cancer", "womens", "neuroscience", "primary care", "bariatrics", "urology", "pediatrics", "surgery"]
    _kw  = ["performing", "performance", "numbers", "leads", "spend", "cpa", "roi", "report", "how is", "how are"]
    _dq  = ["what is our cpa", "whats our cpa", "how many leads", "what is our spend",
            "how much are we spending", "what is our roi", "run a report",
            "build me a report", "give me a report", "show me the data", "query the data",
            "pull the data", "how are our campaigns", "ad performance"]
    if any(p in t for p in _dq):
        return {"action": "data_query", "text": text}
    if any(s in t for s in _svc) and any(k in t for k in _kw):
        return {"action": "data_query", "text": text}


    # ── Marketing dashboard ───────────────────────────────────────────────
    if any(p in t for p in [
        "show me the dashboard", "marketing dashboard", "show me the marketing",
        "open the dashboard", "pull up the dashboard", "open my dashboard",
        "show me my dashboard", "my marketing dashboard", "marketing report",
        "open marketing", "show marketing", "launch dashboard",
        "what are my numbers", "how is marketing doing", "campaign performance",
    ]):
        return {"action": "show_report"}

    words = t.split()

    if len(words) > 20:
        return None

    # ── Teams ──────────────────────────────────────────────────────────────
    if any(p in t for p in [
        "teams", "check teams", "teams messages", "teams inbox",
        "any teams messages", "unread teams", "teams mentions",
        "teams briefing", "teams meeting", "teams meetings",
        "what is on teams", "whats on teams", "go to teams",
        "open teams", "teams chat", "teams chats", "read teams",
        "check my teams", "teams calendar", "my teams",
    ]):
        if any(p in t for p in ["meeting", "meetings", "calendar", "schedule", "appointments", "tomorrow", "today"]):
            return {"action": "check_teams_meetings"}
        if any(p in t for p in ["send", "tell", "write", "dm"]):
            return {"action": "send_teams_message"}
        if any(p in t for p in ["status", "available", "busy", "do not disturb", "away"]):
            return {"action": "set_teams_status"}
        return {"action": "check_teams"}

    # ── Daily briefing ─────────────────────────────────────────────────────
    if any(p in t for p in [
        "daily briefing", "brief me", "morning briefing",
        "start my day", "what is my day", "plan my day",
        "what does my day look like", "run me through my day",
        "whats my day look like",
    ]):
        return {"action": "daily_briefing"}

    # ── Outlook email ──────────────────────────────────────────────────────
    if any(p in t for p in [
        "outlook email", "outlook mail", "outlook messages", "outlook inbox",
        "check outlook", "unread messages", "unread on outlook",
        "messages on outlook", "emails in outlook", "any messages",
        "new messages", "check my email", "check my mail", "any new emails",
        "any new mail", "unread emails", "unread mail", "read my email",
        "any emails", "any mail", "email update",
        "check my inbox", "check inbox", "what's in my inbox", "whats in my inbox",
        "read my inbox", "inbox update", "any new messages", "what emails", "my inbox", "check email", "my inbox",
    ]):
        return {"action": "check_mail"}


    # ── Read webpage content ───────────────────────────────────────────────
    read_web_keywords = [
        "read me the", "read the news", "read me the news", "read the headlines",
        "what are the headlines", "top stories", "top news", "read me the top",
        "summarize the news", "what's in the news", "whats in the news",
        "read associated press", "read ap news", "read bbc", "read cnn",
        "read the page", "read this page", "summarize this page",
        "read me the headlines", "read out the news", "read the top news",
        "read top news", "read the latest", "read me the latest",
        "what are the top stories", "latest headlines", "latest news",
        "news headlines", "news from", "read news", "read me news",
        "headlines from", "stories from", "top stories from",
    ]
    if any(p in t for p in read_web_keywords):
        return {"action": "read_webpage", "target": text}

    # ── Text input modal ────────────────────────────────────────────────────
    if any(p in t for p in [
        "open text mode", "text mode", "type a message", "open text input",
        "give me a screen to type", "let me type", "text input", "open keyboard",
        "i want to type", "type something",
    ]):
        return {"action": "open_text_mode"}

    # ── Outlook calendar ───────────────────────────────────────────────────
    if any(p in t for p in [
        "outlook calendar", "check my calendar", "check calendar",
        "calendar today", "my outlook calendar", "calendar in outlook",
        "what's my schedule", "whats my schedule", "what's on my calendar",
        "whats on my calendar", "do i have any meetings", "any meetings",
        "my schedule today", "what do i have today", "my calendar",
        "upcoming meetings", "next meeting", "calendar for today",
    ]):
        return {"action": "check_calendar"}

    # ── API Balance ────────────────────────────────────────────────────────
    if any(p in t for p in [
        "what's my balance", "whats my balance", "check my balance",
        "api balance", "how much credit", "how much money", "credits left",
        "how much do i have left", "balance left", "remaining balance",
        "how much is left", "check the balance", "what is my balance",
        "how much credits", "anthropic balance", "account balance",
    ]):
        return {"action": "check_balance"}

    # ── Screen ─────────────────────────────────────────────────────────────
    if any(p in t for p in [
        "look at my screen", "what's on my screen", "whats on my screen",
        "what am i looking at", "what do you see", "see my screen",
        "check my screen", "what's open", "whats open", "what apps are open",
    ]):
        return {"action": "describe_screen"}

    # ── Terminal ───────────────────────────────────────────────────────────
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude"]):
        return {"action": "open_terminal"}

    # ── Recent build ───────────────────────────────────────────────────────
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # ── Dispatch ───────────────────────────────────────────────────────────
    if any(p in t for p in [
        "where are we", "project status", "how's the build",
        "hows the build", "status update", "is it done", "is that done",
    ]):
        return {"action": "check_dispatch"}

    # ── Tasks ──────────────────────────────────────────────────────────────
    if any(p in t for p in [
        "what's on my list", "whats on my list", "my tasks",
        "my todo", "what do i need to do", "open tasks", "task list",
    ]):
        return {"action": "check_tasks"}

    # ── Usage ──────────────────────────────────────────────────────────────
    if any(p in t for p in [
        "usage", "how much have you cost", "api cost", "token usage",
    ]):
        return {"action": "check_usage"}

    return None

async def handle_open_terminal() -> str:
    result = await open_terminal("claude --dangerously-skip-permissions")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write CLAUDE.md with clear instructions
    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    # Write prompt to a file, then pipe it to claude -p
    # This avoids all shell escaping issues
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {path} && cat .jarvis_prompt.txt | claude -p --dangerously-skip-permissions"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{last["path"]}"\nend tell'
    await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=60,  # Web lookups need more time
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — skip audio if user spoke recently to avoid collision
        # Never skip for web/news lookups since they take a long time to generate
        _skip_audio = (
            voice_state and 
            time.time() - voice_state["last_user_time"] < 3 and
            lookup_type not in ("web", "news")
        )
        if _skip_audio:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Read calendar — tries Outlook Mac app first, falls back to Apple Calendar."""
    # Try Outlook Mac app via AppleScript
    if _OUTLOOK_AVAILABLE:
        try:
            import asyncio as _a
            events = await _a.get_event_loop().run_in_executor(None, _outlook_calendar.get_today)
            if events:
                result = _outlook_calendar.summarize_for_jarvis(events)
                log.info(f"[outlook_cal] {len(events)} events from Outlook app")
                return result
        except Exception as _e:
            log.warning(f"[outlook_cal] failed: {_e}")

    # Fall back to Apple Calendar
    try:
        events = get_todays_events()
        if events:
            result = format_events_for_context(events)
            log.info(f"[apple_cal] {len(events)} events from Apple Calendar")
            return result
    except Exception as _e:
        log.warning(f"[apple_cal] failed: {_e}")

    return "I could not retrieve your calendar, sir. Please ensure Outlook or Apple Calendar is open."


async def _do_mail_lookup() -> str:
    """Read email — tries Outlook Mac app first, falls back to Apple Mail."""
    # Try Outlook Mac app via AppleScript
    if _OUTLOOK_AVAILABLE:
        try:
            import asyncio as _a
            emails = await _a.get_event_loop().run_in_executor(None, lambda: _outlook_email.get_unread(max_count=10))
            if emails:
                result = _outlook_email.summarize_for_jarvis(emails)
                log.info(f"[outlook_email] {len(emails)} emails from Outlook app")
                return result
        except Exception as _e:
            log.warning(f"[outlook_email] failed: {_e}")

    # Fall back to Apple Mail
    try:
        import asyncio as _a
        loop = _a.get_event_loop()
        count = await loop.run_in_executor(None, lambda: get_unread_count())
        msgs = await loop.run_in_executor(None, lambda: get_unread_messages(max_count=5))
        result = format_unread_summary(count, msgs)
        log.info(f"[apple_mail] {count} unread from Apple Mail")
        return result
    except Exception as _e:
        log.warning(f"[apple_mail] failed: {_e}")

    return "I could not retrieve your emails, sir. Please ensure Outlook or Apple Mail is open."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    if anthropic_client:
        return await describe_screen(anthropic_client)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender



async def _fetch_and_read_url(url: str) -> str:
    """Fetch a webpage and extract readable text summary using Claude."""
    import httpx as _httpx, re as _re
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            html = resp.text

        # Strip HTML tags
        text = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL)
        text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL)
        text = _re.sub(r'<[^>]+>', ' ', text)
        text = _re.sub(r'\s+', ' ', text).strip()
        text = text[:12000]  # Limit to 6k chars

        # Ask Claude to summarize comprehensively
        summary = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": f"""You are JARVIS reading the news to your boss. 
Extract and summarize the top 7-10 news stories from this page: {url}

Speak naturally as if reading to someone out loud. Use full sentences. 
Cover each story in 1-2 sentences. Group related stories.
Start with the most important story. No bullet points, no headers, no markdown.
Sound like a professional news anchor giving a briefing.

Page content:
{text}"""
            }]
        )
        return summary.content[0].text.strip()
    except Exception as e:
        log.warning(f"[web_read] failed for {url}: {e}")
        return f"I was unable to read that page, sir. {str(e)[:100]}"

async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: anthropic.AsyncAnthropic) -> str:
    """Deep research with Opus — write results to HTML, open in browser."""
    try:
        research_response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
        )
        research_text = research_response.content[0].text

        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary via Haiku
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        return summary.content[0].text + " Full results are in your browser, sir."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Phase 3 — one builder per WebSocket connection
    def _builder_runner(sql, params):
        """Adapter: ReportBuilder wants list[dict]; BQClient returns QueryResult."""
        qr = _builder_bq.query(sql, params)
        return qr.rows

    builder_session = BuilderSession(_builder_catalog, query_runner=_builder_runner)

    # Phase 4 - per-WebSocket Martin state
    martin_session = MartinSession()

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            try:
                raw = await ws.receive_text()
            except Exception:
                log.info("Client disconnected during receive — exiting loop")
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            _set_jarvis_state("thinking")
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → claude -p → Haiku summary → JARVIS voice ──
                # ─── MARTIN: Smart with Martin browser automation ──────────────
                elif is_martin_trigger(user_text):
                    response_text = await _handle_martin_command(
                        user_text, martin_session, ws, anthropic_client
                    )

                # ─── BUILDER MODE: interactive report construction ─────────────
                elif builder_session.in_builder_mode or is_builder_trigger(user_text):
                    try:
                        if builder_session.in_builder_mode:
                            ws_msgs = builder_session.handle_input(user_text)
                        else:
                            ws_msgs = builder_session.start(user_text)
                        spoken = None
                        for m in ws_msgs:
                            s = m.pop("_spoken", None)
                            if s:
                                spoken = s
                            await ws.send_json(m)
                        if spoken:
                            audio = await synthesize_speech(spoken)
                            if audio:
                                await ws.send_json({"type": "status", "state": "speaking"})
                                await ws.send_json({
                                    "type": "audio",
                                    "data": base64.b64encode(audio).decode(),
                                    "text": spoken,
                                })
                        response_text = spoken or "(builder action)"
                    except Exception as e:
                        log.exception("Builder error")
                        await ws.send_json({"type": "builder:error", "message": str(e)})
                        await ws.send_json({"type": "builder:done", "reason": "error"})
                        response_text = f"Builder error: {e}"

                elif work_session.active:
                    # Always check fast actions first even in work mode
                    _fast_action = detect_action_fast(user_text)
                    if _fast_action:
                        action = _fast_action
                        if action["action"] == "show_report":
                            response_text = "Opening your marketing dashboard now, sir."
                            import asyncio as _ai3
                            async def _do_report():
                                try:
                                    summary = await build_voice_summary("overview")
                                    audio = await synthesize_speech(summary)
                                    if audio:
                                        await ws.send_json({"type": "status", "state": "speaking"})
                                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": summary})
                                        await ws.send_json({"type": "status", "state": "idle"})
                                    import subprocess
                                    subprocess.Popen(["open", "https://localhost:8340/api/report/dashboard"])
                                except Exception as e:
                                    log.error(f"Report open failed: {e}")
                            _ai3.create_task(_do_report())
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            import asyncio as _ai
                            _ai.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            import asyncio as _ai
                            _ai.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "show_report":
                            response_text = "Opening your marketing dashboard now, sir."
                            import subprocess as _sp
                            _sp.Popen(["open", "https://localhost:8340/api/report/dashboard"])
                            log.info("JARVIS: opened marketing dashboard")
                        elif action["action"] == "check_balance":
                            response_text = "Checking your API balance now, sir."
                            async def _do_balance_check():
                                try:
                                    import asyncio as _asyncio2
                                    info = await _asyncio2.get_event_loop().run_in_executor(None, get_balance)
                                    msg = format_balance(info, "current")
                                    audio = await synthesize_speech(msg)
                                    if audio:
                                        await ws.send_json({"type": "status", "state": "speaking"})
                                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                        await ws.send_json({"type": "status", "state": "idle"})
                                        log.info(f"JARVIS (balance): {msg}")
                                except Exception as e:
                                    log.error(f"Balance check failed: {e}")
                            import asyncio as _ai
                            _ai.create_task(_do_balance_check())
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            import asyncio as _ai
                            _ai.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "read_webpage":
                            import re as _re2
                            _t2 = action.get("target", "").lower()
                            _news_map2 = {
                                "associated press": "https://apnews.com",
                                "ap news": "https://apnews.com",
                                "bbc": "https://bbc.com/news",
                                "cnn": "https://cnn.com",
                                "reuters": "https://reuters.com",
                                "nbc": "https://nbcnews.com",
                                "fox news": "https://foxnews.com",
                                "washington post": "https://washingtonpost.com",
                            }
                            _read_url2 = "https://apnews.com"
                            for _n, _u in _news_map2.items():
                                if _n in _t2:
                                    _read_url2 = _u
                                    break
                            response_text = "Reading the headlines for you now, sir."
                            import asyncio as _ai
                            _ai.create_task(_lookup_and_report("web", lambda: _fetch_and_read_url(_read_url2), ws, history=history, voice_state=voice_state))
                        elif action["action"] == "open_text_mode":
                            response_text = "Opening text input now, sir."
                            import asyncio as _ai
                            _ai.create_task(ws.send_json({"type": "open_text_mode"}))
                        elif action["action"] == "prompt_project":
                            # Dashboard / project build via fast action
                            _pp_target = action.get("target", "")
                            _pp_parts = _pp_target.split("|||")
                            _pp_name = _pp_parts[0].strip()
                            _pp_prompt = _pp_parts[1].strip() if len(_pp_parts) > 1 else _pp_target
                            _pp_dir = str(Path.home() / "Desktop" / _pp_name)
                            os.makedirs(_pp_dir, exist_ok=True)
                            _pp_did = dispatch_registry.register(_pp_name, _pp_dir, _pp_prompt)
                            import asyncio as _ai
                            _ai.create_task(_execute_prompt_project(_pp_name, _pp_prompt, work_session, ws, dispatch_id=_pp_did, history=history, voice_state=voice_state))
                        else:
                            # Let work mode handle other fast actions
                            pass
                    elif is_casual_question(user_text):
                        # Quick chat — bypass claude -p, use Haiku
                        response_text = await generate_response(
                            user_text, anthropic_client, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to claude -p (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → claude -p: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if Claude Code is stalling (asking questions instead of building)
                        if full_response and anthropic_client:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Haiku
                        if full_response and anthropic_client:
                            try:
                                summary = await anthropic_client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}],
                                )
                                response_text = summary.content[0].text
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "self_restart":
                            response_text = "Restarting now, sir. I will be back online in a few seconds."
                            async def _do_restart():
                                import asyncio, subprocess
                                await asyncio.sleep(1.5)
                                subprocess.Popen([
                                    "/bin/bash", "-c",
                                    "lsof -ti :8340 | xargs kill -9 2>/dev/null; sleep 1; "                                    "cd ~/jarvis && source venv/bin/activate && python server.py &"
                                ], shell=False)
                            asyncio.create_task(_do_restart())
                        elif action["action"] == "prompt_project":
                            # Dashboard / project build triggered from chat mode
                            _pp_target = action.get("target", "")
                            _pp_parts = _pp_target.split("|||")
                            _pp_name = _pp_parts[0].strip()
                            _pp_prompt = _pp_parts[1].strip() if len(_pp_parts) > 1 else _pp_target
                            _pp_dir = str(Path.home() / "Desktop" / _pp_name)
                            os.makedirs(_pp_dir, exist_ok=True)
                            _pp_did = dispatch_registry.register(_pp_name, _pp_dir, _pp_prompt)
                            log.info(f"Chat mode dispatching to {_pp_name}: {_pp_prompt[:80]}")
                            asyncio.create_task(_execute_prompt_project(_pp_name, _pp_prompt, work_session, ws, dispatch_id=_pp_did, history=history, voice_state=voice_state))
                            response_text = "On it, sir. Building the dashboard now — I will let you know when it is ready."
                        elif action["action"] == "data_query":
                            question = action.get("text", "")
                            response_text = "Let me query the data for you, sir."
                            async def _do_data_query(q=question):
                                try:
                                    from reporting_module import run_custom_query
                                    answer = await run_custom_query(q)
                                    audio = await synthesize_speech(answer)
                                    if audio:
                                        await ws.send_json({"type": "status", "state": "speaking"})
                                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": answer})
                                        await ws.send_json({"type": "status", "state": "idle"})
                                        log.info(f"JARVIS (data query): {answer[:100]}")
                                except Exception as e:
                                    log.error(f"Data query failed: {e}")
                            asyncio.create_task(_do_data_query())
                        elif action["action"] == "show_ohni":
                            import subprocess as _spA
                            _spA.Popen(["open", "https://localhost:8340/api/report/ohni"])
                            log.info("JARVIS: opened OHNI report")
                            response_text = "Opening the OHNI Marketing Intelligence Report now, sir."
                        elif action["action"] == "show_stroke":
                            import subprocess as _spB
                            _spB.Popen(["open", "https://localhost:8340/api/report/stroke"])
                            log.info("JARVIS: opened stroke report")
                            response_text = "Opening the Stroke Campaign Report now, sir."
                        elif action["action"] == "show_report":
                            import subprocess as _sp
                            _sp.Popen(["open", "https://localhost:8340/api/report/dashboard"])
                            log.info("JARVIS: opened marketing dashboard")
                            response_text = "Opening your marketing dashboard now, sir."
                        elif action["action"] == "check_balance":
                            response_text = "Checking your API balance now, sir."
                            async def _do_balance_check():
                                try:
                                    import asyncio as _asyncio2
                                    info = await _asyncio2.get_event_loop().run_in_executor(None, get_balance)
                                    msg = format_balance(info, "current")
                                    audio = await synthesize_speech(msg)
                                    if audio:
                                        await ws.send_json({"type": "status", "state": "speaking"})
                                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                        await ws.send_json({"type": "status", "state": "idle"})
                                        log.info(f"JARVIS (balance): {msg}")
                                except Exception as e:
                                    log.error(f"Balance check failed: {e}")
                            import asyncio as _ai
                            _ai.create_task(_do_balance_check())
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_teams":
                            response_text = "Checking your Teams messages now, sir."
                            async def _do_teams():
                                import asyncio as _a, base64 as _b64
                                if _TEAMS_AVAILABLE:
                                    msgs = await _a.get_event_loop().run_in_executor(None, lambda: _teams.get_unread_messages(10))
                                    result = _teams.summarize_messages_for_jarvis(msgs)
                                else:
                                    result = "Teams integration unavailable, sir."
                                audio = await synthesize_speech(result)
                                try:
                                    await ws.send_json({"type": "status", "state": "speaking"})
                                    if audio:
                                        await ws.send_json({"type": "audio", "data": _b64.b64encode(audio).decode(), "text": result})
                                    await ws.send_json({"type": "status", "state": "idle"})
                                except Exception:
                                    pass
                            asyncio.create_task(_do_teams())
                        elif action["action"] == "read_webpage":
                            # Extract URL from text and read it
                            import re as _re
                            from urllib.parse import quote as _quote
                            _text = action.get("target", "")
                            _url_match = _re.search(
                                r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)',
                                _text, _re.IGNORECASE
                            )
                            # Map common news sources
                            _news_map = {
                                "associated press": "https://apnews.com",
                                "ap news": "https://apnews.com",
                                "bbc": "https://bbc.com/news",
                                "cnn": "https://cnn.com",
                                "nbc": "https://nbcnews.com",
                                "nyt": "https://nytimes.com",
                                "new york times": "https://nytimes.com",
                                "fox news": "https://foxnews.com",
                                "reuters": "https://reuters.com",
                                "washington post": "https://washingtonpost.com",
                            }
                            _read_url = None
                            for _name, _url in _news_map.items():
                                if _name in _text.lower():
                                    _read_url = _url
                                    break
                            if not _read_url and _url_match:
                                _read_url = _url_match.group(0)
                                if not _read_url.startswith("http"):
                                    _read_url = "https://" + _read_url
                            if not _read_url:
                                _read_url = "https://apnews.com"
                            response_text = f"Reading the headlines for you now, sir."
                            asyncio.create_task(_lookup_and_report(
                                "web", 
                                lambda: _fetch_and_read_url(_read_url),
                                ws, history=history, voice_state=voice_state
                            ))
                        elif action["action"] == "open_text_mode":
                            response_text = "Opening text input now, sir."
                            await ws.send_json({"type": "open_text_mode"})
                        elif action["action"] == "check_teams_meetings":
                            response_text = "Checking your Teams calendar now, sir."
                            async def _do_teams_meetings():
                                import asyncio as _a, base64 as _b64
                                if _TEAMS_AVAILABLE:
                                    await open_browser("https://teams.microsoft.com/v2/#/calendarv2", "chrome")
                                    await _a.sleep(3)
                                    meetings = await _a.get_event_loop().run_in_executor(None, _teams.get_todays_meetings)
                                    result = _teams.summarize_meetings_for_jarvis(meetings)
                                else:
                                    result = "Teams integration unavailable, sir."
                                audio = await synthesize_speech(result)
                                try:
                                    await ws.send_json({"type": "status", "state": "speaking"})
                                    if audio:
                                        await ws.send_json({"type": "audio", "data": _b64.b64encode(audio).decode(), "text": result})
                                    await ws.send_json({"type": "status", "state": "idle"})
                                except Exception:
                                    pass
                            asyncio.create_task(_do_teams_meetings())
                        elif action["action"] == "daily_briefing":
                            response_text = "Preparing your daily briefing now, sir."
                            async def _do_briefing():
                                import asyncio as _a, base64 as _b64
                                parts = []
                                if _OUTLOOK_WEB:
                                    try:
                                        emails = await _a.get_event_loop().run_in_executor(None, lambda: _outlook_web.get_unread_emails(5))
                                        parts.append(_outlook_web.summarize_emails_for_jarvis(emails))
                                        events = await _a.get_event_loop().run_in_executor(None, _outlook_web.get_today_events)
                                        parts.append(_outlook_web.summarize_calendar_for_jarvis(events))
                                    except Exception as _e:
                                        log.warning(f"Briefing outlook error: {_e}")
                                if _TEAMS_AVAILABLE:
                                    try:
                                        msgs = await _a.get_event_loop().run_in_executor(None, lambda: _teams.get_unread_messages(5))
                                        parts.append(_teams.summarize_messages_for_jarvis(msgs))
                                    except Exception as _e:
                                        log.warning(f"Briefing teams error: {_e}")
                                from datetime import date as _date
                                today = _date.today().strftime("%A, %B %d")
                                briefing = f"Good morning, sir. Today is {today}. " + " ".join(parts) + " Shall I help you plan your day?"
                                audio = await synthesize_speech(briefing)
                                try:
                                    await ws.send_json({"type": "status", "state": "speaking"})
                                    if audio:
                                        await ws.send_json({"type": "audio", "data": _b64.b64encode(audio).decode(), "text": briefing})
                                    await ws.send_json({"type": "status", "state": "idle"})
                                except Exception:
                                    pass
                            asyncio.create_task(_do_briefing())
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not anthropic_client:
                            response_text = "API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text, anthropic_client, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                log.info(f"Handling embedded action: {embedded_action['action']}")
                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed CLAUDE.md
                                    Path(path, "CLAUDE.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    log.info(f"Embedded action: research → {embedded_action.get('target', '')[:60]}")
                                    # Research enters work mode too — with BigQuery context
                                    name = _generate_project_name(embedded_action["target"])
                                    _path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(_path, exist_ok=True)
                                    did = dispatch_registry.register(name, _path, embedded_action["target"])
                                    asyncio.create_task(
                                        _execute_prompt_project(name, embedded_action["target"], work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and anthropic_client:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, anthropic_client
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                # TTS
                tts = strip_markdown_for_tts(response_text)
                # Skip duplicate TTS when a handler (Martin, etc.) already spoke
                # and returned an empty/whitespace response.
                if not tts or not tts.strip():
                    log.info(f"JARVIS: {response_text}")  # already spoken by a handler
                    continue
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    audio = await synthesize_speech(tts)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                    else:
                        await ws.send_json({"type": "text", "text": response_text})
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception:
                    log.info("Client disconnected during send — skipping")
                    return
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    return {"success": True}

@app.post("/api/settings/test-anthropic")
async def api_test_anthropic(body: KeyTest):
    key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        await client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.fish.audio/v1/tts",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"text": "test", "reference_id": FISH_VOICE_ID},
            )
            if resp.status_code in (200, 201):
                return {"valid": True}
            elif resp.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}
            else:
                return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}



@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), prompt: str = ""):
    """Handle file attachments from text input modal."""
    import tempfile, os as _os, base64 as _b64
    try:
        content = await file.read()
        filename = file.filename or "attachment"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # Handle images
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            img_b64 = _b64.b64encode(content).decode()
            media_type = "image/" + ("jpeg" if ext == "jpg" else ext)
            combined = "[IMAGE:" + media_type + ":" + img_b64 + "\n" + prompt
            log.info(f"[upload] image {filename} ({len(content)} bytes)")
            return {"combined_prompt": combined, "type": "image"}

        # Handle text files
        elif ext in ("txt", "md", "py", "js", "ts", "jsx", "tsx", "json", "csv"):
            text_content = content.decode("utf-8", errors="ignore")
            combined = f"Here is the file '{filename}':\n\n{text_content}\n\n{prompt}"
            log.info(f"[upload] text file {filename} ({len(content)} bytes)")
            return {"combined_prompt": combined, "type": "text"}

        # Handle PDFs
        elif ext == "pdf":
            pdf_b64 = _b64.b64encode(content).decode()
            combined = "[PDF:" + pdf_b64 + "]\n" + prompt
            log.info(f"[upload] PDF {filename} ({len(content)} bytes)")
            return {"combined_prompt": combined, "type": "pdf"}

        else:
            return {"combined_prompt": prompt, "type": "unknown"}

    except Exception as e:
        log.error(f"[upload] error: {e}")
        return {"combined_prompt": prompt, "error": str(e)}

@app.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """Transcribe iOS audio using Anthropic Claude — no OpenAI key needed."""
    import tempfile, os as _os, base64 as _b64
    try:
        content = await audio.read()
        suffix = ".mp4" if "mp4" in (audio.content_type or "") else ".webm"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        # Convert audio to base64 and ask Claude to transcribe
        audio_b64 = _b64.b64encode(content).decode()
        media_type = "audio/mp4" if suffix == ".mp4" else "audio/webm"

        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(tmp_path, language="en")
            text = " ".join(s.text for s in segments).strip()
            log.info(f"[transcribe] whisper result: {text}")
        except Exception as _e:
            log.warning(f"[transcribe] whisper failed: {_e}")
            text = ""

        _os.unlink(tmp_path)
        log.info(f"[transcribe] iOS → '{text}'")
        return {"text": text}
    except Exception as e:
        log.error(f"[transcribe] error: {e}")
        return {"text": "", "error": str(e)}

@app.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    claude_installed = _shutil.which("claude") is not None
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    try: await get_recent_notes(count=1); notes_ok = True
    except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "claude_code_installed": claude_installed,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "anthropic": bool(env_dict.get("ANTHROPIC_API_KEY", "").strip() and env_dict.get("ANTHROPIC_API_KEY", "") != "your-anthropic-api-key-here"),
            "fish_audio": bool(env_dict.get("FISH_API_KEY", "").strip() and env_dict.get("FISH_API_KEY", "") != "your-fish-audio-api-key-here"),
            "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # For now, also open Terminal so user can see
    script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'    do script "cd {jarvis_dir} && claude --dangerously-skip-permissions"\n'
        'end tell'
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

# Serve martin_app.html from ~/Downloads so you can hit it at /app
# (Chrome blocks file:// URLs for the in-browser MCP; this gives us a stable URL.)
_MARTIN_APP = Path.home() / "Downloads" / "martin_app.html"
if _MARTIN_APP.exists():
    @app.get("/app")
    async def serve_martin_app():
        return FileResponse(str(_MARTIN_APP), media_type="text/html")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )



async def _handle_martin_command(user_text, martin_session, ws, claude_client):
    """Route a Martin-targeted voice utterance."""
    global _martin_browser
    import base64 as _b64

    async def _speak(text):
        audio = await synthesize_speech(text)
        if audio:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({
                "type": "audio",
                "data": _b64.b64encode(audio).decode(),
                "text": text,
            })

    # Follow-up command (only when response is cached)
    if martin_session.has_response:
        followup = parse_followup_command(user_text)
        if followup == "summarize":
            await _speak("One moment, sir.")
            summary = await _martin_summarize(martin_session.full_text, claude_client)
            await _speak(summary)
            return ""
        elif followup == "headlines":
            headlines_text = _martin_format_headlines(martin_session.headlines)
            await _speak(headlines_text)
            return ""

    # Verb command
    cmd = parse_martin_command(user_text)
    if cmd is None:
        await _speak("I didn't catch that Martin command, sir.")
        return ""

    if cmd.verb == "open_view":
        # Click a specific view on the Views page by name
        if _martin_browser is None:
            await _speak("Martin isn't open yet, sir. Say 'Martin open' first.")
            return ""
        try:
            chosen_title = await _martin_browser.open_view(cmd.arg)
            await _speak(f"Opening {chosen_title}, sir.")
            return ""
        except Exception as e:
            log.exception(f"Martin open_view failed for query: {cmd.arg!r}")
            err_msg = str(e)
            if err_msg.startswith("AMBIGUOUS:"):
                spoken = err_msg.replace("AMBIGUOUS:", "").strip()
                await _speak(spoken)
            else:
                await _speak(err_msg if err_msg else "I could not open that view, sir.")
            return ""

    if cmd.verb == "open":
        try:
            if _martin_browser is None:
                _martin_browser = MartinBrowser()
            await _martin_browser.start()
            await _martin_browser.goto("home")
            await _speak("Martin is open, sir.")
            return ""
        except SessionExpiredError:
            await _speak("Martin's session has expired, sir. Please run setup again.")
            return ""
        except Exception as e:
            log.exception("Martin open failed")
            await _speak("I couldn't open Martin, sir.")
            return ""

    if cmd.verb == "close":
        try:
            if _martin_browser is not None:
                await _martin_browser.close()
                _martin_browser = None
            martin_session.reset()
            await _speak("Martin is closed, sir.")
            return ""
        except Exception as e:
            log.exception("Martin close failed")
            return ""

    if cmd.verb == "goto":
        if _martin_browser is None:
            await _speak("Martin isn't open yet, sir. Say 'Martin open' first.")
            return ""
        try:
            if cmd.arg == "automations_reports":
                await _martin_browser.goto("automations")
                page = _martin_browser._page
                await page.locator('button:has-text("Reports"), a:has-text("Reports")').first.click()
                await _speak("Automations Reports, sir.")
                return ""
            elif cmd.arg == "automations_actions":
                await _martin_browser.goto("automations")
                page = _martin_browser._page
                await page.locator('button:has-text("Actions"), a:has-text("Actions")').first.click()
                await _speak("Automations Actions, sir.")
                return ""
            else:
                canonical = await _martin_browser.goto(cmd.arg)
                await _speak(f"{canonical.capitalize()}, sir.")
                return ""
        except Exception as e:
            log.exception(f"Martin goto {cmd.arg} failed")
            await _speak(f"I couldn't navigate to {cmd.arg}, sir.")
            return ""

    if cmd.verb == "ask":
        if not cmd.arg:
            await _speak("What should I ask Martin, sir?")
            return ""
        if _martin_browser is None:
            try:
                _martin_browser = MartinBrowser()
                await _martin_browser.start()
                await _martin_browser.goto("home")
            except Exception as e:
                log.exception("Auto-open failed")
                await _speak("I couldn't open Martin, sir.")
                return ""
        try:
            await _speak("Asking Martin, sir.")
            await _martin_browser.ask(cmd.arg, start_new_chat=True)
            martin_session.last_question = cmd.arg
            martin_session.phase = MartinPhase.WAITING_FOR_RESPONSE

            page = _martin_browser._page
            completed = await _martin_wait_for_response(page, state_callback=_set_jarvis_state)

            if not completed:
                await _speak("Martin is taking longer than expected, sir.")
                martin_session.phase = MartinPhase.IDLE
                return ""

            martin_session.headlines = await _martin_extract_headlines(page)
            martin_session.full_text = await _martin_extract_full_text(page)
            martin_session.phase = MartinPhase.READY

            await _speak("Martin is ready, sir.")
            return ""
        except Exception as e:
            log.exception("Martin ask failed")
            await _speak("Martin had a problem with that question, sir.")
            martin_session.phase = MartinPhase.IDLE
            return ""

    await _speak("I didn't recognize that Martin command, sir.")
    return ""
