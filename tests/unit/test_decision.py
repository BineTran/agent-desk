import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_desk.config.loader import load
from agent_desk.contracts import TaskSpec
from agent_desk.decision import rules
from agent_desk.decision.engine import DecisionPipeline
from agent_desk.decision.engines import EngineCtx, EngineError, JevEngine, LlmEngine, ReplayEngine
from agent_desk.plugins import default_registry
from agent_desk.runtime.base import RunResult
from agent_desk.runtime.mock import MockRuntime
from agent_desk.runtime.registry import RuntimeRouter

NOG = Path("/nonexistent")
REG = default_registry()


def T(kind="investigation", write=False):
    return TaskSpec(id="T1", title="t", goal="g", kind=kind, depends_on=[], relevant_files=[], files_known=False, requires_write=write, acceptance_criteria=[])


def cfg_of(tmp_path, yaml):
    (tmp_path / ".agent-desk.yaml").write_text(yaml)
    return load(tmp_path, global_path=NOG).config


class Fake:
    def __init__(self, choice, p, rest=None):
        self.choice, self.p = choice, p
        self.calls = 0
    async def decide(self, point, state, options):
        from agent_desk.decision.engines import EngineResult
        self.calls += 1
        labels = list(options)
        probs = {l: (self.p if l == self.choice else (1 - self.p) / (len(labels) - 1)) for l in labels}
        return EngineResult(self.choice, probs, self.p, "fake", "llm:fake")


def pipe(cfg, engine=None, main=None, repo=None):
    recorded = []
    async def rec(d, tid): recorded.append(d)
    p = DecisionPipeline(cfg, REG, EngineCtx(cfg), rec, main, repo)
    if engine:
        p._engine = lambda *a, **k: engine
    return p, recorded


LLM_CFG = "decision:\n  points:\n    retry_or_stop: { engine: llm, provider: claude, model: haiku, sharp_threshold: 0.8 }\n"
STATE = {"attempt": 1, "max_attempts": 2, "fingerprints": ["a"], "failure_tail": ["AssertionError: 409 != 200"], "summary": "x"}


# ---- rules ----
def test_environment_error_detection(tmp_path):
    (tmp_path / "calc.py").write_text("")
    assert rules.environment_error(["/usr/bin/python3: No module named pytest"], tmp_path)
    assert rules.environment_error(["sh: pnpm: command not found"], tmp_path)
    assert rules.environment_error(["Error: connect ECONNREFUSED 127.0.0.1:5432"], tmp_path)
    assert not rules.environment_error(["ModuleNotFoundError: No module named 'calc'"], tmp_path)   # project module missing = code bug
    assert not rules.environment_error(["AssertionError: 409 != 200"], tmp_path)
    assert rules.environment_error(["Direct verification passed, but pytest is not installed so the required command could not run."], tmp_path)
    assert rules.environment_error(["unable to install the dependency: no network access"], tmp_path)
    assert not rules.environment_error(["the new test is not implemented yet"], tmp_path)
    assert rules.environment_error(["The required command cannot start due to the environment's unavailable or unwritable temporary directory."], tmp_path)
    assert rules.environment_error(["OSError: [Errno 30] Read-only file system: '/tmp/x'"], tmp_path)
    assert rules.environment_error(["mkdir: .pytest_cache: Operation not permitted"], tmp_path)


def test_route_and_tier_rules():
    assert rules.route_options(T(write=True)) == ["worker"] and rules.route_rule(T("investigation")) == "explorer"
    assert rules.tier_rule(T(write=True), ["fast", "strong"]) == "strong" and rules.tier_rule(T(), ["fast", "strong"]) == "fast"


# ---- pipeline ----
async def test_environment_error_stops_without_calling_any_model(tmp_path):
    cfg, eng = cfg_of(tmp_path, LLM_CFG), Fake("retry", 0.99)
    p, rec = pipe(cfg, eng)
    v = await p.retry_or_stop({**STATE, "failure_tail": ["/usr/bin/python3: No module named pytest"]})
    assert (v.choice, v.engine) == ("stop", "rule") and eng.calls == 0 and rec[-1].engine == "rule"


