"""
builder_tool — the "more power" escalation for the conversational agent (AI OS spec §4 deep path).

Martin's voice/text agent handles conversation + quick tools; for heavy, multi-step work — build a
website, an app, a script, a whole agent/codebase — it INTERVIEWS the user (in conversation) to get a
complete brief, then delegates the job to a more powerful Claude builder (Claude Code, `claude -p`)
that actually creates files and runs code in a project folder on the Desktop. Runs in the background
and reports back. Same mechanism the old Jarvis used for [ACTION:BUILD], now callable as a tool.
"""
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
from pathlib import Path

log = logging.getLogger("builder_tool")
DESKTOP = Path.home() / "Desktop"
_JOBS: list = []   # newest first: {title, folder, status, started, result?}

DELEGATE_SCHEMA = {
    "name": "delegate_to_builder",
    "description": (
        "Hand a heavy, sophisticated build to a MORE POWERFUL Claude builder (Claude Code) that creates "
        "real files and runs code: websites, web apps, scripts, data pipelines, or a whole AI agent / "
        "codebase. Use ONLY after you have INTERVIEWED the user enough to write a complete brief "
        "(goal, audience, key features, tech/stack or 'you choose', visual style, pages/flows, data "
        "sources, and what 'done' looks like). Provide a clear title and a THOROUGH brief. It runs in "
        "the background; tell the user you've started it and that you'll report when it's ready. Do NOT "
        "use this for quick answers, data lookups, on-screen briefs, or short documents — use the other tools."),
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "Short project name"},
        "brief": {"type": "string", "description": "Complete, detailed build spec gathered from the interview"}},
        "required": ["title", "brief"]},
}

STATUS_SCHEMA = {
    "name": "builder_status",
    "description": "Check the status of background builder jobs the user delegated (building / done / failed).",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def _slug(t: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (t or "project").lower()).strip("-")[:40]
    return s or "project"


async def _run(folder: str, brief: str):
    job = next((j for j in _JOBS if j["folder"] == folder), None)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "text", "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=folder)
        out, err = await proc.communicate(input=brief.encode())
        ok = proc.returncode == 0
        if job:
            job["status"] = "done" if ok else "failed"
            job["result"] = (out.decode()[-1500:] if out else "") or (err.decode()[-500:] if err else "")
        # open a built HTML entrypoint if there is one
        try:
            import webbrowser
            entry = Path(folder) / "index.html"
            htmls = [entry] if entry.exists() else list(Path(folder).glob("*.html"))
            if htmls:
                webbrowser.open("file://" + str(htmls[0]))
        except Exception:
            pass
    except FileNotFoundError:
        if job:
            job["status"] = "failed"
            job["result"] = "The 'claude' CLI isn't installed/on PATH for the server. Install Claude Code to enable builds."
    except Exception as e:  # noqa
        if job:
            job["status"] = "failed"; job["result"] = str(e)[:400]


async def delegate_to_builder(args: dict) -> dict:
    title = str((args or {}).get("title", "project"))
    brief = str((args or {}).get("brief", "")).strip()
    if not brief:
        return {"output": "I need a full brief before delegating — interview me first.", "provenance": [], "is_error": True}
    folder = str(DESKTOP / _slug(title))
    os.makedirs(folder, exist_ok=True)
    full = (brief + f"\n\nWorking directory: {folder}\nBuild real, working files here. If it's a site or "
            "app, create the right entrypoint (e.g. index.html) and make it runnable. Use real content, "
            "not lorem ipsum.")
    job = {"title": title, "folder": folder, "status": "building", "started": time.time()}
    _JOBS.insert(0, job)
    asyncio.create_task(_run(folder, full))
    return {"output": {"status": "building", "title": title, "folder": folder,
                       "note": "Started a Claude builder in the background — it's creating the files now. "
                               "Ask me for the status in a bit; I'll open it when it's ready."},
            "provenance": [{"source": "claude_code_builder", "folder": folder}], "is_error": False}


async def builder_status(args: dict) -> dict:
    if not _JOBS:
        return {"output": "No builder jobs yet.", "provenance": [], "is_error": False}
    rows = [{"title": j["title"], "status": j["status"], "folder": j["folder"],
             "note": (j.get("result", "")[:200] if j["status"] != "building" else "still building")} for j in _JOBS[:6]]
    return {"output": rows, "provenance": [], "is_error": False}
