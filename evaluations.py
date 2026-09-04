"""
evaluations — test AI response quality over time (Kaelio /features/evaluations parity).

An evaluation case is a prompt plus pass/fail criteria. Running it does two model
calls: first it answers the prompt, then an LLM judge scores the answer against
the criteria (pass/fail + 0-100 score + reason). Each run is appended to the
case's history so you can watch quality trend over time.

State: evaluations.json under the semantic dir.
Requires ANTHROPIC_API_KEY (the same key Jarvis already uses).
"""
from __future__ import annotations
import system_llm  # route LLM calls through the active system model

import json
import os
import re
import threading
import time
import uuid
from typing import Optional

import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

_lock = threading.Lock()
_PATH = SEMANTIC_DIR / "evaluations.json"
_MODEL = os.getenv("JARVIS_EVAL_MODEL", os.getenv("JARVIS_BRAIN_MODEL", "claude-sonnet-4-6"))
_MAX_RUNS = 50  # keep the last N runs per case

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = system_llm.anthropic_client(api_key=key)
    return _client


def _load() -> list:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d.get("evals", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    except Exception:
        return []


def _save(evals: list) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"evals": evals}, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def _stats(ev: dict) -> dict:
    runs = ev.get("runs", [])
    passed = sum(1 for r in runs if r.get("passed"))
    last = runs[-1] if runs else None
    return {
        "run_count": len(runs),
        "pass_count": passed,
        "pass_rate": round(100 * passed / len(runs)) if runs else None,
        "last_run": last.get("ts") if last else None,
        "last_passed": last.get("passed") if last else None,
        "last_score": last.get("score") if last else None,
    }


def _summary(ev: dict) -> dict:
    return {"id": ev["id"], "name": ev.get("name", ""), "prompt": ev.get("prompt", ""),
            "criteria": ev.get("criteria", ""), "created_at": ev.get("created_at"),
            **_stats(ev)}


def _extract_json(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


# ── API ──────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


def _err(status, error, detail=""):
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


@router.get("")
async def list_evals():
    evals = _load()
    rate = [e for e in (_stats(x)["pass_rate"] for x in evals) if e is not None]
    return {"evaluations": [_summary(x) for x in evals],
            "overall_pass_rate": round(sum(rate) / len(rate)) if rate else None,
            "count": len(evals)}


@router.get("/{eid}")
async def get_eval(eid: str):
    ev = next((x for x in _load() if x.get("id") == eid), None)
    if not ev:
        return _err(404, "not found", "No such evaluation.")
    return ev


class EvalBody(BaseModel):
    name: str
    prompt: str
    criteria: str = ""


@router.post("")
async def create_eval(body: EvalBody):
    if not body.name.strip() or not body.prompt.strip():
        return _err(400, "missing", "Name and prompt are required.")
    ev = {"id": str(uuid.uuid4())[:8], "name": body.name.strip(), "prompt": body.prompt.strip(),
          "criteria": body.criteria.strip(), "created_at": time.time(), "runs": []}
    with _lock:
        evals = _load(); evals.append(ev); _save(evals)
    return ev


@router.put("/{eid}")
async def update_eval(eid: str, body: EvalBody):
    with _lock:
        evals = _load()
        ev = next((x for x in evals if x.get("id") == eid), None)
        if not ev:
            return _err(404, "not found", "No such evaluation.")
        ev["name"] = body.name.strip() or ev["name"]
        ev["prompt"] = body.prompt.strip() or ev["prompt"]
        ev["criteria"] = body.criteria.strip()
        _save(evals)
    return ev


@router.delete("/{eid}")
async def delete_eval(eid: str):
    with _lock:
        evals = _load()
        evals = [x for x in evals if x.get("id") != eid]
        _save(evals)
    return {"ok": True, "deleted": eid}


async def _run_case(ev: dict) -> dict:
    client = _get_client()
    # 1) Produce an answer to the prompt.
    ans = await client.messages.create(
        model=_MODEL, max_tokens=700,
        system="You are Jarvis, a precise analytics assistant. Answer the question clearly and concisely.",
        messages=[{"role": "user", "content": ev.get("prompt", "")}],
    )
    answer = ans.content[0].text if ans.content else ""
    # 2) Judge the answer against the criteria.
    crit = ev.get("criteria") or "The answer is correct, relevant, and clearly written."
    judge = await client.messages.create(
        model=_MODEL, max_tokens=300,
        system=("You are a strict evaluation judge. Given a question, an answer, and pass criteria, "
                "decide if the answer PASSES. Respond ONLY with JSON: "
                '{"passed": true|false, "score": 0-100, "reason": "one sentence"}.'),
        messages=[{"role": "user", "content":
                   f"QUESTION:\n{ev.get('prompt','')}\n\nANSWER:\n{answer}\n\nPASS CRITERIA:\n{crit}"}],
    )
    verdict = _extract_json(judge.content[0].text if judge.content else "")
    run = {
        "ts": time.time(),
        "passed": bool(verdict.get("passed")),
        "score": int(verdict.get("score", 0)) if str(verdict.get("score", "")).strip() not in ("", "None") else None,
        "reason": (verdict.get("reason") or "").strip(),
        "output": answer[:2000],
    }
    return run


@router.post("/{eid}/run")
async def run_eval(eid: str):
    evals = _load()
    ev = next((x for x in evals if x.get("id") == eid), None)
    if not ev:
        return _err(404, "not found", "No such evaluation.")
    try:
        run = await _run_case(ev)
    except Exception as e:
        return _err(500, "run failed", str(e))
    with _lock:
        evals = _load()
        ev = next((x for x in evals if x.get("id") == eid), None)
        if ev is not None:
            ev.setdefault("runs", []).append(run)
            ev["runs"] = ev["runs"][-_MAX_RUNS:]
            _save(evals)
    return {"ok": True, "run": run, "stats": _stats(ev)}


@router.post("/run-all")
async def run_all():
    evals = _load()
    results = []
    for ev in evals:
        try:
            run = await _run_case(ev)
            with _lock:
                cur = _load()
                target = next((x for x in cur if x.get("id") == ev["id"]), None)
                if target is not None:
                    target.setdefault("runs", []).append(run)
                    target["runs"] = target["runs"][-_MAX_RUNS:]
                    _save(cur)
            results.append({"id": ev["id"], "name": ev.get("name"), "passed": run["passed"], "score": run["score"]})
        except Exception as e:
            results.append({"id": ev["id"], "name": ev.get("name"), "error": str(e)})
    passed = sum(1 for r in results if r.get("passed"))
    return {"ok": True, "results": results, "passed": passed, "total": len(results)}
