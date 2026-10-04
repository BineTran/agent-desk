from pathlib import Path

import pytest

from agent_desk.config.check import ProviderInfo
from agent_desk.config.loader import ConfigError, load
from agent_desk.config.writer import DELETE, commit_many
from agent_desk.plugins import default_registry
from agent_desk.tui.settings_model import Item, SettingsModel, suggest_tiers
from agent_desk.tui.status import StatusInfo


@pytest.fixture
def m(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / ".agent-desk.yaml").write_text("# mine\nverification: {}\n")
    st = StatusInfo()
    st.infos["antigravity"] = ProviderInfo({"gemini-3.8-flash-medium": [], "gemini-3.1-pro-high": [], "claude-opus-5-5-high": []}, False, True)
    return SettingsModel(load(repo, global_path=tmp_path / "g" / "config.yaml"), default_registry(), st)


def test_items_cover_roles_points_providers_jev_add(m):
    kinds = [i.kind for i in m.items()]
    assert kinds.count("role") == 7 and kinds.count("point") == 4 and kinds[-5:] == ["jev", "add", "account", "account", "add_account"]
    assert [i.key for i in m.items() if i.kind == "account"] == ["codex", "claude"]        # the implicit ones
    assert Item("provider", "antigravity") in m.items()


def test_write_role_cannot_pick_read_only_runtime(m):
    opts = m.role_options("worker")
    agy = [o for o in opts if o.value and o.value[0] == "antigravity"]
    assert agy and all("no write sandbox" in o.disabled for o in agy)
    assert any(o.value == ("antigravity", "model", "claude-opus-5-5-high", None) and not o.disabled for o in m.role_options("explorer"))


@pytest.fixture
def mt(tmp_path):
    """Global config with a second claude login `team`; claude runs as `personal`."""
    repo = tmp_path / "repo"; repo.mkdir()
    g = tmp_path / "g" / "config.yaml"; g.parent.mkdir()
    g.write_text("accounts:\n  personal: {runtime: claude-cli}\n  team: {runtime: claude-cli}\nproviders:\n  claude: {account: personal}\n")
    return SettingsModel(load(repo, global_path=g), default_registry(), StatusInfo())


def test_role_picker_offers_every_account_of_the_runtime(mt):
    opts = mt.role_options("architect")
    groups = [o.group for o in opts if o.group]
    assert groups.index("claude") < groups.index("claude@team") and "claude@personal" not in groups   # the default is the plain group
    assert ("claude", "model", "opus", "team") in [o.value for o in opts] or ("claude", "tier", "max", "team") in [o.value for o in opts]


def test_stage_role_account_then_back(mt):
    mt.stage_role("architect", ("claude", "tier", "max", "team"), "medium")
    ch = mt.pending["roles.architect"].changes
    assert ch["roles.architect.account"] == "team" and ch["roles.architect.provider"] == "claude"
    assert mt.role_view("architect")["account"] == "team" and mt.role_current("architect") == ("claude", "tier", "max", "team")
    previews, probs = mt.review()
    assert not probs
    commit_many(mt.loaded, previews)
    assert mt.loaded.config.roles["architect"].account == "team"
    assert "account: team" in previews[0].path.read_text()
    mt.pending.clear()
    assert mt.role_current("architect") == ("claude", "tier", "max", "team")
    mt.stage_role("architect", ("claude", "tier", "max", None), "medium")      # back to the provider's own login
    assert mt.by_scope()["repo"]["roles.architect.account"] is DELETE           # it lives in the repo file: delete it there
    commit_many(mt.loaded, mt.review()[0])
    assert mt.loaded.config.roles["architect"].account is None


def test_stage_role_then_undo(m):
    m.stage_role("explorer", ("antigravity", "tier", "fast"), "low")
    st = m.pending["roles.explorer"]
    assert st.scope == "repo" and st.changes == {"roles.explorer.provider": "antigravity", "roles.explorer.tier": "fast"}
    assert m.role_view("explorer")["model"] == "gemini-3.8-flash-medium" and m.source("roles.explorer") == "edited"
    m.stage_role("explorer", ("codex", "tier", "fast"), "low")          # back to the original -> nothing pending
    assert "roles.explorer" not in m.pending
    m.stage_role("main", ("codex", "model", "gpt-6.1-sol"), None)
    assert m.pending["roles.main"].changes == {"roles.main.effort": DELETE}
    m.undo(Item("role", "main"))
    assert not m.pending


