import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_desk.config.loader import load
from agent_desk.roles import resolve
from agent_desk.runtime.base import Approval, RunSpec
from agent_desk.runtime.claude_cli import ClaudeCliRuntime, build_argv, build_settings

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


def role(tmp_path, name="worker", **over):
    y = "roles:\n  %s: { provider: claude, tier: strong, access: %s, effort: medium }\n" % (name, "write" if name == "worker" else "read")
    (tmp_path / ".agent-desk.yaml").write_text(y)
    return resolve(load(tmp_path, global_path=Path("/nonexistent")).config, name)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return ClaudeCliRuntime(binary=FAKE)


async def go(rt, tmp_path, name="worker", schema=None, approve=None, **kw):
    events = []
    async def emit(t, p): events.append((t, p))
    async def deny(a): return False
    res = await rt.run(RunSpec("r1", role(tmp_path, name), str(tmp_path), "do it", schema, **kw), emit, approve or deny)
    return res, events


def argv_of(tmp_path):
    return json.loads((tmp_path / "argv.log").read_text().splitlines()[-1])


async def test_isolation_flags_and_tools_per_access(rt, tmp_path):
    await go(rt, tmp_path, "worker", {"type": "object"})
    a = argv_of(tmp_path)
    i = lambda f: a[a.index(f) + 1]
    assert i("--permission-mode") == "dontAsk" and i("--setting-sources") == "" and i("--model") == "sonnet" and i("--effort") == "medium"
    assert "--strict-mcp-config" in a and "--disable-slash-commands" in a and "--bare" not in a
    assert set(i("--tools").split(",")) == {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    assert "Bash" not in i("--allowedTools").split(",")           # Bash only via the hook
    assert json.loads(i("--json-schema")) == {"type": "object"}
    s = json.loads(i("--settings"))
    assert s["sandbox"]["failIfUnavailable"] is True and s["sandbox"]["autoAllowBashIfSandboxed"] is False       # crashed hook => deny
    assert s["sandbox"]["filesystem"]["allowWrite"] == ["."] and s["sandbox"]["network"]["allowedDomains"] == []
    assert "claude_hook" in s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


async def test_read_role_has_no_write_tools_and_no_write_paths(rt, tmp_path):
    await go(rt, tmp_path, "architect")
    a = argv_of(tmp_path)
    assert set(a[a.index("--tools") + 1].split(",")) == {"Read", "Grep", "Glob"}
    assert json.loads(a[a.index("--settings") + 1])["sandbox"]["filesystem"]["allowWrite"] == []


async def test_success_uses_structured_output_and_sums_usage(rt, tmp_path):
    res, ev = await go(rt, tmp_path, schema={"type": "object"})
    assert res.status == "completed" and json.loads(res.final_text) == {"status": "completed", "ok": True}
    assert res.thread_id == "S1" and res.usage["inputTokens"] == 160 and res.usage["cachedInputTokens"] == 50
    types = [t for t, _ in ev]
    assert types[0] == "agent.started" and types[-1] == "agent.finished" and "tool.started" in types and "tool.completed" in types
    assert not any("thinking" in t for t in types)                # noisy events are dropped
    live = [(t, p["delta"]) for t, p in ev if t.startswith("live.")]
    assert live == [("live.reasoning", "Let me "), ("live.reasoning", "check."), ("live.text", "Running tests")]
    assert ("agent.message", {"text": "Running tests"}) in ev
    lim = next(p for t, p in ev if t == "provider.limit")
    assert lim["status"] == "allowed_warning" and lim["rateLimitType"] == "five_hour" and lim["resetsAt"] == 1791034200


async def test_resume_passes_session_id(rt, tmp_path):
    await go(rt, tmp_path, thread_id="SESS9")
    a = argv_of(tmp_path)
    assert a[a.index("--resume") + 1] == "SESS9"


async def test_quota_is_a_state_with_reset_time(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "quota")
    res, _ = await go(rt, tmp_path)
    assert res.status == "quota" and "3:45pm" in (res.reset or "")


async def test_errors_never_look_like_success(rt, tmp_path, monkeypatch):
    for mode in ("error", "nores"):
        monkeypatch.setenv("FAKE_MODE", mode)
        res, _ = await go(rt, tmp_path)
        assert res.status == "failed" and res.status != "completed"


async def test_lost_thread_on_resume_is_a_resume_failure(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "noconv")
    res, _ = await go(rt, tmp_path, thread_id="X")
    assert res.status == "failed" and res.error.startswith("resume failed") and "No conversation found" in res.error


async def test_result_errors_are_reported_not_just_the_subtype(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "noconv")
    res, _ = await go(rt, tmp_path)
    assert "No conversation found" in res.error and not res.error.startswith("resume failed")
    monkeypatch.setenv("FAKE_MODE", "error")
    res, _ = await go(rt, tmp_path)
    assert res.error == "boom"


async def test_missing_binary_fails_cleanly(tmp_path):
    res, _ = await go(ClaudeCliRuntime(binary="/nonexistent/claude"), tmp_path)
    assert res.status == "failed" and "not found" in res.error


async def test_cancel_is_interrupted_by_our_flag(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    t = asyncio.create_task(go(rt, tmp_path))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if rt._runs.get("r1") and rt._runs["r1"].proc:
            break
    await asyncio.sleep(0.5)
    await rt.cancel("r1")
    res, _ = await asyncio.wait_for(t, 15)
    assert res.status == "interrupted"


async def test_hook_bridge_allow_deny_and_fail_closed(rt, tmp_path, monkeypatch):
    seen = []
    async def approve(a: Approval):
        seen.append(a.command)
        return "FORBIDDEN" not in (a.command or "")
    monkeypatch.setenv("FAKE_MODE", "hook")
    monkeypatch.setenv("FAKE_CMD", "pnpm test")
    await go(rt, tmp_path, approve=approve)
    assert json.loads((tmp_path / "argv.log.hook").read_text())["hookSpecificOutput"]["permissionDecision"] == "allow"
    monkeypatch.setenv("FAKE_CMD", "echo FORBIDDEN")
    await go(rt, tmp_path, approve=approve)
    assert json.loads((tmp_path / "argv.log.hook").read_text())["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen == ["pnpm test", "echo FORBIDDEN"]


def test_hook_denies_when_bridge_is_unreachable(tmp_path):
    import subprocess
    env = {"AGENT_DESK_RUN": "r", "AGENT_DESK_SOCK": str(tmp_path / "missing.sock"), "PATH": "/usr/bin:/bin"}
    p = subprocess.run([sys.executable, "-m", "agent_desk.runtime.claude_hook"], input='{"tool_name":"Bash","tool_input":{"command":"ls"}}',
                       capture_output=True, text=True, env={**env, "PYTHONPATH": str(Path(__file__).parents[2])})
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    p = subprocess.run([sys.executable, "-m", "agent_desk.runtime.claude_hook"], input="not json", capture_output=True, text=True,
                       env={**env, "PYTHONPATH": str(Path(__file__).parents[2])})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_mcp_per_role_and_hooked_tools(tmp_path):
    from dataclasses import replace
    from agent_desk.runtime.claude_cli import HOOKED, mcp_args
    r = role(tmp_path, "explorer")
    home = tmp_path / "home"; home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"clickup": {"command": "c"}, "db": {"command": "d"}}}))
    spec = lambda **kw: RunSpec("r", replace(r, **kw), str(tmp_path), "p")
    assert mcp_args(spec(), str(home)) == ["--strict-mcp-config"]                  # default: no MCP
    assert mcp_args(spec(mcp="all"), str(home)) == []                                # the user's servers and connectors
    a = mcp_args(spec(mcp=("clickup",)), str(home))
    assert a[0] == "--strict-mcp-config" and json.loads(a[2]) == {"mcpServers": {"clickup": {"command": "c"}}}
    assert build_settings(spec(), "h")["hooks"]["PreToolUse"][0]["matcher"] == HOOKED and "Read" in HOOKED and "mcp__" in HOOKED


def test_writable_roots_extend_the_write_sandbox(tmp_path):
    s = RunSpec("r", role(tmp_path), str(tmp_path), "p", writable_roots=["/repo/.git"])
    assert build_settings(s, "h")["sandbox"]["filesystem"]["allowWrite"] == [".", "/repo/.git"]
