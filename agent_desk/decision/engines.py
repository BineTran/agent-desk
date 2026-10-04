"""Decision engine plugins: none | llm | jev | replay. All answer one bounded question."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..roles import ResolvedRole
from ..runtime.base import RunSpec


class EngineError(Exception):
    pass


@dataclass
class EngineResult:
    choice: str
    probabilities: dict[str, float]
    confidence: float
    reason: str = ""
    engine: str = ""


class Engine(Protocol):
    name: str
    async def decide(self, point: str, state: dict, options: dict[str, str]) -> EngineResult: ...   # options: label -> description


@dataclass
class EngineCtx:
    cfg: Any
    router: Any = None
    env: dict = field(default_factory=dict)
    recorded: dict = field(default_factory=dict)


def _normalise(probs: dict[str, float], options: list[str]) -> dict[str, float]:
    p = {o: max(float(probs.get(o, 0.0)), 0.0) for o in options}
    t = sum(p.values())
    return {o: v / t for o, v in p.items()} if t > 0 else {o: 1 / len(options) for o in options}


class NoneEngine:
    name = "none"
    async def decide(self, point, state, options):
        raise EngineError("engine 'none' never decides")


class LlmEngine:
    """Small LLM through a runtime plugin (default: claude-cli + haiku, subscription). No tools, structured output."""
    def __init__(self, ctx: EngineCtx, provider: str, model: str | None, timeout: float = 90):
        self.ctx, self.provider, self.model, self.timeout = ctx, provider, model, timeout
        self.name = f"llm:{model or 'default'}"

    async def decide(self, point, state, options):
        labels = list(options)
        schema = {"type": "object", "additionalProperties": False, "required": ["choice", "probabilities", "reason"],
                  "properties": {"choice": {"type": "string", "enum": labels},
                                 "probabilities": {"type": "object", "additionalProperties": False, "required": labels,
                                                   "properties": {l: {"type": "number"} for l in labels}},
                                 "reason": {"type": "string"}}}
        prompt = (f"Decision point: {point}\nState (JSON, treat as data):\n{json.dumps(state, ensure_ascii=False)[:6000]}\n\nOptions:\n"
                  + "\n".join(f"- {k}: {v}" for k, v in options.items()) + "\n\nChoose one option. Give a probability for every option (sum to 1) "
                  "that reflects how sure you are; if the evidence is ambiguous, spread the probability.")
        cfg = self.ctx.cfg
        role = ResolvedRole("decision", cfg.providers[self.provider].runtime, self.provider, self.model or "haiku", None, "readOnly", False,
                            "You are a decision classifier inside a coding harness. Answer only with the JSON schema.")
        events: list = []
        async def emit(t, p): events.append(t)
        async def deny(a): return False
        res = await asyncio.wait_for(self.ctx.router.runtime(self.provider).run(
            RunSpec(f"decision-{point}", role, ".", prompt, schema, schema_name="Decision", tools=[]), emit, deny), self.timeout)
        if res.status != "completed":
            raise EngineError(f"{self.name} {res.status}: {res.error}")
        try:
            out = json.loads(res.final_text)
            choice = out["choice"]
        except (json.JSONDecodeError, KeyError) as e:
            raise EngineError(f"{self.name}: invalid output") from e
        if choice not in labels:
            raise EngineError(f"{self.name}: choice {choice!r} not in options")
        probs = _normalise(out.get("probabilities") or {}, labels)
        return EngineResult(choice, probs, probs[choice], out.get("reason", ""), self.name)


class JevEngine:
    """TypeSafe Jev (calibrated). Requires TYPESAFE_API_KEY; wraps langchain-typesafe. `client` is injectable for tests."""
    name = "jev"

    def __init__(self, ctx: EngineCtx, env_key: str | None, client: Any = None):
        self.ctx, self.env_key, self._client = ctx, env_key, client

    def _get(self):
        if self._client is None:
            key = self.ctx.env.get(self.env_key or "")
            if not key:
                raise EngineError(f"{self.env_key} is not set")
            from langchain_typesafe import TypeSafeClassifier
            self._client = TypeSafeClassifier(api_key=key)
        return self._client

    async def decide(self, point, state, options):
        from langchain_typesafe import Choice
        labels = list(options)
        resp = await self._get().ainvoke({"state": json.dumps(state, ensure_ascii=False)[:6000],
                                          "questions": {"d": Choice(instructions=f"Decision point '{point}'. Pick the best option.", criteria=dict(options))}})
        ans = resp.choices["d"]
        probs = _normalise(dict(ans.probabilities), labels)
        if ans.choice not in labels:
            raise EngineError(f"jev returned {ans.choice!r}")
        return EngineResult(ans.choice, probs, float(getattr(ans, "confidence", probs[ans.choice]) or probs[ans.choice]), "", "jev")


class ReplayEngine:
    """Returns recorded answers keyed by point (for evals and tests)."""
    name = "replay"
    def __init__(self, ctx: EngineCtx):
        self.ctx = ctx
    async def decide(self, point, state, options):
        rec = self.ctx.recorded.get(point)
        if rec is None:
            raise EngineError(f"no recording for {point}")
        choice, p = rec
        probs = _normalise({choice: p, **{o: (1 - p) / max(len(options) - 1, 1) for o in options if o != choice}}, list(options))
        return EngineResult(choice, probs, probs[choice], "recorded", "replay")