async def test_same_failure_twice_goes_to_architect_by_rule(tmp_path):
    p, _ = pipe(cfg_of(tmp_path, LLM_CFG), Fake("retry", 0.99))
    v = await p.retry_or_stop({**STATE, "fingerprints": ["a", "a"]})
    assert (v.choice, v.engine) == ("architect", "rule")


async def test_sharp_engine_answer_is_used_and_recorded(tmp_path):
    p, rec = pipe(cfg_of(tmp_path, LLM_CFG), Fake("retry", 0.9))
    v = await p.retry_or_stop(STATE)
    assert (v.choice, v.sharp, v.engine) == ("retry", True, "llm:fake")
    assert rec[-1].engine == "llm" and rec[-1].sharp is True and abs(sum(rec[-1].distribution.values()) - 1) < 1e-9


async def test_not_sharp_goes_to_main(tmp_path):
    async def main(point, state, options): return "stop"
    p, rec = pipe(cfg_of(tmp_path, LLM_CFG), Fake("retry", 0.55), main)
    v = await p.retry_or_stop(STATE)
    assert (v.choice, v.engine) == ("stop", "main")
    assert [d.engine for d in rec] == ["llm", "main"]                     # the unsure opinion is kept for evals


async def test_engine_failure_never_blocks_the_session(tmp_path):
    class Boom:
        async def decide(self, *a): raise EngineError("TYPESAFE_API_KEY is not set")
    async def main(point, state, options): return "retry"
    p, rec = pipe(cfg_of(tmp_path, LLM_CFG), Boom(), main)
    assert (await p.retry_or_stop(STATE)).engine == "main"
    p, _ = pipe(cfg_of(tmp_path, LLM_CFG), Boom(), None)                   # no main either -> safe default
    assert (await p.retry_or_stop(STATE)).choice == "retry"


async def test_replan_becomes_architect_until_m4(tmp_path):
    p, _ = pipe(cfg_of(tmp_path, LLM_CFG), Fake("replan", 0.95))
    assert (await p.retry_or_stop(STATE)).choice == "architect"


async def test_engine_none_uses_rules_and_safe_retry(tmp_path):
    p, _ = pipe(load(None, global_path=NOG).config)
    v = await p.retry_or_stop(STATE)
    assert (v.choice, v.engine) == ("retry", "rule")


async def test_shadow_engine_is_recorded_but_never_decides(tmp_path):
    y = LLM_CFG.replace("sharp_threshold: 0.8 }", "sharp_threshold: 0.8, shadow: { engine: replay } }")
    cfg = cfg_of(tmp_path, y)
    p, rec = pipe(cfg, Fake("retry", 0.9))
    shadow = ReplayEngine(EngineCtx(cfg, recorded={"retry_or_stop": ("stop", 0.9)}))
    orig = p._engine
    p._engine = lambda e, pr, m: shadow if e == "replay" else Fake("retry", 0.9)
    v = await p.retry_or_stop(STATE)
    await asyncio.sleep(0.05)
    assert v.choice == "retry"
    assert {d.selected for d in rec} == {"retry", "stop"} and any(d.input_snapshot.get("shadow") for d in rec)


async def test_route_and_tier_are_rules_unless_ambiguous(tmp_path):
    p, rec = pipe(load(None, global_path=NOG).config)
    assert (await p.route(T(write=True))).engine == "rule"
    assert (await p.tier(T(write=True), ["fast", "strong"])).choice == "strong"
    assert await p.tier(T(), None) is None                                  # fixed-model role: no tier question
    # research can go to researcher or explorer: only here would an engine be consulted
    cfg = cfg_of(tmp_path, "decision:\n  points:\n    route: { engine: llm, provider: claude, model: haiku }\n")
    p, rec = pipe(cfg, Fake("researcher", 0.95))
    assert (await p.route(T("research"))).choice == "researcher"


