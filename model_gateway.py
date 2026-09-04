"""
model_gateway — provider-independent model interface (Phase 2 P0).

Martin owns conversation state; providers are stateless components behind a common interface.
AnthropicAdapter is REAL (uses the Anthropic key already present). An OpenAIAdapter /
OpenAIRealtimeAdapter slot in here unchanged once OPENAI_API_KEY + Realtime access are provided
(seam documented at the bottom) — this file ships only adapters that actually reach a real service.

The message/block format is provider-neutral in intent; today it is Anthropic-shaped and the
AnthropicAdapter passes it through. A future OpenAIAdapter translates neutral blocks <-> OpenAI
tool-call format. The runtime never imports a vendor SDK — only this gateway.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import system_llm


@dataclass
class Capabilities:
    streaming_text: bool = True
    tool_calling: bool = True
    structured_output: bool = False
    realtime_audio: bool = False
    cancellation: bool = True
    max_context_tokens: int = 200000
    reasoning_tier: str = "deep"          # "fast" | "balanced" | "deep"
    typical_ttft_ms: int = 500
    input_cost: float = 0.0
    output_cost: float = 0.0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Completion:
    text: str
    tool_calls: list                      # list[ToolCall]
    stop_reason: str
    usage: dict = field(default_factory=dict)
    assistant_blocks: list = field(default_factory=list)   # provider-neutral echo for the next message


class CancelToken:
    def __init__(self):
        self._c = False

    def cancel(self):
        self._c = True

    @property
    def cancelled(self) -> bool:
        return self._c


class ModelGateway(Protocol):
    name: str
    def capabilities(self) -> Capabilities: ...
    async def complete(self, *, system: str, messages: list, tools: list,
                       max_tokens: int = 1500, cancel: "CancelToken | None" = None) -> Completion: ...


class AnthropicAdapter:
    """REAL adapter — reaches the Anthropic Messages API via the app's async client."""
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or os.getenv("JARVIS_MARTIN_MODEL",
                                        os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"))

    def capabilities(self) -> Capabilities:
        return Capabilities(realtime_audio=False, reasoning_tier="deep", typical_ttft_ms=500,
                            max_context_tokens=200000)

    async def complete(self, *, system, messages, tools, max_tokens=1500, cancel=None) -> Completion:
        client = system_llm.anthropic_client(api_key=self.api_key)
        resp = await client.messages.create(model=self.model, max_tokens=max_tokens,
                                             system=system, messages=messages, tools=tools or [])
        text, calls, blocks = "", [], []
        for b in resp.content:
            t = getattr(b, "type", "")
            if t == "text":
                text += b.text
                blocks.append({"type": "text", "text": b.text})
            elif t == "tool_use":
                calls.append(ToolCall(b.id, b.name, b.input))
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        usage = {}
        u = getattr(resp, "usage", None)
        if u:
            usage = {"input_tokens": getattr(u, "input_tokens", 0),
                     "output_tokens": getattr(u, "output_tokens", 0)}
        return Completion(text.strip(), calls, getattr(resp, "stop_reason", ""), usage, blocks)


class OpenAIAdapter:
    """REAL adapter — reaches the OpenAI Chat Completions API (httpx, no SDK dependency).

    Translates Martin's canonical (Anthropic-shaped) message/tool format <-> OpenAI on every call,
    so the transcript stays provider-neutral and a mid-thread switch never loses context."""
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")

    def capabilities(self) -> Capabilities:
        return Capabilities(realtime_audio=False, reasoning_tier="balanced",
                            typical_ttft_ms=400, max_context_tokens=128000)

    def _to_openai(self, system, messages, tools):
        oai = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                oai.append({"role": m["role"], "content": c}); continue
            if m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                tcs = [{"id": b["id"], "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                       for b in c if b.get("type") == "tool_use"]
                msg = {"role": "assistant", "content": text or None}
                if tcs:
                    msg["tool_calls"] = tcs
                oai.append(msg)
            else:                                   # user turn carrying tool_result / text blocks
                for b in c:
                    if b.get("type") == "tool_result":
                        content = b.get("content", "")
                        oai.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                    "content": content if isinstance(content, str) else json.dumps(content)})
                    elif b.get("type") == "text":
                        oai.append({"role": "user", "content": b.get("text", "")})
        otools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                   "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
                  for t in (tools or [])]
        return oai, otools

    async def complete(self, *, system, messages, tools, max_tokens=1500, cancel=None) -> Completion:
        oai, otools = self._to_openai(system, messages, tools)
        payload = {"model": self.model, "messages": oai, "max_tokens": max_tokens}
        if otools:
            payload["tools"] = otools
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason", "")
        text = msg.get("content") or ""
        calls, blocks = [], []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in (msg.get("tool_calls") or []):
            fn = tc["function"]
            try:
                inp = json.loads(fn.get("arguments") or "{}")
            except Exception:
                inp = {}
            calls.append(ToolCall(tc["id"], fn["name"], inp))
            blocks.append({"type": "tool_use", "id": tc["id"], "name": fn["name"], "input": inp})
        stop = "tool_use" if (finish == "tool_calls" or calls) else "end_turn"
        u = data.get("usage", {})
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        return Completion(text.strip(), calls, stop, usage, blocks)


def build_default_gateways() -> list:
    """Real adapters present in this environment. Anthropic is primary when present; OpenAI is the
    second provider (enables switching + real fallback). Order defines default + fallback target."""
    gws = []
    if os.getenv("ANTHROPIC_API_KEY"):
        gws.append(AnthropicAdapter())
    if os.getenv("OPENAI_API_KEY"):
        gws.append(OpenAIAdapter())
    return gws or [AnthropicAdapter()]


class Router:
    """Provider selection + real fallback. Martin owns the transcript, so choose()/fallback() swap
    providers mid-thread without losing state."""

    def __init__(self, gateways: list | None = None):
        self.gateways = gateways if gateways is not None else build_default_gateways()
        self._by = {g.name: g for g in self.gateways}

    def names(self) -> list:
        return [g.name for g in self.gateways]

    def by_name(self, name: str):
        return self._by.get(name)

    def choose(self, need: dict | None = None) -> ModelGateway:
        need = need or {}
        if need.get("provider") and self._by.get(need["provider"]):
            return self._by[need["provider"]]
        if need.get("realtime"):
            for g in self.gateways:
                if g.capabilities().realtime_audio:
                    return g
        return self.gateways[0]

    def fallback(self, failed) -> "ModelGateway | None":
        for g in self.gateways:
            if g is not failed:
                return g
        return None
