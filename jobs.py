"""
jobs — background job monitoring (Kaelio /admin/jobs parity).

A tiny registry that background work reports into: skill runs, scans, embeddings,
eval batches, etc. Currently-running jobs live in memory; finished jobs persist
to jobs.json (capped) so the admin dashboard shows history across restarts.

Usage from any background task:
    import jobs
    jid = jobs.start("Morning Brief", "skill", "trigger=schedule")
    ... do work ...
    jobs.finish(jid, "success", "wrote 04-JARVIS-OUTPUTS/…")
Or one-shot:
    jobs.record("Schema scan", "scan", "success", "312 tables", duration=4.2)
"""
from __future__ import annotations

import json
import secrets
import threading
import time

from semantic.loader import SEMANTIC_DIR

_lock = threading.Lock()
_PATH = SEMANTIC_DIR / "jobs.json"
_MAX = 200
_running: dict[str, dict] = {}


def _load() -> list:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def start(name: str, jtype: str = "task", detail: str = "") -> str:
    jid = secrets.token_hex(6)
    _running[jid] = {
        "id": jid, "name": name, "type": jtype, "status": "running",
        "detail": detail, "started_at": time.time(), "finished_at": None, "duration": None,
    }
    return jid


def finish(jid: str, status: str = "success", detail: str | None = None) -> dict:
    job = _running.pop(jid, None)
    now = time.time()
    if not job:
        job = {"id": jid or secrets.token_hex(6), "name": "?", "type": "task", "started_at": now}
    job["status"] = status
    job["finished_at"] = now
    job["duration"] = round(now - job.get("started_at", now), 2)
    if detail is not None:
        job["detail"] = detail
    with _lock:
        items = _load()
        items.insert(0, job)
        _save(items[:_MAX])
    return job


def record(name: str, jtype: str = "task", status: str = "success",
           detail: str = "", duration: float | None = None) -> None:
    now = time.time()
    job = {"id": secrets.token_hex(6), "name": name, "type": jtype, "status": status,
           "detail": detail, "started_at": now, "finished_at": now, "duration": duration}
    with _lock:
        items = _load()
        items.insert(0, job)
        _save(items[:_MAX])


def snapshot() -> dict:
    done = _load()
    running = sorted(_running.values(), key=lambda j: j.get("started_at", 0), reverse=True)
    counts: dict[str, int] = {"running": len(running)}
    for j in done:
        counts[j.get("status", "success")] = counts.get(j.get("status", "success"), 0) + 1
    return {"running": running, "jobs": done, "counts": counts}


# ── API ──────────────────────────────────────────────────────────────────────
try:
    from fastapi import APIRouter
    router = APIRouter(prefix="/api/admin/jobs", tags=["jobs"])

    @router.get("")
    async def list_jobs(limit: int = 100, type: str = None, status: str = None):
        snap = snapshot()
        jobs_list = snap["jobs"]
        if type and type != "all":
            jobs_list = [j for j in jobs_list if j.get("type") == type]
        if status and status != "all":
            jobs_list = [j for j in jobs_list if j.get("status") == status]
        return {"running": snap["running"], "jobs": jobs_list[:max(1, min(limit, 200))],
                "counts": snap["counts"]}
except Exception:  # fastapi missing (e.g. during isolated tests) — API optional
    router = None
