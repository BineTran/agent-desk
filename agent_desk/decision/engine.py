"""DecisionPipeline: rules -> configured engine (plugin) -> fallback (main | user). Every outcome is recorded."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..config.schema import Config, Point
from ..contracts import ControlDecision, now
from ..plugins import Registry
from . import rules
from .engines import EngineCtx, EngineError, EngineResult

RETRY_OPTIONS = {"retry": "another attempt is likely to make progress (the failure is about the code or the approach)",
                 "replan": "the plan itself is wrong or incomplete; the task needs to be rethought",
                 "stop": "no attempt can succeed without a human (impossible requirement, missing access, unrecoverable state)"}


@dataclass
class Verdict:
    point: str
    choice: str
    engine: str                       # rule | llm:haiku | jev | main | user
    confidence: float | None = None
    distribution: dict[str, float] = field(default_factory=dict)
    sharp: bool | None = None
    reason: str = ""


MainPick = Callable[[str, dict, dict[str, str]], Awaitable[str]]


class DecisionPipeline:
    def __init__(self, cfg: Config, reg: Registry, ctx: EngineCtx, record: Callable[[ControlDecision, str | None], Awaitable[None]],
                 main_pick: MainPick | None = None, repo: Path | None = None):
        self.cfg, self.reg, self.ctx, self._record, self.main_pick, self.repo = cfg, reg, ctx, record, main_pick, repo
        self._engines: dict[tuple, object] = {}

    def point(self, name: str) -> Point:
        return self.cfg.decision.points.get(name) or Point()

    def _engine(self, engine: str, provider: str | None, model: str | None):
        key = (engine, provider, model)
        if key not in self._engines:
            plug = self.reg.get("decision", engine)
            prov = self.cfg.providers.get(provider) if provider else None
            self._engines[key] = plug.factory(self.ctx, provider, model, prov)
        return self._engines[key]

    async def _log(self, v: Verdict, task_id: str | None, state: dict, engine_label: str | None = None) -> None:
        await self._record(ControlDecision(type=v.point, engine=engine_label or self._kind(v.engine), selected=v.choice, confidence=v.confidence,
                                           distribution=v.distribution, sharp=v.sharp, input_snapshot={"state": state, "reason": v.reason, "engine": v.engine}), task_id)

    @staticmethod
    def _kind(engine: str) -> str:
        return engine if engine in ("rule", "main", "user") else ("jev" if engine == "jev" else "llm")

    async def _consult(self, name: str, state: dict, options: dict[str, str]) -> Verdict | None:
        """Ask the configured engine. None = no usable sharp answer (caller falls back)."""
        pt = self.point(name)
        if pt.engine == "none":
            return None
        if pt.shadow:
            asyncio.create_task(self._shadow(name, pt, state, options))
        try:
            res: EngineResult = await self._engine(pt.engine, pt.provider, pt.model).decide(name, state, options)
        except Exception as e:                                   # unavailable engine must never stop the session
            await self._log(Verdict(name, "-", "rule", reason=f"engine {pt.engine} unavailable: {type(e).__name__}: {e}"[:300]), None, state, "rule")
            return None
        top = max(res.probabilities, key=res.probabilities.get)
        sharp = res.choice == top and res.confidence >= pt.sharp_threshold
        return Verdict(name, res.choice, res.engine, res.confidence, res.probabilities, sharp, res.reason)

    async def _shadow(self, name: str, pt: Point, state: dict, options: dict[str, str]) -> None:
        sh = pt.shadow or {}
        try:
            res = await self._engine(sh["engine"], sh.get("provider"), sh.get("model")).decide(name, state, options)
            await self._record(ControlDecision(type=name, engine="jev" if res.engine == "jev" else "llm", selected=res.choice, confidence=res.confidence,
                                               distribution=res.probabilities, sharp=None, input_snapshot={"shadow": True, "engine": res.engine}), None)
        except Exception:
            pass

    async def _fallback(self, name: str, state: dict, options: dict[str, str], safe: str) -> Verdict:
        if self.point(name).fallback == "main" and self.main_pick:
            try:
                return Verdict(name, await self.main_pick(name, state, options), "main", reason="engine not sharp -> main")
            except Exception as e:
                return Verdict(name, safe, "rule", reason=f"main fallback failed ({type(e).__name__}); safe default")
        return Verdict(name, safe, "user" if self.point(name).fallback == "user" else "rule", reason="no engine answer; safe default")

    # ---------------- points ----------------
    async def retry_or_stop(self, state: dict, task_id: str | None = None) -> Verdict:
        """state: task, attempt, max_attempts, fingerprints[], failure_tail[], summary, cwd"""
        tail = state.get("failure_tail") or []
        env = rules.environment_error(tail + [state.get("summary", "")], self.repo)
        fps = state.get("fingerprints") or []
        if env:
            v = Verdict("retry_or_stop", "stop", "rule", reason=f"environment error (a retry cannot fix it): {env}")
        elif len(fps) >= 2 and fps[-1] == fps[-2]:
            v = Verdict("retry_or_stop", "architect", "rule", reason="same failure twice")
        elif state.get("attempt", 0) > state.get("max_attempts", 2):
            v = Verdict("retry_or_stop", "architect", "rule", reason="retry budget exhausted")
        else:
            v = await self._consult("retry_or_stop", state, RETRY_OPTIONS)
            if v is None or not v.sharp:
                if v is not None:
                    await self._log(v, task_id, state)               # keep the not-sharp opinion for later evals
                v = await self._fallback("retry_or_stop", state, RETRY_OPTIONS, "retry")
            if v.choice == "replan":
                v = Verdict(v.point, "architect", v.engine, v.confidence, v.distribution, v.sharp, "replan requested -> architect (M4)")
        await self._log(v, task_id, state)
        return v

    async def route(self, task, task_id: str | None = None) -> Verdict:
        opts = rules.route_options(task, self.cfg.roles)
        if len(opts) == 1:
            v = Verdict("route", opts[0], "rule", reason="single feasible role")
        else:
            desc = {"explorer": "reads and maps the repository", "researcher": "reads external docs and sources"}
            v = await self._consult("route", {"task": task.model_dump()}, {o: desc.get(o, o) for o in opts})
            if v is None or not v.sharp:
                v = Verdict("route", rules.route_rule(task), "rule", reason="rule default")
        await self._log(v, task_id, {"kind": task.kind, "options": opts})
        return v

    async def tier(self, task, role_tiers: list[str] | None, task_id: str | None = None) -> Verdict | None:
        opts = rules.tier_options(role_tiers)
        if len(opts) < 2:
            return None                                              # fixed-model role: nothing to decide
        v = Verdict("tier", rules.tier_rule(task, opts), "rule", reason="writers strongest, readers cheapest")
        if not task.requires_write:
            c = await self._consult("tier", {"task": task.model_dump()}, {o: o for o in opts})
            if c is not None and c.sharp:
                v = c
        await self._log(v, task_id, {"options": opts})
        return v
