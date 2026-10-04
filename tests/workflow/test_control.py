import pytest

from agent_desk.contracts import Event
from agent_desk.graph import open_session
from agent_desk.runtime.mock import MockRuntime
from agent_desk.roles import resolve
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, task


async def sess(repo, tmp_path, extra="", script=None, delay=0):
    sc = script or Script(plan(task("T1", write=True)))
    rt = MockRuntime(sc, delay)
    s = await open_session(repo, "x", cfg(repo, None, extra), rt, HeadlessUI(), tmp_path / "home")
    return s, rt, sc


FB = "roles:\n  worker: { provider: claude, tier: strong, fallback: { provider: codex, tier: strong } }\n  architect: { provider: claude, model: opus, fallback: { provider: codex, model: gpt-6-astra } }\n"


async def test_switch_role_applies_to_next_run_and_is_an_event(repo, tmp_path):
    s, rt, sc = await sess(repo, tmp_path)
    await s.plan_phase()
    assert resolve(s.cfg, "explorer", overrides=s.overrides).provider == "codex"
    text = await s.switch_role("architect", "claude", model="opus")
    assert "architect → claude·opus" in text and resolve(s.cfg, "architect", overrides=s.overrides).provider == "claude"
    assert s.reasoner.overrides is s.overrides and s.architect.overrides is s.overrides          # shared, so the next call uses it
    ev = [e for e in await s.bus.store.events(s.sid) if e.type == "role.switched"][-1]
    assert ev.payload["source"] == "session" and ev.source == "user"


async def test_switch_role_validates(repo, tmp_path):
    s, rt, sc = await sess(repo, tmp_path)
    with pytest.raises(ValueError, match="unknown role"):
        await s.switch_role("nobody", "codex", model="x")
    with pytest.raises(Exception):
        await s.switch_role("worker", "ghost", model="x")


async def test_fallbacks_move_all_roles_on_the_provider(repo, tmp_path):
    s, rt, sc = await sess(repo, tmp_path, FB)
    moved = await s.use_fallbacks("claude")
    assert len(moved) == 2 and all("fallback" in m for m in moved)
    assert resolve(s.cfg, "worker", overrides=s.overrides).provider == "codex" and resolve(s.cfg, "architect", overrides=s.overrides).model == "gpt-6-astra"
    assert resolve(s.cfg, "main", overrides=s.overrides).provider == "codex"                   # untouched


async def test_switch_engine_is_validated_and_recorded(repo, tmp_path):
    s, rt, sc = await sess(repo, tmp_path)
    t = await s.switch_engine("retry_or_stop", "none")
    assert s.pipeline.point("retry_or_stop").engine == "none" and "decision retry_or_stop → none" in t
    with pytest.raises(ValueError, match="needs an existing provider"):
        await s.switch_engine("route", "jev")
    with pytest.raises(ValueError, match="no decision engine"):
        await s.switch_engine("route", "magic")
    with pytest.raises(ValueError, match="unknown decision point"):
        await s.switch_engine("nope", "none")
    with pytest.raises(ValueError, match="kind"):
        await s.switch_engine("route", "jev", provider="codex")             # jev needs a decision provider, not an llm one
    assert [e.type for e in await s.bus.store.events(s.sid)].count("engine.switched") == 1


async def test_stop_cancels_running_agents_and_keeps_work(repo, tmp_path):
    import asyncio
    sc = Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "x"))
    s, rt, _ = await sess(repo, tmp_path, script=sc, delay=2)
    await s.plan_phase()
    t = asyncio.create_task(s.execute())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if s.active_runs:
            break
    await s.stop()
    await asyncio.wait_for(t, 5)
    assert s.sched.cancelled and "user.stop" in [e.type for e in await s.bus.store.events(s.sid)]
    assert s.sched.tasks["T1"].status != "done"


async def test_resume_after_quota_finishes_the_session(repo, tmp_path):
    from agent_desk.runtime.base import RunResult
    inner = Script(plan(task("T1", write=True)))
    n = {"k": 0}
    def script(spec):
        if spec.role.role == "worker":
            n["k"] += 1
            if n["k"] == 1:
                return RunResult("quota", "", "", {}, "limit", "3:45pm")
        return inner(spec)
    s, rt, _ = await sess(repo, tmp_path, script=script)
    assert await s.plan_phase()
    await s.execute()
    assert await s.finalize() == "WAITING_QUOTA" and s.quota_hit
    assert await s.resume_after_quota() == "COMPLETED"
    assert s.sched.tasks["T1"].status == "done" and s.quota_hit is None
