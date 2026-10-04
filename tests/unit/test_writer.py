from pathlib import Path

import pytest

from agent_desk.config.loader import ConfigError, load
from agent_desk.config.writer import DELETE, apply_live, commit, plan_change


@pytest.fixture
def loaded(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / ".agent-desk.yaml").write_text("# my repo config\nroles:\n  worker: { provider: codex, tier: strong }  # keep me\n")
    return load(repo, global_path=tmp_path / "home" / "config.yaml")


def test_repo_save_keeps_comments_and_switches_selector(loaded):
    p = plan_change(loaded, "repo", {"roles.worker.provider": "claude", "roles.worker.model": "opus"})
    commit(loaded, p)
    text = p.path.read_text()
    assert "# my repo config" in text and "# keep me" in text and "tier" not in text
    assert loaded.config.roles["worker"].provider == "claude" and loaded.config.roles["worker"].model == "opus"
    assert loaded.sources["roles.worker.model"] == "repo"
    assert p.path.with_name(".agent-desk.yaml.bak").exists() and "+" in p.diff()


def test_global_creates_file_and_live_keeps_identity(loaded):
    cfg = loaded.config
    p = plan_change(loaded, "global", {"providers.agy2": {"runtime": "agy-cli", "auth": "login"},
                                       "tiers.agy2": {"fast": "gemini-3.8-flash-low"}})
    commit(loaded, p)
    assert loaded.config is cfg and "agy2" in cfg.providers and cfg.tiers["agy2"]["fast"] == "gemini-3.8-flash-low"
    assert p.path.exists() and "agy2" in p.path.read_text() and loaded.sources["providers.agy2"] == "global"


def test_invalid_change_writes_nothing(loaded):
    before = (loaded.repo / ".agent-desk.yaml").read_text()
    with pytest.raises(ConfigError, match="unknown provider"):
        plan_change(loaded, "repo", {"roles.worker.provider": "nobody"})
    assert (loaded.repo / ".agent-desk.yaml").read_text() == before


def test_secret_value_refused(loaded):
    with pytest.raises(ConfigError, match="secret"):
        plan_change(loaded, "global", {"providers.x.env_key": "sk-" + "a" * 30})


def test_jev_needs_policy_exception_then_validates(loaded):
    jev = {"providers.typesafe": {"kind": "decision", "runtime": "jev", "auth": "env", "env_key": "TYPESAFE_API_KEY"},
           "decision.points.retry_or_stop.engine": "jev", "decision.points.retry_or_stop.provider": "typesafe"}
    with pytest.raises(ConfigError, match="allow_api_key"):
        plan_change(loaded, "global", jev)
    commit(loaded, plan_change(loaded, "global", {**jev, "policy.allow_api_key": ["typesafe"]}))
    assert loaded.config.decision.points["retry_or_stop"].engine == "jev"
    commit(loaded, plan_change(loaded, "global", {"decision.points.retry_or_stop": DELETE, "providers.typesafe": DELETE,
                                                  "policy.allow_api_key": DELETE}))
    assert "typesafe" not in loaded.config.providers
    assert loaded.config.decision.points["retry_or_stop"].engine == "llm"        # back to the default


def test_session_scope_is_memory_only_and_survives_file_save(loaded):
    commit(loaded, plan_change(loaded, "session", {"roles.explorer.provider": "antigravity", "roles.explorer.tier": "fast"}))
    assert loaded.config.roles["explorer"].provider == "antigravity" and loaded.sources["roles.explorer.provider"] == "session"
    assert "explorer" not in (loaded.repo / ".agent-desk.yaml").read_text()
    commit(loaded, plan_change(loaded, "repo", {"roles.worker.effort": "low"}))
    assert loaded.config.roles["explorer"].provider == "antigravity"           # session change re-applied on reload
    commit(loaded, plan_change(loaded, "repo", {"roles.explorer.provider": "claude", "roles.explorer.tier": "fast"}))
    assert loaded.config.roles["explorer"].provider == "claude" and loaded.sources["roles.explorer.provider"] == "repo"


def test_saved_role_drops_session_override(loaded):
    class S: overrides = {"worker": object()}
    s = S()
    p = plan_change(loaded, "repo", {"roles.worker.effort": "low"})
    commit(loaded, p, s)
    assert s.overrides == {}
