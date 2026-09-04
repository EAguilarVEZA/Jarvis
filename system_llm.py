"""system_llm — the single source of truth for WHICH model runs the system.

The multi-LLM harness (llm_router.py) can talk to Anthropic, OpenAI, Gemini, or a
local Ollama model. This module decides which of the configured providers is the
*active system brain* — the one that powers Martin's chat and the KTX / semantic
data-question planner — and resolves it with a safe Anthropic fallback so the app
never goes dark if the selected provider is misconfigured.

Storage: the same llm_providers.json the Agent-Studio harness already uses
(plaintext JSON, list under "providers"), plus a top-level "active_id".

Nothing here reaches the network — it only reads/writes the JSON and hands a
provider dict to llm_router.complete().
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

log = logging.getLogger("system_llm")


# ── error classification + a circuit breaker so a down model can't rack up charges ──
def classify_error(err) -> str:
    """Bucket a provider error string into a clear category."""
    e = (err or "").lower()
    if "503" in e or "service unavailable" in e or "overloaded" in e:
        return "unavailable"
    if "timed out" in e or "timeout" in e or "read operation" in e:
        return "timeout"
    if "429" in e or "too many" in e or "resource_exhausted" in e or "quota" in e or "rate limit" in e:
        return "rate_limit"
    if "401" in e or "403" in e or "unauthorized" in e or "forbidden" in e or "api key" in e or "invalid key" in e:
        return "auth"
    if "404" in e or "not found" in e or "does not exist" in e:
        return "not_found"
    if "400" in e or "invalid_argument" in e or "invalid argument" in e or "bad request" in e:
        return "bad_request"
    return "error"


ERROR_LABEL = {
    "unavailable": "the provider's service is temporarily down (HTTP 503) — not billed",
    "timeout": "the model didn't respond in time — may bill for partial work",
    "rate_limit": "hit the provider's rate or daily quota limit",
    "auth": "the API key was rejected — check the key",
    "not_found": "the model name wasn't found — wrong model id",
    "bad_request": "the model rejected a request parameter (e.g. an unsupported setting)",
    "error": "an unexpected error",
    "empty": "the model returned an empty response",
}

# Circuit breaker: after a few consecutive failures on a provider, stop calling it
# for a short cooldown. This is what prevents a brief (10–20 calls) from hammering a
# down model and charging you for every timeout.
_breaker = {}          # provider key -> {"fails": int, "until": epoch, "last": category}
_BREAK_AFTER = 2       # consecutive failures before the breaker trips
_BREAK_COOLDOWN = 90   # seconds to stay tripped (skip the provider entirely)


def _bkey(prov):
    return prov.get("id") or (prov.get("type", "") + ":" + prov.get("model", ""))


def breaker_open(prov):
    b = _breaker.get(_bkey(prov))
    return bool(b and b.get("until", 0) > time.time())


def breaker_status(prov):
    b = _breaker.get(_bkey(prov)) or {}
    open_ = b.get("until", 0) > time.time()
    return {"open": open_, "fails": b.get("fails", 0),
            "cooldown_left": max(0, int(b.get("until", 0) - time.time())) if open_ else 0,
            "last": b.get("last")}


def _breaker_record(prov, ok, category=None):
    k = _bkey(prov)
    if ok:
        _breaker.pop(k, None)
        return
    b = _breaker.setdefault(k, {"fails": 0, "until": 0, "last": None})
    b["fails"] += 1
    b["last"] = category
    if b["fails"] >= _BREAK_AFTER:
        b["until"] = time.time() + _BREAK_COOLDOWN


def reset_breaker(prov=None):
    if prov is None:
        _breaker.clear()
    else:
        _breaker.pop(_bkey(prov), None)


async def probe_active(anthropic_key=None, timeout=12) -> dict:
    """One cheap call to the ACTIVE model to check it's alive BEFORE running an
    expensive multi-call brief. Returns {ok} or {ok:False, category, label, name}.
    For the Claude default (which uses the SDK) it skips the probe."""
    p = get_active_provider()
    if not p or p.get("type") == "anthropic":
        return {"ok": True, "skip": True}
    if breaker_open(p):
        st = breaker_status(p)
        cat = st.get("last") or "unavailable"
        return {"ok": False, "category": cat, "label": ERROR_LABEL.get(cat),
                "name": p.get("name"), "cooldown_left": st.get("cooldown_left")}
    try:
        import llm_router
        r = await llm_router.complete(p, "Reply with the single word OK.",
                                      [{"role": "user", "content": "OK"}],
                                      max_tokens=5, timeout=timeout)
    except Exception as e:
        r = {"error": str(e)}
    if r.get("error"):
        cat = classify_error(r.get("error"))
        _breaker_record(p, False, cat)
        return {"ok": False, "category": cat, "label": ERROR_LABEL.get(cat),
                "name": p.get("name"), "error": r.get("error")}
    _breaker_record(p, True)
    return {"ok": True, "name": p.get("name")}


def active_health() -> dict:
    """Current health of the active model (from the circuit breaker), for the UI/logs."""
    p = get_active_provider()
    if not p:
        return {"name": "Claude (default)", "circuit_open": False, "last_error": None}
    st = breaker_status(p)
    cat = st.get("last")
    return {"name": p.get("name"), "model": p.get("model"),
            "circuit_open": st.get("open"), "cooldown_left": st.get("cooldown_left"),
            "last_error": cat, "message": ERROR_LABEL.get(cat) if cat else None}

_LLM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_providers.json")


# ── storage ──────────────────────────────────────────────────────────────────
def _read() -> dict:
    try:
        with open(_LLM_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {"providers": []}
    except Exception:
        return {"providers": []}


def _write(d: dict) -> None:
    tmp = _LLM_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, _LLM_PATH)


def list_providers() -> list:
    return _read().get("providers", []) or []


def get_active_id():
    return _read().get("active_id")


def set_active(pid):
    d = _read()
    if pid and not any(p.get("id") == pid for p in d.get("providers", [])):
        return False
    d["active_id"] = pid or None
    _write(d)
    return True


def get_active_provider():
    d = _read()
    aid = d.get("active_id")
    if not aid:
        return None
    for p in d.get("providers", []):
        if p.get("id") == aid:
            return p
    return None


# ── one-time seed: offer a local Ollama option out of the box ─────────────────
# Ready-to-use FREE model options seeded into the picker. Each is an OPTION only —
# the admin adds the (free) key and clicks "Use this". Matched by a stable seed tag
# so we never duplicate them.
_SEED_PROVIDERS = [
    {
        "seed": "ollama-local",
        "name": "Martin Local (Ollama)",
        "type": "ollama", "base_url": "", "api_key": "",
        "model": "qwen2.5:32b-instruct",       # fully private/on-device; weaker at long briefs
    },
    {
        "seed": "groq-free",
        "name": "Groq — GPT-OSS 120B (free, fast)",
        "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",                          # free key from console.groq.com (no card)
        "model": "openai/gpt-oss-120b",         # 120B open model — great at JSON briefs, ~500 tok/s
    },
    {
        "seed": "gemini-free",
        "name": "Google Gemini 3.5 Flash (free)",
        "type": "gemini", "base_url": "", "api_key": "",  # free key from aistudio.google.com
        "model": "gemini-3.5-flash",            # free tier (~1500 req/day), clean JSON, huge context
    },
]


def ensure_seed() -> None:
    """Make sure the ready-to-use free provider OPTIONS exist in the picker.
    Does NOT make any of them active — the admin adds a (free) key and picks one."""
    d = _read()
    provs = d.setdefault("providers", [])
    changed = False
    for sp in _SEED_PROVIDERS:
        # already present if a provider carries this seed tag, or matches type+model
        exists = any(p.get("seed") == sp["seed"] or
                     (p.get("type") == sp["type"] and p.get("model") == sp["model"])
                     for p in provs)
        if not exists:
            provs.append({"id": uuid.uuid4().hex[:12], "seed": sp["seed"],
                          "name": sp["name"], "type": sp["type"],
                          "base_url": sp["base_url"], "api_key": sp["api_key"],
                          "model": sp["model"]})
            changed = True
    if changed:
        _write(d)


# ── resolution: the model the SYSTEM should use right now ─────────────────────
def resolve(anthropic_key: str | None = None,
            anthropic_model: str = "claude-haiku-4-5-20251001",
            name: str = "Claude") -> dict | None:
    """Return the provider dict the system should call.

    Priority:
      1. the admin-selected active provider (filling the Anthropic key from env
         if the active provider is Anthropic and left the key blank);
      2. otherwise the Anthropic fallback built from the env key.
    Returns None only if there is no usable provider at all.
    """
    p = get_active_provider()
    if p:
        ptype = p.get("type", "")
        if ptype == "anthropic" and not (p.get("api_key") or "").strip() and anthropic_key:
            p = {**p, "api_key": anthropic_key}
            if not p.get("model"):
                p["model"] = anthropic_model
        # A provider is usable if it's local (Ollama/private) or carries a key.
        try:
            import llm_router
            local = llm_router.is_local(p)
        except Exception:
            local = (ptype == "ollama")
        if local or (p.get("api_key") or "").strip():
            return p
    if anthropic_key:
        return {"type": "anthropic", "api_key": anthropic_key,
                "model": anthropic_model, "name": name}
    return None


# ── drop-in Anthropic proxy: makes the WHOLE app follow the selected model ────
# Many modules call `anthropic.AsyncAnthropic(...).messages.create(...)` directly.
# Swapping that constructor for `system_llm.anthropic_client(...)` means those
# features follow the admin's active system model too: when a non-Anthropic model
# (e.g. local Ollama) is selected, calls are transparently routed through the
# harness; when Anthropic is active/default, the real SDK is used unchanged.
class _Block:
    __slots__ = ("type", "text")
    def __init__(self, text):
        self.type = "text"; self.text = text

class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.role = "assistant"

def _flatten(messages):
    conv = []
    for m in (messages or []):
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
        conv.append({"role": ("assistant" if m.get("role") == "assistant" else "user"),
                     "content": str(c or "")})
    return conv

class _Messages:
    def __init__(self, owner):
        self._owner = owner
    async def create(self, model=None, max_tokens=1024, system=None, messages=None, **kw):
        act = get_active_provider()
        if act and act.get("type") and act.get("type") != "anthropic":
            # Route to the selected system model (e.g. Gemini/Groq/Ollama). If it
            # errors (rate limit 429, quota, outage, empty), automatically fall back
            # to the next configured model so one free tier's cap can't take Martin
            # down. Tools/streaming are dropped — this is a text completion.
            import llm_router
            conv = _flatten(messages)
            mt = max_tokens or 1024
            # Failover is OFF by default and fully admin-controlled. When off, we call
            # only the active model (no automatic fallback). When on, we try the
            # active model, then the admin's chosen chain in the order they set.
            fo = get_failover()
            chain = [act]
            if fo.get("enabled"):
                by_id = {p.get("id"): p for p in list_providers()}
                for cid in fo.get("chain", []):
                    p = by_id.get(cid)
                    if p and p.get("id") != act.get("id"):
                        chain.append(p)
            for prov in chain:
                # Circuit breaker: if this model just failed repeatedly, skip it
                # entirely (no network call, no charge) until its cooldown passes.
                if breaker_open(prov):
                    st = breaker_status(prov)
                    log.warning("SKIP '%s' — circuit open (%s), %ss cooldown left; not calling to avoid charges",
                                prov.get("name"), st.get("last"), st.get("cooldown_left"))
                    continue
                # Shorter timeout so a hung provider fails fast instead of waiting 45s.
                r = await llm_router.complete(prov, system or "", conv, max_tokens=mt, timeout=25)
                out = "" if r.get("error") else (r.get("output") or "")
                if out.strip():
                    _breaker_record(prov, True)
                    if prov is not act:
                        log.warning("system model '%s' unavailable — answered via fallback '%s'",
                                    act.get("name"), prov.get("name"))
                    return _Msg(out)
                cat = classify_error(r.get("error")) if r.get("error") else "empty"
                _breaker_record(prov, False, cat)
                st = breaker_status(prov)
                log.warning("provider '%s' (%s) failed [%s: %s]%s: %s",
                            prov.get("name"), prov.get("model"), cat, ERROR_LABEL.get(cat, ""),
                            " → circuit TRIPPED, pausing this model" if st.get("open") else "",
                            r.get("error") or "empty response")
            return _Msg("")
        real = self._owner._real_messages()
        return await real.create(model=model, max_tokens=max_tokens,
                                 system=system, messages=messages, **kw)
    def __getattr__(self, name):        # passthrough for anything else (e.g. .stream)
        return getattr(self._owner._real_messages(), name)

class _AnthropicProxy:
    def __init__(self, api_key=None):
        self._api_key = api_key
        self._real = None                 # lazily created only if Anthropic is actually used
        self.messages = _Messages(self)
    def _real_client(self):
        if self._real is None:
            import anthropic
            self._real = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._real
    def _real_messages(self):
        return self._real_client().messages
    def __getattr__(self, name):
        return getattr(self._real_client(), name)

def anthropic_client(api_key=None):
    """Drop-in replacement for anthropic.AsyncAnthropic that follows the active system model.
    When a non-Anthropic model (e.g. local Ollama) is active, the Anthropic SDK is never imported."""
    return _AnthropicProxy(api_key=api_key)


# ── admin-controlled failover (OFF by default) ───────────────────────────────
def get_failover() -> dict:
    """Whether automatic failover is on, and the admin-ordered chain of provider ids."""
    d = _read()
    return {"enabled": bool(d.get("failover_enabled")),
            "chain": list(d.get("failover_chain") or [])}


def set_failover(enabled=None, chain=None) -> dict:
    """Update the failover toggle and/or the ordered chain (list of provider ids)."""
    d = _read()
    if enabled is not None:
        d["failover_enabled"] = bool(enabled)
    if chain is not None:
        valid = {p.get("id") for p in d.get("providers", [])}
        seen, ordered = set(), []
        for cid in chain:
            if cid in valid and cid not in seen:
                ordered.append(cid); seen.add(cid)
        d["failover_chain"] = ordered
    _write(d)
    return {"enabled": bool(d.get("failover_enabled")),
            "chain": list(d.get("failover_chain") or [])}


def suggest_chain(exclude_id=None) -> list:
    """A sensible DEFAULT order (cloud-with-key first, then local) the admin can
    accept or reorder. Only a suggestion — not used unless the admin turns failover on."""
    return [p.get("id") for p in _fallback_chain(exclude_id=exclude_id)]


def _fallback_chain(exclude_id=None) -> list:
    """Ordered list of OTHER usable providers — used only to SUGGEST a default chain
    in the admin UI (cloud-with-key first, then local, then an Anthropic env fallback)."""
    try:
        import llm_router
        _is_local = llm_router.is_local
    except Exception:
        _is_local = lambda p: p.get("type") == "ollama"
    cloud, local = [], []
    for p in list_providers():
        if exclude_id and p.get("id") == exclude_id:
            continue
        if _is_local(p):
            local.append(p)
        elif (p.get("api_key") or "").strip():
            cloud.append(p)
    chain = cloud + local
    key = os.getenv("ANTHROPIC_API_KEY", "") or None
    if key and not any(p.get("type") == "anthropic" for p in chain):
        chain.append({"type": "anthropic", "api_key": key,
                      "model": os.getenv("JARVIS_MARTIN_MODEL", "claude-haiku-4-5-20251001"),
                      "name": "Claude (fallback)"})
    return chain


def active_summary() -> dict:
    """Small, secret-free description of what's running the system."""
    p = get_active_provider()
    if not p:
        return {"active_id": None, "name": "Claude (default)", "type": "anthropic",
                "model": os.getenv("JARVIS_MARTIN_MODEL",
                                   os.getenv("JARVIS_ASSISTANT_MODEL", "claude-haiku-4-5-20251001")),
                "local": False, "is_default": True}
    try:
        import llm_router
        local = llm_router.is_local(p)
    except Exception:
        local = (p.get("type") == "ollama")
    return {"active_id": p.get("id"), "name": p.get("name"), "type": p.get("type"),
            "model": p.get("model"), "local": local, "is_default": False}
