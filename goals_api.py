"""
goals_api
─────────
Goals & target tracking on top of the semantic layer.

A Goal pins a target value to a metric for a period (e.g. "spend should be
$1M this month", "CTR ≥ 2.5% always"). The /evaluate endpoint runs the
metric for that period through the curated engine and returns actual,
delta, and on-track status. KPI widgets render this inline; a Goals page
lists them with status pips.

Routes (prefix /api/goals):
  GET    /api/goals                — list goals (with current evaluation)
  GET    /api/goals/{id}           — one goal
  POST   /api/goals                — create
  PUT    /api/goals/{id}           — update
  DELETE /api/goals/{id}           — delete
  POST   /api/goals/{id}/evaluate  — re-run evaluation for one goal
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import shutil
import threading
import datetime as dt
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic import load_model, SemanticLoadError, build_sql, run_query, StructuredQuery, QueryBuildError, ResolverError, ExecutorConfigError
from semantic.loader import SEMANTIC_DIR

router = APIRouter(prefix="/api/goals", tags=["goals"])

GOALS_PATH = SEMANTIC_DIR / "goals.json"
_lock = threading.Lock()

VALID_COMPARISONS = {"ge", "le", "gt", "lt", "eq"}   # ge = actual should be ≥ target
VALID_PERIODS = {"day", "week", "month", "quarter", "year", "all"}


def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not GOALS_PATH.exists():
        return {"goals": []}
    try:
        with open(GOALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "goals" not in data:
            return {"goals": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"goals": []}


def _atomic_write(data: dict):
    GOALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if GOALS_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(GOALS_PATH, GOALS_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = GOALS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(GOALS_PATH)


# ─── Period → date range ─────────────────────────────────────────────
def _period_range(period: str) -> Optional[tuple[str, str]]:
    today = dt.date.today()
    if period == "all":
        return None
    if period == "day":
        return (today.isoformat(), today.isoformat())
    if period == "week":
        # Monday of this week → today
        start = today - dt.timedelta(days=today.weekday())
        return (start.isoformat(), today.isoformat())
    if period == "month":
        start = today.replace(day=1)
        return (start.isoformat(), today.isoformat())
    if period == "quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_start_month, day=1)
        return (start.isoformat(), today.isoformat())
    if period == "year":
        start = today.replace(month=1, day=1)
        return (start.isoformat(), today.isoformat())
    return None


def _find_date_field(model, table_key: str) -> Optional[str]:
    """Pick the most likely date field on a table to scope a goal's period."""
    t = model.table(table_key)
    if not t or not t.dates:
        return None
    # Prefer one named 'date'; else first date field
    for f in t.dates:
        if f.key.lower() == "date":
            return f.key
    return t.dates[0].key


# ─── Pydantic models ─────────────────────────────────────────────────
class GoalIn(BaseModel):
    name: Optional[str] = None
    table: str
    metric: str
    target: float
    comparison: str = "ge"            # actual {ge|le|gt|lt|eq} target
    period: str = "month"
    description: Optional[str] = ""


def _validate(body: GoalIn) -> Optional[str]:
    if body.comparison not in VALID_COMPARISONS:
        return f"comparison must be one of {sorted(VALID_COMPARISONS)}"
    if body.period not in VALID_PERIODS:
        return f"period must be one of {sorted(VALID_PERIODS)}"
    return None


# ─── Evaluation ──────────────────────────────────────────────────────
def _on_track(actual: Optional[float], target: float, comparison: str) -> Optional[bool]:
    if actual is None:
        return None
    if comparison == "ge": return actual >= target
    if comparison == "le": return actual <= target
    if comparison == "gt": return actual > target
    if comparison == "lt": return actual < target
    if comparison == "eq": return abs(actual - target) < 1e-9
    return None


def _delta_pct(actual: Optional[float], target: float) -> Optional[float]:
    if actual is None or target == 0:
        return None
    return (actual - target) / abs(target) * 100.0


