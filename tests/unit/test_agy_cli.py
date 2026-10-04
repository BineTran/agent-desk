import json
from pathlib import Path

import pytest

from agent_desk.config.loader import load
from agent_desk.roles import resolve
from agent_desk.runtime.agy_cli import AgyCliRuntime, parse_models
from agent_desk.runtime.base import RunSpec

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_agy.py")


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("FAKE_MODE", "ok")
    (tmp_path / ".agent-desk.yaml").write_text("roles:\n  explorer: { provider: antigravity, tier: fast, effort: low }\n")
    return AgyCliRuntime(binary=FAKE)


async def go(rt, tmp_path, **kw):
    events = []
    async def emit(t, p): events.append((t, p))
    async def deny(a): return False
    r = resolve(load(tmp_path, global_path=Path("/nonexistent")).config, "explorer")
    return await rt.run(RunSpec("r1", r, str(tmp_path), "read a.txt", **kw), emit, deny), events


async def test_run_normalizes_stream_and_result(rt, tmp_path):
    res, ev = await go(rt, tmp_path, output_schema={"type": "object"}, thread_id="c-1")
    a = json.loads((tmp_path / "argv.log").read_text().splitlines()[-1])
    i = lambda f: a[a.index(f) + 1]
    assert i("--model") == "gemini-3.8-flash-medium" and i("--effort") == "low" and i("--conversation") == "c-1"
    assert "--sandbox" in a and json.loads(i("--json-schema")) == {"type": "object"} and "--dangerously-skip-permissions" not in a
    assert res.status == "completed" and res.final_text == "hello" and res.thread_id == "e952e4e5-640b-4ced-9e8b-0ab99477d555"
    assert res.usage["inputTokens"] == 21748 + 16290 and res.usage["cachedInputTokens"] == 16290
    kinds = [t for t, _ in ev]
    assert kinds[0] == "agent.started" and kinds[-1] == "agent.finished"
    assert ("tool.started", "view_file") in [(t, p.get("tool")) for t, p in ev]
    assert "".join(p["delta"] for t, p in ev if t == "live.text") == "hello\n"


async def test_quota_error(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error")
    res, _ = await go(rt, tmp_path)
    assert res.status == "quota" and "5pm" in (res.reset or "")


async def test_denied_write_is_a_clear_failure(rt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "denied")
    res, _ = await go(rt, tmp_path)
    assert res.status == "failed" and "write_file" in res.error and res.thread_id == "c9"


async def test_info_lists_models(rt):
    info = await rt.info()
    assert info.logged_in and info.version == "agy 1.2.16" and set(info.models) == {"gemini-3.8-flash-low", "gemini-3.1-pro-high"}


def test_parse_models_skips_header():
    assert parse_models("Fetching available models...\nx-1\tX\n") == {"x-1": []}


async def test_missing_binary():
    info = await AgyCliRuntime(binary="/nonexistent/agy").info()
    assert not info.logged_in
