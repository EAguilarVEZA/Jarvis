"""
knowledge_api
─────────────
Knowledge / Business-rules library — mirrors Kaelio's "Knowledge" concept.

Each item stores a small, named guidance block that the LLM planner should
follow when interpreting the user's question. Two usage modes:

  • "always" — injected into the /ask system prompt on every request
  • "auto"   — kept as retrieval candidates the planner can consult

Storage: ~/jarvis/semantic/knowledge.json (atomic writes + timestamped backup,
same pattern as views.json / briefs.json).

Routes (prefix /api/semantic/knowledge):
  GET    /api/semantic/knowledge          — list items (summaries)
  GET    /api/semantic/knowledge/{id}     — full item
  POST   /api/semantic/knowledge          — create a new item
  PUT    /api/semantic/knowledge/{id}     — update
  DELETE /api/semantic/knowledge/{id}     — delete

Helper for /ask:
  always_rules_text() → string of all "always"-mode rules, joined with ---,
                        ready to splice into the planner system prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from typing import Optional, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semantic.loader import SEMANTIC_DIR

router = APIRouter(prefix="/api/semantic/knowledge", tags=["knowledge"])

KNOWLEDGE_PATH = SEMANTIC_DIR / "knowledge.json"
_lock = threading.Lock()


# ─── Helpers ────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load() -> dict:
    if not KNOWLEDGE_PATH.exists():
        return {"items": []}
    try:
        with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "items" not in data:
            return {"items": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def _atomic_write(data: dict):
    KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if KNOWLEDGE_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(KNOWLEDGE_PATH, KNOWLEDGE_PATH.with_suffix(".json.bak." + ts))
        except OSError:
            pass
    tmp = KNOWLEDGE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(KNOWLEDGE_PATH)


def _summary(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "summary": item.get("summary"),
        "scope": item.get("scope", "global"),
        "usage_mode": item.get("usage_mode", "auto"),
        "sort_order": item.get("sort_order", 0),
        "used_count": item.get("used_count", 0),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


# ─── Model ───────────────────────────────────────────────────────────

ScopeT = Literal["global", "user"]
ModeT = Literal["always", "auto"]


class KnowledgeCreate(BaseModel):
    summary: str
    content: str
    scope: ScopeT = "global"
    usage_mode: ModeT = "auto"
    sort_order: int = 0


class KnowledgeUpdate(BaseModel):
    summary: Optional[str] = None
    content: Optional[str] = None
    scope: Optional[ScopeT] = None
    usage_mode: Optional[ModeT] = None
    sort_order: Optional[int] = None


# ─── Routes ──────────────────────────────────────────────────────────

@router.get("")
async def list_items(scope: Optional[str] = None, usage_mode: Optional[str] = None):
    data = _load()
    items = data.get("items", [])
    if scope:
        items = [x for x in items if x.get("scope") == scope]
    if usage_mode:
        items = [x for x in items if x.get("usage_mode") == usage_mode]
    items = sorted(items, key=lambda x: (x.get("sort_order", 0), -(x.get("updated_at") or 0)))
    return {"items": [_summary(x) for x in items], "count": len(items)}


@router.get("/{item_id}")
async def get_item(item_id: str):
    data = _load()
    item = next((x for x in data["items"] if x.get("id") == item_id), None)
    if not item:
        return _err(404, "not found", f"No knowledge item '{item_id}'")
    return item


@router.post("")
async def create_item(body: KnowledgeCreate):
    if not body.summary.strip():
        return _err(400, "summary required", "Items need a short summary description.")
    if not body.content.strip():
        return _err(400, "content required", "Items need a content body.")
    with _lock:
        data = _load()
        item = {
            "id": str(uuid.uuid4()),
            "summary": body.summary.strip(),
            "content": body.content,
            "scope": body.scope,
            "usage_mode": body.usage_mode,
            "sort_order": body.sort_order,
            "used_count": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["items"].append(item)
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return item


@router.put("/{item_id}")
async def update_item(item_id: str, body: KnowledgeUpdate):
    with _lock:
        data = _load()
        item = next((x for x in data["items"] if x.get("id") == item_id), None)
        if not item:
            return _err(404, "not found", f"No knowledge item '{item_id}'")
        # Version history — snapshot the prior content before overwriting it.
        if body.content is not None and body.content != item.get("content"):
            item.setdefault("versions", []).append({
                "content": item.get("content", ""),
                "summary": item.get("summary", ""),
                "at": item.get("updated_at") or item.get("created_at") or _now(),
            })
            item["versions"] = item["versions"][-20:]  # keep last 20
        for field in ("summary", "content", "scope", "usage_mode", "sort_order"):
            v = getattr(body, field)
            if v is not None:
                item[field] = v
        item["updated_at"] = _now()
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return item


@router.get("/{item_id}/versions")
async def list_versions(item_id: str):
    item = next((x for x in _load()["items"] if x.get("id") == item_id), None)
    if not item:
        return _err(404, "not found", f"No knowledge item '{item_id}'")
    return {"id": item_id, "current": {"content": item.get("content", ""), "at": item.get("updated_at")},
            "versions": list(reversed(item.get("versions", [])))}


@router.post("/{item_id}/revert/{index}")
async def revert_version(item_id: str, index: int):
    """Revert to a prior version (index into the reversed history from /versions)."""
    with _lock:
        data = _load()
        item = next((x for x in data["items"] if x.get("id") == item_id), None)
        if not item:
            return _err(404, "not found", f"No knowledge item '{item_id}'")
        hist = item.get("versions", [])
        rev = list(reversed(hist))
        if index < 0 or index >= len(rev):
            return _err(400, "bad index", "No such version.")
        target = rev[index]
        # snapshot current, then restore
        item.setdefault("versions", []).append({"content": item.get("content", ""), "summary": item.get("summary", ""), "at": item.get("updated_at") or _now()})
        item["content"] = target.get("content", "")
        if target.get("summary"):
            item["summary"] = target["summary"]
        item["updated_at"] = _now()
        item["versions"] = item["versions"][-20:]
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return item


@router.delete("/{item_id}")
async def delete_item(item_id: str):
    with _lock:
        data = _load()
        before = len(data["items"])
        data["items"] = [x for x in data["items"] if x.get("id") != item_id]
        if len(data["items"]) == before:
            return _err(404, "not found", f"No knowledge item '{item_id}'")
        try:
            _atomic_write(data)
        except OSError as e:
            return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": item_id}


class AutoCaptureBody(BaseModel):
    text: str
    create: bool = False   # persist extracted blocks as usage_mode='auto'


@router.post("/auto-capture")
async def auto_capture(body: AutoCaptureBody):
    """LLM-extract durable knowledge blocks from a chunk of text (chat, notes)."""
    text = (body.text or "").strip()
    if not text:
        return _err(400, "empty", "Provide text to extract knowledge from.")
    try:
        import os
        import re as _re
        import uuid as _uuid
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return _err(503, "no api key", "ANTHROPIC_API_KEY not set.")
        client = anthropic.AsyncAnthropic(api_key=key)
        model = os.getenv("JARVIS_BRAIN_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=model, max_tokens=800,
            system=("Extract durable KNOWLEDGE BLOCKS from the text — facts, definitions, business rules, "
                    "and preferences worth remembering long-term. Skip transient chatter. Respond ONLY as JSON: "
                    '{"blocks":[{"summary":"short title","content":"the fact"}]} (max 6; empty list if none).'),
            messages=[{"role": "user", "content": text[:6000]}],
        )
        raw = resp.content[0].text if resp.content else ""
        m = _re.search(r"\{.*\}", raw, _re.S)
        blocks = []
        if m:
            try:
                blocks = (json.loads(m.group(0)) or {}).get("blocks", [])
            except Exception:
                blocks = []
        created = 0
        if body.create and blocks:
            with _lock:
                data = _load()
                for b in blocks[:6]:
                    data["items"].append({
                        "id": _uuid.uuid4().hex[:8],
                        "summary": (b.get("summary") or "")[:120],
                        "content": b.get("content") or "",
                        "scope": "global", "usage_mode": "auto",
                        "created_at": _now(), "updated_at": _now(), "source": "auto-capture",
                    })
                    created += 1
                _atomic_write(data)
        return {"ok": True, "blocks": blocks, "created": created}
    except Exception as e:
        return _err(500, "extract failed", str(e))


# ─── Integration helper for /ask ────────────────────────────────────

def always_rules_text() -> str:
    """Return all 'always'-mode rules joined with separators, ready to splice
    into the planner system prompt. Empty string if there are no rules."""
    try:
        data = _load()
    except Exception:
        return ""
    rules = [x for x in data.get("items", []) if x.get("usage_mode") == "always"]
    rules.sort(key=lambda x: x.get("sort_order", 0))
    if not rules:
        return ""
    blocks = []
    for r in rules:
        title = r.get("summary", "").strip()
        body = r.get("content", "").strip()
        if title and body:
            blocks.append(f"### {title}\n{body}")
        elif body:
            blocks.append(body)
    return (
        "\n\n──── BUSINESS RULES (always apply) ────\n"
        + "\n\n".join(blocks)
        + "\n──── end of business rules ────\n"
    )


def auto_rules_for_question(question: str, max_rules: int = 3) -> str:
    """Simple keyword-overlap retrieval for 'auto'-mode rules. Returns the
    top-N rules whose summary or content has the highest token overlap with
    the question. Cheap fallback for now — can be replaced with embeddings."""
    try:
        data = _load()
    except Exception:
        return ""
    auto_items = [x for x in data.get("items", []) if x.get("usage_mode") == "auto"]
    if not auto_items:
        return ""
    q_tokens = set(t.lower() for t in question.split() if len(t) > 3)
    if not q_tokens:
        return ""
    scored = []
    for r in auto_items:
        haystack = (r.get("summary", "") + " " + r.get("content", "")).lower()
        score = sum(1 for t in q_tokens if t in haystack)
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    picks = [r for _, r in scored[:max_rules]]
    if not picks:
        return ""
    blocks = []
    for r in picks:
        title = r.get("summary", "").strip()
        body = r.get("content", "").strip()
        if title and body:
            blocks.append(f"### {title}\n{body}")
    return (
        "\n\n──── RELEVANT BUSINESS RULES (auto-applied) ────\n"
        + "\n\n".join(blocks)
        + "\n──── end of auto rules ────\n"
    )
