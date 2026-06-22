"""Scheduler + executor for the seven skills.

A lightweight asyncio loop checks every minute whether any skill's cron is due
and runs it: execute → write output to the vault → store a memory residue →
record state. Also exposes run_now() for on-demand runs from the API/voice.

No external scheduler dependency (no APScheduler) — a tiny cron matcher keeps
the footprint zero-install.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging

from . import skills, vault

log = logging.getLogger("jarvis.brain.runner")

_task: asyncio.Task | None = None
_get_client = None
_last_minute: dict[str, str] = {}


# ── cron matching (minute hour day-of-month month day-of-week) ────────────
def _field_match(field: str, value: int, dow: bool = False) -> bool:
    field = field.strip()
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if part.startswith("*/"):
            try:
                if value % int(part[2:]) == 0:
                    return True
            except ValueError:
                pass
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if dow and n == 7:
            n = 0
        if n == value:
            return True
    return False


def cron_due(cron: str, now: _dt.datetime) -> bool:
    try:
        minute, hour, dom, month, dowf = cron.split()
    except ValueError:
        return False
    dow_now = now.isoweekday() % 7  # Mon=1..Sat=6, Sun=0
    return (
        _field_match(minute, now.minute)
        and _field_match(hour, now.hour)
        and _field_match(dom, now.day)
        and _field_match(month, now.month)
        and _field_match(dowf, dow_now, dow=True)
    )


# ── state persistence ─────────────────────────────────────────────────────
def _state_path():
    return vault.folder("system") / "skills" / "state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        vault.ensure_structure()
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("could not save brain state: %s", e)


def _record(name: str, **fields) -> None:
    state = load_state()
    entry = state.get(name, {})
    entry.update(fields)
    state[name] = entry
    _save_state(state)


# ── execution ─────────────────────────────────────────────────────────────
async def execute(skill: skills.Skill, client, trigger: str = "schedule") -> dict:
    """Run one skill end-to-end; returns a small result dict."""
    last_err = None
    for attempt in range(2):
        try:
            result = await skill.run(client)
            path = vault.write_output(skill.subdir, result.title, result.body)
            try:
                import memory
                memory.remember(result.memory_note, mem_type="brief",
                                source=f"brain:{skill.name}", importance=result.importance)
            except Exception as e:
                log.debug("memory write skipped: %s", e)
            rel = str(path.relative_to(vault.VAULT_ROOT))
            _record(skill.name, status="ok", trigger=trigger,
                    last_run=_dt.datetime.now().isoformat(timespec="seconds"),
                    last_output=rel, error=None)
            log.info("brain skill ok: %s -> %s (%s)", skill.name, rel, trigger)
            return {"ok": True, "skill": skill.name, "output": rel}
        except Exception as e:
            last_err = e
            log.warning("brain skill %s attempt %d failed: %s", skill.name, attempt + 1, e)
            await asyncio.sleep(2)
    _record(skill.name, status="error", trigger=trigger,
            last_run=_dt.datetime.now().isoformat(timespec="seconds"), error=str(last_err))
    return {"ok": False, "skill": skill.name, "error": str(last_err)}


async def run_now(name: str, client) -> dict:
    skill = skills.get(name)
    if not skill:
        return {"ok": False, "error": f"unknown skill '{name}'"}
    if client is None:
        return {"ok": False, "error": "LLM client not available"}
    return await execute(skill, client, trigger="manual")


# ── scheduler loop ─────────────────────────────────────────────────────────
async def _loop():
    log.info("brain scheduler running (%d skills)", len(skills.SKILLS))
    while True:
        try:
            now = _dt.datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            client = _get_client() if _get_client else None
            for skill in skills.SKILLS:
                if not cron_due(skill.cron, now):
                    continue
                if _last_minute.get(skill.name) == stamp:
                    continue  # already fired this minute
                _last_minute[skill.name] = stamp
                if client:
                    asyncio.create_task(execute(skill, client, trigger="schedule"))
                else:
                    log.warning("skill %s due but no LLM client", skill.name)
        except Exception as e:
            log.warning("scheduler tick error: %s", e)
        await asyncio.sleep(max(5, 61 - _dt.datetime.now().second))


def start(get_client) -> None:
    """Start the background scheduler. `get_client` returns the Anthropic client or None."""
    global _task, _get_client
    _get_client = get_client
    vault.ensure_structure()
    try:
        from . import operating_doc
        operating_doc.ensure()
    except Exception:
        pass
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("brain scheduler started")


def status() -> list[dict]:
    state = load_state()
    out = []
    for s in skills.SKILLS:
        st = state.get(s.name, {})
        out.append({
            "name": s.name, "label": s.label, "cron": s.cron,
            "description": s.description, "subdir": s.subdir,
            "last_run": st.get("last_run"), "status": st.get("status"),
            "last_output": st.get("last_output"), "error": st.get("error"),
        })
    return out
