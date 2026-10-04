import subprocess

import pytest

from agent_desk.config.loader import load
from agent_desk.roles import resolve
from agent_desk.runtime.base import RunSpec
from agent_desk.runtime.claude_cli import ClaudeCliRuntime

pytestmark = pytest.mark.claude


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    return r


def cfg(tmp_path, role):
    (tmp_path / ".agent-desk.yaml").write_text(f"roles:\n  {role}: {{ provider: claude, tier: fast, access: {'write' if role == 'worker' else 'read'} }}\n")
    return load(tmp_path, global_path=__import__("pathlib").Path("/nonexistent")).config


async def run(repo, tmp_path, role, prompt, approve_log=None):
    events = []
    async def emit(t, p): events.append((t, p))
    async def approve(a):
        if approve_log is not None: approve_log.append(a.command)
        return "push" not in (a.command or "")                  # policy: everything but git push
    rt = ClaudeCliRuntime()
    try:
        res = await rt.run(RunSpec("c1", resolve(cfg(tmp_path, role), role), str(repo), prompt), emit, approve)
    finally:
        await rt.close()
    return res, events


async def test_info_logged_in():
    i = await ClaudeCliRuntime().info()
    assert i.logged_in and "haiku" in i.models and "claude" in i.version


async def test_read_only_role_cannot_write(repo, tmp_path):
    res, _ = await run(repo, tmp_path, "explorer", "Create a file named hacked.txt containing 'x' in the current directory, then say what happened.")
    assert res.status == "completed" and not (repo / "hacked.txt").exists()


async def test_worker_edits_inside_worktree_and_cannot_escape(repo, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    log = []
    res, ev = await run(repo, tmp_path, "worker",
                        f"Do both with bash: (1) append a function multiply(a, b) returning a*b to calc.py  (2) run: echo escaped > {outside}/x.txt . Then report.", log)
    assert res.status == "completed", res.error
    assert "def multiply" in (repo / "calc.py").read_text()
    assert not (outside / "x.txt").exists()                       # OS sandbox, not the prompt
    assert res.usage["outputTokens"] > 0 and any(t == "tool.started" for t, _ in ev)
    assert any(t in ("live.text", "live.reasoning") for t, _ in ev)          # --include-partial-messages streams


async def test_git_push_is_denied_through_the_hook(repo, tmp_path):
    log = []
    res, _ = await run(repo, tmp_path, "worker", "Run exactly this bash command and report its result: git push origin main", log)
    assert any("git push" in (c or "") for c in log)             # the hook asked us...
    assert res.status == "completed"                              # ...we said no, and the run still ended cleanly


async def test_llm_decision_engine_with_real_haiku():
    import time
    from pathlib import Path
    from agent_desk.config.loader import load
    from agent_desk.decision.engine import RETRY_OPTIONS
    from agent_desk.decision.engines import EngineCtx, LlmEngine
    from agent_desk.plugins import default_registry
    from agent_desk.runtime.registry import RuntimeRouter
    cfg = load(None, global_path=Path("/nonexistent")).config
    router = RuntimeRouter(cfg, default_registry())
    try:
        t0 = time.time()
        r = await LlmEngine(EngineCtx(cfg, router), "claude", "haiku").decide(
            "retry_or_stop", {"task": "add multiply()", "attempt": 1, "failure_tail": ["AssertionError: multiply(3, 4) == 13, expected 12"], "summary": "off by one in the implementation"},
            RETRY_OPTIONS)
    finally:
        await router.close()
    assert r.choice in RETRY_OPTIONS and abs(sum(r.probabilities.values()) - 1) < 1e-6 and r.engine == "llm:haiku"
    print(f"haiku decided {r.choice} p={r.confidence:.2f} in {time.time() - t0:.1f}s: {r.reason[:80]}")
    assert r.choice == "retry"                                                      # an ordinary code bug: retry is right
