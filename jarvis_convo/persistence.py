"""
Durable memory + audit log (blueprint §4.3, §11, §13 Phase 4). Makes memory survive restarts
and gives every consequential action an auditable, provenance-carrying record.

DurableMemory persists MemoryItems to a JSON file, auto-saving on every write (commit / forget /
correct). AuditLog is an append-only JSONL of actions with a unique action_id — the trail the
blueprint requires for "log provenance and action IDs for auditability" (§13).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict

from .memory import MemoryItem, MemoryStore


class DurableMemory(MemoryStore):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.load()

    def load(self) -> "DurableMemory":
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    for d in json.load(f).get("items", []):
                        self.items[d["id"]] = MemoryItem(**d)
        except Exception:
            pass
        return self

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": [asdict(i) for i in self.items.values()]}, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # persist on every mutating op
    def commit(self, item):
        r = super().commit(item); self._persist(); return r

    def forget(self, item_id):
        r = super().forget(item_id); self._persist(); return r

    def correct(self, item_id, new_text):
        r = super().correct(item_id, new_text); self._persist(); return r


class AuditLog:
    """Append-only action log (§13). One JSON object per line."""
    def __init__(self, path: str):
        self.path = path

    def append(self, type: str, conversation_id: str = "", **data) -> str:
        action_id = uuid.uuid4().hex
        rec = {"action_id": action_id, "ts": time.time(), "type": type,
               "conversation_id": conversation_id, "data": data}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass
        return action_id

    def tail(self, n: int = 50) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(x) for x in lines if x.strip()]
        except Exception:
            return []
