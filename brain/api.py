"""FastAPI surface for the brain: list/run skills, read outputs, edit the
operating doc, preview the injected context, and quick-capture a thought.

The Anthropic client lives in server.py; we receive a getter via
set_client_getter() during wiring so this module stays decoupled.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import context, operating_doc, runner, vault

router = APIRouter(prefix="/api/brain", tags=["brain"])

_get_client = None


def set_client_getter(fn) -> None:
    global _get_client
    _get_client = fn


def _client():
    return _get_client() if _get_client else None


@router.get("/skills")
async def list_skills():
    return {"skills": runner.status()}


class RunBody(BaseModel):
    pass


@router.post("/skills/{name}/run")
async def run_skill(name: str):
    result = await runner.run_now(name, _client())
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


@router.get("/outputs")
async def outputs(limit: int = 25):
    return {"outputs": vault.list_outputs(limit=limit)}


@router.get("/outputs/{rel_id:path}")
async def output(rel_id: str):
    try:
        return {"id": rel_id, "content": vault.read_output(rel_id)}
    except Exception as e:
        return JSONResponse(status_code=404, content={"error": str(e)})


@router.get("/operating-doc")
async def get_operating_doc():
    return {"content": operating_doc.load(), "path": str(operating_doc.path())}


class DocBody(BaseModel):
    content: str


@router.put("/operating-doc")
async def put_operating_doc(body: DocBody):
    operating_doc.save(body.content)
    return {"ok": True}


@router.get("/context")
async def preview_context():
    """What the voice assistant currently sees from the brain (for debugging)."""
    return {"context": context.build_brain_context()}


class CaptureBody(BaseModel):
    text: str
    source: str = "api"


@router.post("/capture")
async def capture(body: CaptureBody):
    if not body.text.strip():
        return JSONResponse(status_code=400, content={"error": "empty capture"})
    p = vault.capture(body.text, body.source)
    return {"ok": True, "path": str(p)}
