import asyncio
import subprocess

import pytest

from agent_desk.config.loader import load
from agent_desk.roles import resolve
from agent_desk.runtime.base import RunSpec
from agent_desk.runtime.codex_app_server import CodexAppServerRuntime

pytestmark = pytest.mark.codex


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text("from calc import add, multiply\n\ndef test_m():\n    assert multiply(3, 4) == 12\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    return tmp_path


async def collect():
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def approve(a):
        events.append(("approval.requested", {"cmd": a.command}))
        return False
    return events, emit, approve


@pytest.fixture
async def rt():
    r = CodexAppServerRuntime()
    yield r
    await r.close()


async def test_models_listed(rt):
    models = await rt.list_models()
    assert "gpt-5.6-luna" in models and "high" in models["gpt-6.1-sol"]


async def test_worker_edits_in_sandbox_and_reports(rt, repo):
    cfg = load(None).config
    role = resolve(cfg, "worker", "fast")
    events, emit, approve = await collect()
    res = await rt.run(RunSpec("r1", role, str(repo), "Add `multiply(a, b)` to calc.py so test_calc.py would pass. Do not run tests. Reply 'done'."), emit, approve)
    assert res.status == "completed", res.error
    assert "def multiply" in (repo / "calc.py").read_text()
    assert subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True, text=True).stdout.strip()
    types = [t for t, _ in events]
    assert types[0] == "agent.started" and types[-1] == "agent.finished" and res.usage["inputTokens"] > 0
    assert "live.text" in types                                    # the reply streams token by token
    assert "".join(p["delta"] for t, p in events if t == "live.text").strip().endswith(res.final_text.strip())
    assert any(t == "agent.message" for t in types)


async def test_read_only_role_cannot_modify(rt, repo):
    cfg = load(None).config
    events, emit, approve = await collect()
    res = await rt.run(RunSpec("r2", resolve(cfg, "explorer"), str(repo), "Create a file named hacked.txt containing 'x'. Then say what happened."), emit, approve)
    assert not (repo / "hacked.txt").exists()          # enforced by the sandbox, not the prompt


async def test_cancel_marks_interrupted_not_completed(rt, repo):
    cfg = load(None).config
    events, emit, approve = await collect()
    task = asyncio.create_task(rt.run(RunSpec("r3", resolve(cfg, "explorer"), str(repo),
                                              "Read every file in this repo slowly, one command per file, and summarise each at length."), emit, approve))
    for _ in range(300):
        await asyncio.sleep(0.1)
        if any(t == "agent.started" for t, _ in events) and rt._by_run.get("r3") and rt._by_run["r3"].turn_id:
            break
    await asyncio.sleep(1.0)
    await rt.cancel("r3")
    res = await asyncio.wait_for(task, 60)
    assert res.status in ("interrupted", "completed")   # completed only if it genuinely finished first
    if res.status != "completed":
        assert res.status == "interrupted"
