"""Scriptable runtime for workflow tests. Records every spec it was given."""
from __future__ import annotations

import asyncio
from typing import Callable

from .base import ApprovalHandler, EventSink, RunResult, RunSpec


class MockRuntime:
    def __init__(self, script: Callable[[RunSpec], RunResult] | None = None, delay: float = 0,
                 stream: Callable[[RunSpec], list[tuple[str, dict]]] | None = None):
        self.script = script or (lambda s: RunResult("completed", "{}", f"th-{s.run_id}"))
        self.delay = delay
        self.stream = stream                      # events to emit (spread over `delay`) while "working"
        self.specs: list[RunSpec] = []
        self._cancelled: set[str] = set()

    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult:
        self.specs.append(spec)
        await emit("agent.started", {"model": spec.role.model, "provider": spec.role.provider})
        evs = list(self.stream(spec)) if self.stream else []
        steps = int(self.delay * 100)
        every = max(1, steps // (len(evs) + 1))
        if not steps:
            for t, p in evs:
                await emit(t, p)
        for i in range(steps):
            if evs and i % every == every - 1:
                t, p = evs.pop(0)
                await emit(t, p)
            if spec.run_id in self._cancelled:
                res = RunResult("interrupted", "", f"th-{spec.run_id}")
                await emit("agent.finished", {"status": res.status})
                return res
            await asyncio.sleep(0.01)
        res = self.script(spec)
        await emit("agent.finished", {"status": res.status, "error": res.error})
        return res

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)

    async def close(self) -> None:
        pass