async def _evaluate(goal: dict) -> dict:
    """Run the goal's metric for its period and return evaluation."""
    try:
        model = load_model()
    except SemanticLoadError as e:
        return {"ok": False, "error": str(e)}
    table = model.table(goal["table"])
    if not table:
        return {"ok": False, "error": f"Unknown table '{goal['table']}'"}
    # Build a metric-only query
    qdict = {"primary_table": goal["table"],
             "metrics": [{"table": goal["table"], "field": goal["metric"]}],
             "filters": [], "limit": 1}
    rng = _period_range(goal.get("period", "month"))
    if rng:
        df = _find_date_field(model, goal["table"])
        if df:
            qdict["filters"].append({"table": goal["table"], "field": df,
                                     "op": "between", "value": list(rng)})
    try:
        sq = StructuredQuery.from_dict(qdict)
        sql = build_sql(sq, model)
    except (QueryBuildError, ResolverError, KeyError) as e:
        return {"ok": False, "error": f"Could not build query: {e}"}
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, run_query, sql)
    except ExecutorConfigError as e:
        return {"ok": False, "error": "BigQuery not configured: " + str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    actual = None
    if result.rows and result.rows[0]:
        v = result.rows[0][0]
        try: actual = float(v) if v is not None else None
        except (TypeError, ValueError): actual = None
    return {
        "ok": True,
        "actual": actual,
        "target": goal["target"],
        "delta_pct": _delta_pct(actual, goal["target"]),
        "on_track": _on_track(actual, goal["target"], goal.get("comparison", "ge")),
        "period_range": list(rng) if rng else None,
        "evaluated_at": _now(),
    }


# ─── Endpoints ───────────────────────────────────────────────────────
@router.get("")
async def list_goals(evaluate: bool = True):
    data = _load()
    goals = data["goals"]
    if not evaluate:
        return {"goals": goals, "count": len(goals)}
    enriched = []
    for g in goals:
        ev = await _evaluate(g)
        enriched.append({**g, "evaluation": ev})
    return {"goals": enriched, "count": len(enriched)}


@router.get("/{goal_id}")
async def get_goal(goal_id: str):
    data = _load()
    g = next((x for x in data["goals"] if x.get("id") == goal_id), None)
    if not g:
        return _err(404, "not found", f"No goal '{goal_id}'")
    ev = await _evaluate(g)
    return {**g, "evaluation": ev}


@router.post("")
async def create_goal(body: GoalIn):
    err = _validate(body)
    if err: return _err(400, "invalid", err)
    with _lock:
        data = _load()
        g = {
            "id": str(uuid.uuid4()),
            "name": body.name or f"{body.metric} {body.comparison} {body.target}",
            "table": body.table, "metric": body.metric,
            "target": float(body.target),
            "comparison": body.comparison, "period": body.period,
            "description": body.description or "",
            "created_at": _now(), "updated_at": _now(),
        }
        data["goals"].append(g)
        try: _atomic_write(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return g


@router.put("/{goal_id}")
async def update_goal(goal_id: str, body: GoalIn):
    err = _validate(body)
    if err: return _err(400, "invalid", err)
    with _lock:
        data = _load()
        g = next((x for x in data["goals"] if x.get("id") == goal_id), None)
        if not g: return _err(404, "not found", f"No goal '{goal_id}'")
        g.update({
            "name": body.name or g.get("name"),
            "table": body.table, "metric": body.metric,
            "target": float(body.target),
            "comparison": body.comparison, "period": body.period,
            "description": body.description or "",
            "updated_at": _now(),
        })
        try: _atomic_write(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return g


@router.delete("/{goal_id}")
async def delete_goal(goal_id: str):
    with _lock:
        data = _load()
        before = len(data["goals"])
        data["goals"] = [x for x in data["goals"] if x.get("id") != goal_id]
        if len(data["goals"]) == before:
            return _err(404, "not found", f"No goal '{goal_id}'")
        try: _atomic_write(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": goal_id}


@router.post("/{goal_id}/evaluate")
async def evaluate_goal(goal_id: str):
    data = _load()
    g = next((x for x in data["goals"] if x.get("id") == goal_id), None)
    if not g: return _err(404, "not found", f"No goal '{goal_id}'")
    ev = await _evaluate(g)
    return {"goal_id": goal_id, "evaluation": ev}
