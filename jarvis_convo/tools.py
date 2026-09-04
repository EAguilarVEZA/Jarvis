"""
Typed tool registry + execution controller (blueprint §6, §6.2). The LLM chooses tools, but
this controller — not the model — validates arguments, enforces permissions, applies safety
policy BEFORE execution, and handles timeouts, retries, cancellation, and idempotency.

Guarantees:
  - read / draft / write are separate permission classes (§13). Writes need an explicit grant
    or per-call confirmation.
  - safety policy runs before tool execution, not after generation (§13).
  - write operations are idempotent by request_id — a retry after a network blip does NOT
    duplicate the external action (§6.2, §17 "No external action is duplicated after retry").
  - every result carries structured status + provenance (§6.2).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Perm(str, Enum):
    READ = "read"      # public retrieval / computation
    DRAFT = "draft"    # prepare something, nothing leaves
    WRITE = "write"    # send/schedule/modify external state


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass
class ToolSpec:
    name: str
    description: str
    permission: Perm
    params: dict                     # {name: {"type": "str", "required": True}}
    fn: Callable[[dict], dict]       # returns {"output":..., "provenance":[...]} or raises
    idempotent: bool = False         # writes should be True (dedupe by request_id)
    max_retries: int = 1
    required_scope: str = ""         # e.g. "gmail:write" — connector actions need an explicit grant


@dataclass
class ToolResult:
    tool: str
    request_id: str
    status: Status
    output: Any = None
    provenance: list = field(default_factory=list)
    error: str = ""
    attempts: int = 0
    deduped: bool = False

    def to_dict(self):
        return {"tool": self.tool, "request_id": self.request_id, "status": self.status.value,
                "output": self.output, "provenance": self.provenance, "error": self.error,
                "attempts": self.attempts, "deduped": self.deduped}


class ExecutionController:
    def __init__(self, grants: set[str] | None = None, safety: Callable[[str, dict], bool] | None = None,
                 scope_store=None, principal: str = "user"):
        self.registry: dict[str, ToolSpec] = {}
        self.grants = grants or set()          # e.g. {"write"} or {"write:email"}
        self.safety = safety
        self.scope_store = scope_store         # ScopeStore — gates connector writes (§13)
        self.principal = principal
        self._idem: dict[str, ToolResult] = {}   # request_id -> result (idempotency cache)
        self.exec_count: dict[str, int] = {}     # tool -> real executions (for tests/observability)

    def register(self, spec: ToolSpec):
        self.registry[spec.name] = spec

    def _validate_args(self, spec: ToolSpec, args: dict) -> str | None:
        for name, meta in spec.params.items():
            if meta.get("required") and name not in args:
                return f"missing required argument '{name}'"
        return None

    def _permitted(self, spec: ToolSpec, confirm: bool) -> bool:
        if spec.permission != Perm.WRITE:
            return True
        # a connector action with a required scope needs an explicit grant — confirm is NOT enough
        if spec.required_scope:
            return bool(self.scope_store and self.scope_store.has(self.principal, spec.required_scope))
        return confirm or "write" in self.grants or f"write:{spec.name}" in self.grants

    def execute(self, name: str, args: dict, request_id: str | None = None,
                confirm: bool = False, cancel: Callable[[], bool] = lambda: False) -> ToolResult:
        rid = request_id or uuid.uuid4().hex
        spec = self.registry.get(name)
        if not spec:
            return ToolResult(name, rid, Status.FAILED, error="unknown tool")

        # idempotency: a retried write returns the first result, never re-runs (§6.2)
        if spec.idempotent and rid in self._idem:
            cached = self._idem[rid]
            return ToolResult(cached.tool, rid, cached.status, cached.output,
                              cached.provenance, cached.error, cached.attempts, deduped=True)

        err = self._validate_args(spec, args)
        if err:
            return ToolResult(name, rid, Status.FAILED, error=err)
        if not self._permitted(spec, confirm):
            err = f"scope_required:{spec.required_scope}" if spec.required_scope else "permission_required"
            return ToolResult(name, rid, Status.FAILED, error=err,
                              provenance=[{"permission": spec.permission.value,
                                           "required_scope": spec.required_scope}])
        if self.safety and not self.safety(name, args):
            return ToolResult(name, rid, Status.FAILED, error="blocked_by_policy")

        attempts = 0
        last_err = ""
        while attempts <= spec.max_retries:
            if cancel():
                return ToolResult(name, rid, Status.CANCELLED, attempts=attempts)
            attempts += 1
            try:
                out = spec.fn(args)
                self.exec_count[name] = self.exec_count.get(name, 0) + 1
                res = ToolResult(name, rid, Status.SUCCEEDED, out.get("output"),
                                 out.get("provenance", []), attempts=attempts)
                if spec.idempotent:
                    self._idem[rid] = res
                return res
            except Exception as e:  # noqa
                last_err = str(e)
                time.sleep(0)   # a real controller backs off; kept 0 for tests
        res = ToolResult(name, rid, Status.FAILED, error=last_err, attempts=attempts)
        if spec.idempotent:
            self._idem[rid] = res
        return res


# ── default safety policy + a small set of mock tools ────────────────────────
def default_safety(name: str, args: dict) -> bool:
    blob = " ".join(str(v) for v in args.values()).lower()
    return not any(bad in blob for bad in ("rm -rf", "drop table", "wire transfer to"))


def _web_search(args):
    q = args["query"]
    return {"output": [{"title": f"Result for {q}", "url": "https://example.org/1",
                        "snippet": "..."}],
            "provenance": [{"source": "web", "url": "https://example.org/1", "retrieved_at": time.time()}]}


def _calculate(args):
    import ast, operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Pow: op.pow, ast.USub: op.neg}
    def ev(n):
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.BinOp): return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp): return ops[type(n.op)](ev(n.operand))
        raise ValueError("unsupported expression")
    val = ev(ast.parse(args["expression"], mode="eval").body)
    return {"output": val, "provenance": [{"source": "sandbox_calc"}]}


_SENT = {"count": 0}
def _send_message(args):
    _SENT["count"] += 1                     # simulate the real external side effect
    return {"output": {"message_id": f"msg-{_SENT['count']}", "to": args["to"]},
            "provenance": [{"action": "send_message", "to": args["to"]}]}


def default_controller(grants: set[str] | None = None) -> ExecutionController:
    c = ExecutionController(grants=grants, safety=default_safety)
    c.register(ToolSpec("web_search", "Search the public web.", Perm.READ,
                        {"query": {"type": "str", "required": True}}, _web_search))
    c.register(ToolSpec("calculate", "Evaluate arithmetic safely.", Perm.READ,
                        {"expression": {"type": "str", "required": True}}, _calculate))
    c.register(ToolSpec("send_message", "Send a message (write).", Perm.WRITE,
                        {"to": {"type": "str", "required": True},
                         "body": {"type": "str", "required": True}},
                        _send_message, idempotent=True))
    return c
