"""
active_context — the ActiveContextGraph (Phase 2 P0).

Extends WorkingMemory beyond a rolling transcript into structured, referenceable state: entities,
referents (phrase -> entity/action), tasks (commitments + their workflow), constraints, and watch
targets. This is what lets Martin resolve "my flight", "the plane", "check it" after unrelated topics.

It COMPOSES the existing WorkingMemory (keeping the spoken-boundary / correction rules intact) and
adds the graph on top. Aviation and other domains populate the graph; resolution is domain-agnostic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from jarvis_convo.state import WorkingMemory


@dataclass
class Entity:
    id: str
    type: str                       # e.g. "flight", "airport", "campaign", "person"
    label: str = ""
    attrs: dict = field(default_factory=dict)
    first_seen: float = field(default_factory=time.time)
    last_ref: float = field(default_factory=time.time)
    source_event: str = ""


@dataclass
class Referent:
    phrase: str                     # "my flight", "the plane", "check it"
    target_id: str                  # entity id or task id
    kind: str = "entity"            # "entity" | "action"


@dataclass
class Task:
    id: str
    goal: str
    status: str = "active"          # active | done | cancelled
    constraints: list = field(default_factory=list)
    workflow: str | None = None     # name of a rerunnable workflow ("flight_risk")
    entity_ids: list = field(default_factory=list)


@dataclass
class WatchTarget:
    id: str
    factor: str                     # "MCO weather", "FAA ATL constraints", "inbound aircraft"
    entity_id: str = ""
    condition: str | None = None


class ActiveContextGraph:
    def __init__(self, conversation_id: str = "", max_moves: int = 20):
        self.conversation_id = conversation_id
        self.working = WorkingMemory(max_moves=max_moves)     # transcript + corrections (reused)
        self.entities: dict[str, Entity] = {}
        self.referents: list[Referent] = []
        self.tasks: list[Task] = []
        self.constraints: list[str] = []
        self.watch: list[WatchTarget] = []

    # ── transcript passthrough (preserve spoken-boundary semantics) ──
    def commit_user_turn(self, text: str):
        self.working.commit_user_turn(text)

    def commit_assistant_spoken(self, spoken_text: str):
        self.working.commit_assistant_spoken(spoken_text)

    def add_correction(self, text: str):
        self.working.add_correction(text)

    def recent(self) -> list:
        return self.working.authoritative_context()

    # ── graph mutation ──
    def note_entity(self, id: str, type: str, label: str = "", **attrs) -> Entity:
        e = self.entities.get(id)
        if e:
            e.attrs.update(attrs); e.last_ref = time.time()
            if label:
                e.label = label
        else:
            e = Entity(id=id, type=type, label=label, attrs=attrs)
            self.entities[id] = e
        return e

    def bind_referent(self, phrase: str, target_id: str, kind: str = "entity"):
        self.referents = [r for r in self.referents if r.phrase.lower() != phrase.lower()]
        self.referents.append(Referent(phrase.lower(), target_id, kind))

    def add_task(self, id: str, goal: str, workflow: str | None = None,
                 constraints: list | None = None, entity_ids: list | None = None) -> Task:
        t = Task(id=id, goal=goal, workflow=workflow,
                 constraints=constraints or [], entity_ids=entity_ids or [])
        self.tasks = [x for x in self.tasks if x.id != id] + [t]
        return t

    def add_watch(self, id: str, factor: str, entity_id: str = "", condition: str | None = None):
        self.watch = [w for w in self.watch if w.id != id]
        self.watch.append(WatchTarget(id, factor, entity_id, condition))

    def add_constraint(self, c: str):
        if c and c not in self.constraints:
            self.constraints.append(c)

    # ── resolution ──
    def resolve(self, phrase: str):
        """phrase -> Entity or Task via bound referents (survives topic changes)."""
        p = (phrase or "").lower().strip()
        for r in self.referents:
            if r.phrase in p or p in r.phrase:
                return self.entities.get(r.target_id) or self._task(r.target_id)
        # fall back to most recently referenced entity of a guessed type
        return None

    def rerun_target(self, phrase: str) -> "Task | None":
        """'check it' / 'run it again' -> the active task/workflow to rerun."""
        p = (phrase or "").lower()
        for r in self.referents:
            if r.kind == "action" and (r.phrase in p or p in r.phrase):
                t = self._task(r.target_id)
                if t:
                    return t
        active = [t for t in self.tasks if t.status == "active" and t.workflow]
        return active[-1] if active else None

    def _task(self, tid: str) -> "Task | None":
        for t in self.tasks:
            if t.id == tid:
                return t
        return None

    def snapshot(self) -> dict:
        """Compact, human-readable view for prompt context (never drops a commitment)."""
        return {
            "entities": [{"id": e.id, "type": e.type, "label": e.label, "attrs": e.attrs}
                         for e in self.entities.values()],
            "referents": [{"phrase": r.phrase, "target": r.target_id, "kind": r.kind} for r in self.referents],
            "tasks": [{"id": t.id, "goal": t.goal, "status": t.status, "workflow": t.workflow,
                       "constraints": t.constraints} for t in self.tasks],
            "constraints": list(self.constraints),
            "watch": [{"factor": w.factor, "entity": w.entity_id} for w in self.watch],
        }

    def context_lines(self) -> list:
        """Render active state for the model prompt (only if non-empty)."""
        lines = []
        if self.entities:
            lines.append("Active entities: " + "; ".join(
                f"{e.label or e.id} ({e.type})" for e in self.entities.values()))
        if self.tasks:
            act = [t for t in self.tasks if t.status == "active"]
            if act:
                lines.append("Active tasks: " + "; ".join(t.goal for t in act))
        if self.watch:
            lines.append("Watching: " + ", ".join(w.factor for w in self.watch))
        if self.constraints:
            lines.append("Constraints: " + "; ".join(self.constraints))
        return lines
