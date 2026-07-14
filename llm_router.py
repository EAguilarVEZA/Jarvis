"""Unified multi-LLM harness for JARVIS.

Route agent/transform steps to ANY model provider — Claude, OpenAI, Gemini, or a
local/offline model via Ollama — behind one `complete()` call. Only stdlib is
used (urllib), so there is no vendor SDK to install.

Security model
--------------
Each provider can be marked `local`. Local providers (Ollama on
http://localhost:11434, or any 127.0.0.1 / .internal host) keep every prompt and
completion on your own network — nothing leaves the company perimeter. `is_local`
and `egresses(provider)` let the caller enforce a "no external egress" policy:
run entirely on-prem by only allowing local providers.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.parse
import asyncio
import logging

log = logging.getLogger("llm_router")

_DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "",  # user supplies base_url
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://localhost:11434",
}

_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", ".internal", ".local", "host.docker.internal")


def is_local(provider: dict) -> bool:
    """True if the provider clearly runs on the local machine / private network."""
    if provider.get("local"):
        return True
    if provider.get("type") == "ollama" and not (provider.get("base_url") or "").strip():
        return True
    base = (provider.get("base_url") or _DEFAULT_BASE.get(provider.get("type"), "")).lower()
    return any(h in base for h in _LOCAL_HINTS)


def egresses(provider: dict) -> bool:
    """Whether calling this provider sends data outside the local network."""
    return not is_local(provider)


def build_request(provider: dict, system: str, messages: list, max_tokens: int = 1200):
    """Return (method, url, headers, payload_dict) for the provider. Pure — no network.
    `messages` is a list of {role: 'user'|'assistant', content: str}."""
    ptype = provider.get("type", "openai")
    base = (provider.get("base_url") or _DEFAULT_BASE.get(ptype, "")).rstrip("/")
    key = provider.get("api_key", "")
    model = provider.get("model", "")

    if ptype == "anthropic":
        url = base + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            payload["system"] = system
        return "POST", url, headers, payload

    if ptype in ("openai", "openai_compatible"):
        url = base + "/chat/completions"
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        return "POST", url, headers, payload

    if ptype == "gemini":
        url = base + f"/models/{model}:generateContent?key=" + urllib.parse.quote(key)
        headers = {"Content-Type": "application/json"}
        contents = [{"role": ("model" if m.get("role") == "assistant" else "user"),
                     "parts": [{"text": m.get("content", "")}]} for m in messages]
        payload = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return "POST", url, headers, payload

    if ptype == "ollama":
        url = base + "/api/chat"
        headers = {"Content-Type": "application/json"}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {"model": model, "messages": msgs, "stream": False}
        return "POST", url, headers, payload

    raise ValueError(f"Unknown provider type '{ptype}'.")


def parse_response(ptype: str, data: dict) -> str:
    """Extract the assistant text from a provider's JSON response."""
    try:
        if ptype == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if ptype in ("openai", "openai_compatible"):
            return data["choices"][0]["message"]["content"]
        if ptype == "gemini":
            return data["candidates"][0]["content"]["parts"][0]["text"]
        if ptype == "ollama":
            return data["message"]["content"]
    except Exception:
        return ""
    return ""


async def complete(provider: dict, system: str, messages: list, max_tokens: int = 1200,
                   allow_egress: bool = True, timeout: int = 45) -> dict:
    """Run a completion. Returns {output} or {error}.
    If allow_egress is False, non-local providers are refused (on-prem lockdown)."""
    if not provider:
        return {"error": "No LLM provider selected."}
    if not allow_egress and egresses(provider):
        return {"error": "Blocked: this provider sends data off-network and egress is disabled."}
    try:
        method, url, headers, payload = build_request(provider, system, messages, max_tokens)
    except Exception as e:
        return {"error": str(e)}
    ptype = provider.get("type", "openai")

    def _do():
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, _do)
        text = parse_response(ptype, data)
        return {"output": text}
    except Exception as e:
        return {"error": f"{provider.get('name', ptype)} error: {e}"}