# ---- engines ----
async def test_llm_engine_through_router_validates_and_normalises(tmp_path):
    cfg = load(None, global_path=NOG).config
    seen = {}
    def script(spec):
        seen["spec"] = spec
        return RunResult("completed", '{"choice":"stop","probabilities":{"retry":0.2,"replan":0.2,"stop":0.8},"reason":"env"}', "s")
    router = RuntimeRouter(cfg, REG, {"claude": MockRuntime(script)})
    r = await LlmEngine(EngineCtx(cfg, router), "claude", "haiku").decide("retry_or_stop", {"a": 1}, {"retry": "x", "replan": "y", "stop": "z"})
    assert r.choice == "stop" and abs(sum(r.probabilities.values()) - 1) < 1e-9 and r.confidence == pytest.approx(0.8 / 1.2)
    assert seen["spec"].tools == [] and seen["spec"].output_schema["properties"]["choice"]["enum"] == ["retry", "replan", "stop"]
    bad = RuntimeRouter(cfg, REG, {"claude": MockRuntime(lambda s: RunResult("completed", '{"choice":"explode","probabilities":{},"reason":""}', "s"))})
    with pytest.raises(EngineError):
        await LlmEngine(EngineCtx(cfg, bad), "claude", "haiku").decide("p", {}, {"retry": "x"})
    quota = RuntimeRouter(cfg, REG, {"claude": MockRuntime(lambda s: RunResult("quota", "", "", {}, "limit", "3pm"))})
    with pytest.raises(EngineError):
        await LlmEngine(EngineCtx(cfg, quota), "claude", "haiku").decide("p", {}, {"retry": "x"})


async def test_jev_engine_maps_langchain_typesafe_response(tmp_path):
    class Client:
        async def ainvoke(self, req):
            assert req["questions"]["d"].criteria == {"retry": "again", "stop": "halt"}
            return SimpleNamespace(choices={"d": SimpleNamespace(choice="stop", probabilities={"retry": 0.1, "stop": 0.9}, confidence=0.88)})
    r = await JevEngine(EngineCtx(load(None, global_path=NOG).config), "TYPESAFE_API_KEY", Client()).decide("retry_or_stop", {}, {"retry": "again", "stop": "halt"})
    assert (r.choice, r.confidence, r.engine) == ("stop", 0.88, "jev")
    with pytest.raises(EngineError, match="TYPESAFE_API_KEY"):                                  # no key -> engine error -> pipeline falls back
        await JevEngine(EngineCtx(load(None, global_path=NOG).config, env={}), "TYPESAFE_API_KEY").decide("p", {}, {"a": "x"})


async def test_swapping_llm_for_jev_is_only_config(tmp_path):
    y = ("providers:\n  typesafe: { kind: decision, runtime: jev, auth: env, env_key: TYPESAFE_API_KEY }\npolicy: { allow_api_key: [typesafe] }\n" + LLM_CFG)
    cfg = cfg_of(tmp_path, y)
    p, _ = pipe(cfg)
    assert type(p._engine("llm", "claude", "haiku")).__name__ == "LlmEngine"
    cfg2 = load(tmp_path, ["decision.points.retry_or_stop.engine=jev", "decision.points.retry_or_stop.provider=typesafe"], global_path=NOG).config
    p2, _ = pipe(cfg2)
    pt = p2.point("retry_or_stop")
    assert type(p2._engine(pt.engine, pt.provider, pt.model)).__name__ == "JevEngine"


def _guard(kind="quick", outputs=("a.py",), text="rename foo", tkind="implementation", **chat):
    from agent_desk.config.schema import Chat
    from agent_desk.contracts import TaskSpec
    from agent_desk.decision.rules import route_guard
    t = TaskSpec(id="T1", title="t", goal="g", kind=tkind, depends_on=[], relevant_files=[], files_known=False, requires_write=True,
                 acceptance_criteria=["ac"], outputs=list(outputs))
    return route_guard(kind, t, text, Chat(**chat))[0]


def test_route_guard_only_moves_up():
    assert _guard() == "quick"
    assert _guard("answer") == "answer" and _guard("plan") == "plan"
    assert _guard(text="please push it") == "plan" and _guard(text="run the Migration") == "plan"
    assert _guard(outputs=("a", "b", "c", "d")) == "plan" and _guard(outputs=("a", "b", "c", "d"), max_quick_files=4) == "quick"
    assert _guard(outputs=()) == "plan" and _guard(outputs=("src/*.py",)) == "plan"
    assert _guard(tkind="investigation") == "plan" and _guard(quick_enabled=False) == "plan"


def test_route_guard_without_task_goes_to_plan():
    from agent_desk.config.schema import Chat
    from agent_desk.decision.rules import route_guard
    assert route_guard("quick", None, "x", Chat())[0] == "plan"