def test_review_saves_two_files_validated_together(m):
    m.stage_provider("gem", "agy-cli", {"fast": "gemini-3.8-flash-medium"})
    m.stage_role("architect", ("gem", "tier", "fast"), None)               # repo role uses a provider staged to global
    previews, probs = m.review()
    assert [p.scope for p in previews] == ["global", "repo"] and not probs
    commit_many(m.loaded, previews)
    assert m.loaded.config.roles["architect"].provider == "gem"
    assert "gem" in previews[0].path.read_text() and "# mine" in previews[1].path.read_text()


def test_review_reports_invalid(m):
    m.stage_role("architect", ("ghost", "model", "x"), None)
    with pytest.raises(ConfigError):
        m.review()


def test_jev_stage_decide_and_shadow(m):
    m.stage_jev("TYPESAFE_API_KEY", ["retry_or_stop", "route"], "decide")
    ch = m.pending["jev"].changes
    assert ch["providers.typesafe"]["env_key"] == "TYPESAFE_API_KEY" and ch["policy.allow_api_key"] == ["typesafe"]
    assert ch["decision.points.route.engine"] == "jev" and m.point_view("route")["engine"] == "jev"
    previews, probs = m.review()
    assert not probs and previews[0].scope == "global"
    m.stage_jev("TYPESAFE_API_KEY", ["retry_or_stop"], "shadow")
    assert m.pending["jev"].changes["decision.points.retry_or_stop.shadow"] == {"engine": "jev", "provider": "typesafe"}
    assert m.point_view("retry_or_stop")["engine"] == "llm" and m.point_view("retry_or_stop")["shadow"] == "jev"


def test_engine_options_offer_jev_setup_until_enabled(m):
    vals = [o.value for o in m.engine_options()]
    assert ("jev-setup", None, None) in vals and ("llm", "codex", "gpt-5.6-luna") in vals and ("llm", "codex", None) not in vals
    m.stage_jev("TYPESAFE_API_KEY", [], "decide")
    assert ("jev", "typesafe", None) in [o.value for o in m.engine_options()]


def test_point_to_llm_on_codex_carries_its_model(m):
    m.stage_point("route", ("llm", "codex", "gpt-5.6-luna"), "main")
    assert m.pending["decision.route"].changes["decision.points.route.model"] == "gpt-5.6-luna"
    previews, probs = m.review()
    assert not probs


def test_suggest_tiers():
    assert suggest_tiers(["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.1-pro-high"]) == {"fast": "gemini-3.8-flash-medium", "strong": "gemini-3.1-pro-high"}


def test_efforts_for_and_apply_key(m, monkeypatch):
    m.status.infos["codex"] = ProviderInfo({"gpt-5.6-luna": ["low", "medium"]}, True, True)
    assert m.efforts_for("codex", "gpt-5.6-luna") == ["low", "medium"]
    assert m.efforts_for("antigravity", "gemini-3.1-pro-high") == ["low", "medium", "high", "xhigh", "max"]
    assert m.model_of_choice("worker", ("codex", "tiers", None)) == "gpt-6.1-sol"
    monkeypatch.delenv("JEV_TEST_KEY", raising=False)

    class Pipe:
        def __init__(self): self.ctx, self._engines = type("C", (), {"env": {}})(), {"x": 1}
    s = type("S", (), {"pipeline": Pipe()})()
    m.apply_key("JEV_TEST_KEY", "abcdefgh12345", s)
    import os
    assert os.environ["JEV_TEST_KEY"] == "abcdefgh12345" and s.pipeline.ctx.env["JEV_TEST_KEY"] == "abcdefgh12345" and not s.pipeline._engines
    assert "this run only" in m.key_state("JEV_TEST_KEY")[0]
    monkeypatch.delenv("JEV_TEST_KEY")
