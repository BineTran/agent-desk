from pathlib import Path

from agent_desk.config.loader import load
from agent_desk.plugins import default_registry
from agent_desk.roles import resolve
from agent_desk.runtime.base import RunResult, RunSpec
from agent_desk.runtime.mock import MockRuntime
from agent_desk.runtime.registry import RuntimeRouter


async def test_router_dispatches_by_provider_and_cancels_on_the_right_runtime(tmp_path):
    (tmp_path / ".agent-desk.yaml").write_text("roles:\n  architect: { provider: claude, model: opus }\n")
    cfg = load(tmp_path, global_path=Path("/nonexistent")).config
    codex, claude = MockRuntime(delay=0.3), MockRuntime(delay=0.3)
    router = RuntimeRouter(cfg, default_registry(), {"codex": codex, "claude": claude})

    async def emit(t, p): pass
    async def approve(a): return True
    import asyncio
    t1 = asyncio.create_task(router.run(RunSpec("r1", resolve(cfg, "main"), ".", "p"), emit, approve))
    t2 = asyncio.create_task(router.run(RunSpec("r2", resolve(cfg, "architect"), ".", "p"), emit, approve))
    await asyncio.sleep(0.05)
    await router.cancel("r2")
    r1, r2 = await t1, await t2
    assert [s.run_id for s in codex.specs] == ["r1"] and [s.run_id for s in claude.specs] == ["r2"]
    assert r1.status == "completed" and r2.status == "interrupted"          # only the claude run was cancelled
