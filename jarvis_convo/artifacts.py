"""
Artifact completion service (blueprint §10). The recurring failure mode is an assistant that
says it will 'work on' a document and then ends the turn producing nothing. This service makes
durable work explicit and honest:

  create → build → validate → complete

An artifact is marked complete ONLY after validation succeeds, and is returned visibly with a
stable filename. If execution fails, the caller gets the partial state plus the EXACT failure —
never an implication that work continues invisibly (§10, §20). If requirements change mid-build,
the build is cancellable and can be regenerated from the new spec (§17).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class ArtifactStatus(str, Enum):
    CREATED = "created"
    BUILDING = "building"
    VALIDATED = "validated"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Artifact:
    id: str
    kind: str                       # markdown | text | json | docx | code
    filename: str
    content: str = ""
    status: ArtifactStatus = ArtifactStatus.CREATED
    error: str = ""
    version: int = 1
    history: list = field(default_factory=list)

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "filename": self.filename,
                "status": self.status.value, "bytes": len(self.content),
                "error": self.error, "version": self.version}


def _validate(kind: str, content: str) -> tuple[bool, str]:
    if not (content or "").strip():
        return False, "artifact is empty"
    if kind == "json":
        try:
            json.loads(content)
        except Exception as e:
            return False, f"invalid JSON: {e}"
    if kind in ("docx", "markdown", "text", "code") and len(content) < 3:
        return False, "content too short to be a valid document"
    return True, ""


class ArtifactService:
    def __init__(self, log=None):
        self.log = log
        self.store: dict[str, Artifact] = {}

    def _emit(self, evtype, **d):
        if self.log:
            self.log.publish(evtype, **d)

    def create(self, kind: str, filename: str) -> Artifact:
        a = Artifact(id=f"art-{uuid.uuid4().hex[:8]}", kind=kind, filename=filename)
        self.store[a.id] = a
        self._emit("artifact.created", artifact_id=a.id, filename=filename)
        return a

    def run(self, kind: str, filename: str, builder: Callable[[Callable[[], bool]], str],
            cancel: Callable[[], bool] = lambda: False) -> Artifact:
        """create → build → validate → complete. Returns the artifact in its final state.
        `builder(cancel)` produces the content and may check cancel() to stop early."""
        a = self.create(kind, filename)
        a.status = ArtifactStatus.BUILDING
        try:
            a.content = builder(cancel) or ""
        except Exception as e:  # noqa
            a.status, a.error = ArtifactStatus.FAILED, f"builder error: {e}"
            self._emit("error.recoverable", artifact_id=a.id, error=a.error)
            return a
        if cancel():
            a.status, a.error = ArtifactStatus.FAILED, "cancelled"
            self._emit("artifact.updated", artifact_id=a.id, status="cancelled")
            return a                                   # partial state returned honestly
        self._emit("artifact.updated", artifact_id=a.id, bytes=len(a.content))

        ok, err = _validate(kind, a.content)
        if not ok:
            a.status, a.error = ArtifactStatus.FAILED, err
            return a                                   # partial + EXACT failure (§10)
        a.status = ArtifactStatus.VALIDATED
        self._emit("artifact.validated", artifact_id=a.id)
        a.status = ArtifactStatus.COMPLETED
        self._emit("artifact.completed", artifact_id=a.id, filename=a.filename)
        return a

    def regenerate(self, artifact: Artifact, builder: Callable[[Callable[[], bool]], str],
                   cancel: Callable[[], bool] = lambda: False) -> Artifact:
        """Requirements changed mid-build → cancel the old and produce a new version (§17)."""
        artifact.history.append({"version": artifact.version, "status": artifact.status.value})
        new = self.run(artifact.kind, artifact.filename, builder, cancel)
        new.version = artifact.version + 1
        return new
