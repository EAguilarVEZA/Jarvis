"""
Permission scopes + connector auth (blueprint §13, Phase 4). Agentic capability is paired with
strict action boundaries: reading and taking external actions are DIFFERENT permission classes,
and a connector write requires an explicitly granted scope — not just a per-turn "yes".

  ScopeStore    grants like "gmail:read" / "gmail:write" / "calendar:write" per principal,
                persisted; grant / revoke / has. This is what the ExecutionController consults.
  ConnectorAuth stores connector tokens ENCRYPTED at rest (via crypto_store when available) and
                hands out only a reference — the token value never lands in a model-visible log
                (§13, §20). Actual decryption happens at API-call time, outside the model path.
"""
from __future__ import annotations

import json
import os
import time


class ScopeStore:
    def __init__(self, path: str = ""):
        self.path = path
        self.grants: dict[str, set[str]] = {}
        self._load()

    def _load(self):
        try:
            if self.path and os.path.exists(self.path):
                d = json.load(open(self.path, encoding="utf-8"))
                self.grants = {k: set(v) for k, v in d.get("grants", {}).items()}
        except Exception:
            pass

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump({"grants": {k: sorted(v) for k, v in self.grants.items()}},
                      open(self.path, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass

    def grant(self, principal: str, scope: str):
        self.grants.setdefault(principal, set()).add(scope); self._save()

    def revoke(self, principal: str, scope: str):
        self.grants.get(principal, set()).discard(scope); self._save()

    def has(self, principal: str, scope: str) -> bool:
        return scope in self.grants.get(principal, set())

    def list(self, principal: str) -> list[str]:
        return sorted(self.grants.get(principal, set()))


class ConnectorAuth:
    """Encrypted-at-rest connector tokens. Never returns or logs the plaintext token — only a
    reference and a 'connected' flag. The real API layer decrypts on demand via crypto_store."""
    def __init__(self, path: str = ""):
        self.path = path
        self._blobs: dict[str, str] = {}   # "principal:connector" -> encrypted blob (or ref)
        self._load()

    def _key(self, principal, connector):
        return f"{principal}:{connector}"

    def _encrypt(self, token: str) -> str:
        try:
            import crypto_store
            for fn in ("encrypt", "seal", "encrypt_str"):
                if hasattr(crypto_store, fn):
                    return "enc:" + getattr(crypto_store, fn)(token)
        except Exception:
            pass
        # fallback: store only a non-reversible reference, NEVER the plaintext (§9, §13)
        import hashlib
        return "ref:" + hashlib.sha256(token.encode()).hexdigest()[:24]

    def _load(self):
        try:
            if self.path and os.path.exists(self.path):
                self._blobs = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            pass

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump(self._blobs, open(self.path, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass

    def store_token(self, principal: str, connector: str, token: str):
        self._blobs[self._key(principal, connector)] = self._encrypt(token)
        self._save()

    def connected(self, principal: str, connector: str) -> bool:
        return self._key(principal, connector) in self._blobs

    def reference(self, principal: str, connector: str) -> str | None:
        # a reference/handle only — safe to log; the value is elsewhere
        blob = self._blobs.get(self._key(principal, connector))
        return None if not blob else f"{connector}#{blob[:8]}"
