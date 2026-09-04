"""
obs_timing — per-turn, per-stage latency instrumentation (Phase 2 P1, started early).

Records real spans for each stage of a turn and aggregates p50/p95/p99 so latency is *measured*,
never asserted. Stages captured today (text/tool path): orchestrator, model_call, tools, total_turn.
TTFT, STT, TTS-first-audio, and mouth-to-ear land when the streaming/WebRTC voice path exists
(Phase 2 steps 8-9) — this module already has slots for them so nothing needs re-plumbing.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class TurnTrace:
    """Collect stage durations for one turn. Use as: with trace.span('model_call'): ...."""
    def __init__(self, conversation_id: str = ""):
        self.conversation_id = conversation_id
        self.t0 = time.time()
        self.stages: dict[str, float] = {}
        self._starts: dict[str, float] = {}

    def start(self, stage: str):
        self._starts[stage] = time.time()

    def stop(self, stage: str):
        if stage in self._starts:
            self.stages[stage] = self.stages.get(stage, 0.0) + (time.time() - self._starts.pop(stage)) * 1000.0

    class _Span:
        def __init__(self, tr, stage): self.tr, self.stage = tr, stage
        def __enter__(self): self.tr.start(self.stage); return self
        def __exit__(self, *a): self.tr.stop(self.stage)

    def span(self, stage: str) -> "_Span":
        return TurnTrace._Span(self, stage)

    def finalize(self) -> dict:
        self.stages["total_turn"] = (time.time() - self.t0) * 1000.0
        return dict(self.stages)


class MetricsStore:
    """Rolling window of per-stage samples with percentile reporting."""
    def __init__(self, window: int = 500):
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self.count = 0

    def record(self, stages: dict):
        self.count += 1
        for k, v in stages.items():
            self.samples[k].append(float(v))

    @staticmethod
    def _pct(vals: list, p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        i = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
        return round(s[i], 1)

    def report(self) -> dict:
        out = {"turns": self.count, "stages": {}}
        # informational targets from the spec (not a claim they are met)
        targets = {"total_turn": {"p50": 700, "p95": 1500}}
        for stage, dq in self.samples.items():
            vals = list(dq)
            row = {"n": len(vals), "p50": self._pct(vals, 50), "p95": self._pct(vals, 95),
                   "p99": self._pct(vals, 99)}
            if stage in targets:
                row["target_p50"] = targets[stage]["p50"]
                row["target_p95"] = targets[stage]["p95"]
                row["meets_p50"] = row["p50"] <= targets[stage]["p50"] if vals else None
                row["meets_p95"] = row["p95"] <= targets[stage]["p95"] if vals else None
            out["stages"][stage] = row
        return out


# process-wide store shared by the runtime + exposed via /api/convo/metrics
METRICS = MetricsStore()
