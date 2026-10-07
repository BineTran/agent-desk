from pathlib import Path

import pytest

from agent_desk.config.check import ProviderInfo, check
from agent_desk.config.loader import ConfigError, load
from agent_desk.plugins import default_registry
from agent_desk.roles import resolve

NOGLOBAL = Path("/nonexistent/config.yaml")
CODEX = {"gpt-5.6-luna": [], "gpt-6.1-sol": ["low", "medium", "high"], "gpt-6-astra": ["low", "medium", "high"]}
CLAUDE = {"haiku": [], "sonnet": [], "opus": []}
REG = default_registry()


def probe(logged=None, **kw):
    logged = logged or {}
    infos = {"codex": ProviderInfo(CODEX, True, logged.get("codex", True), "chatgpt plus", "codex 0.159", "codex login"),
             "claude": ProviderInfo(CLAUDE, False, logged.get("claude", True), "claude.ai team", "claude 2.1", "claude auth login")}
    return lambda n: infos.get(n)


def L(tmp_path, yaml_text, sets=None):
    (tmp_path / ".agent-desk.yaml").write_text(yaml_text)
    return load(tmp_path, sets, global_path=NOGLOBAL)


def test_defaults_resolve():
    c = load(None, global_path=NOGLOBAL).config
    assert resolve(c, "worker").model == "gpt-6.1-sol" and resolve(c, "worker", "fast").model == "gpt-5.6-luna"
    assert resolve(c, "architect").sandbox == "readOnly" and resolve(c, "worker").sandbox == "workspaceWrite"
    assert c.policy.subscription_only and all(p.auth == "login" for p in c.providers.values())


def test_layers_override_with_sources(tmp_path):
    l = L(tmp_path, "roles:\n  worker: { effort: high }\n", ["roles.worker.default_tier=fast"])
    assert l.config.roles["worker"].effort == "high" and l.config.roles["worker"].default_tier == "fast"
    assert l.sources["roles.worker.effort"] == "repo" and l.sources["roles.worker.default_tier"] == "--set"


def test_role_can_move_to_claude_in_one_line(tmp_path):
    c = L(tmp_path, "roles:\n  architect: { provider: claude, model: opus }\n").config
    r = resolve(c, "architect")
    assert (r.provider, r.runtime, r.model) == ("claude", "claude-cli", "opus")


def test_fallback_resolution_and_session_override(tmp_path):
    c = L(tmp_path, "roles:\n  worker: { provider: claude, tier: strong, fallback: { provider: codex, tier: strong } }\n").config
    assert resolve(c, "worker").provider == "claude" and resolve(c, "worker").model == "sonnet"
    fb = resolve(c, "worker", fallback=True)
    assert (fb.provider, fb.model, fb.via_fallback, fb.sandbox) == ("codex", "gpt-6.1-sol", True, "workspaceWrite")
    ov = resolve(c, "worker", overrides={"worker": c.roles["worker"].fallback})
    assert ov.provider == "codex"
    with pytest.raises(ValueError):
        resolve(load(None, global_path=NOGLOBAL).config, "worker", fallback=True)


def test_unknown_key_points_at_file_line(tmp_path):
    with pytest.raises(ConfigError) as e:
        L(tmp_path, "scheduler:\n  max_writerz: 2\n")
    assert ".agent-desk.yaml:2" in str(e.value)


def test_model_and_tier_together_rejected(tmp_path):
    with pytest.raises(ConfigError, match="exactly one"):
        L(tmp_path, "roles:\n  explorer: { provider: codex, model: x, tier: fast }\n")


def test_secret_value_rejected(tmp_path):
    with pytest.raises(ConfigError, match="secret"):
        L(tmp_path, "providers:\n  x: { runtime: codex, auth: env, env_key: sk-abcdefghijklmnopqrstuvwxyz123456 }\n")


def test_env_key_must_be_a_name_and_login_forbids_it(tmp_path):
    with pytest.raises(ConfigError, match="NAME"):
        L(tmp_path, "providers:\n  x: { runtime: codex, auth: env, env_key: my-key }\n")
    with pytest.raises(ConfigError, match="must not set env_key"):
        L(tmp_path, "providers:\n  x: { runtime: codex, auth: login, env_key: FOO }\n")


def test_subscription_only_rejects_api_key_provider_unless_allowed(tmp_path):
    y = "providers:\n  typesafe: { kind: decision, runtime: jev, auth: env, env_key: TYPESAFE_API_KEY }\n"
    with pytest.raises(ConfigError, match="subscription_only"):
        L(tmp_path, y)
    c = L(tmp_path, y + "policy: { allow_api_key: [typesafe] }\n").config
    assert c.providers["typesafe"].env_key == "TYPESAFE_API_KEY"


def test_switch_decision_engine_to_jev_is_config_only(tmp_path):
    y = ("providers:\n  typesafe: { kind: decision, runtime: jev, auth: env, env_key: TYPESAFE_API_KEY }\n"
         "policy: { allow_api_key: [typesafe] }\n"
         "decision:\n  points:\n    retry_or_stop: { engine: llm, provider: claude, model: haiku }\n")
    base = L(tmp_path, y)
    assert base.config.decision.points["retry_or_stop"].engine == "llm"
    sw = L(tmp_path, y, ["decision.points.retry_or_stop.engine=jev", "decision.points.retry_or_stop.provider=typesafe"])
    assert sw.config.decision.points["retry_or_stop"].engine == "jev"
    assert sw.sources["decision.points.retry_or_stop.engine"] == "--set"
    rows = {r.role: r for r in check(sw.config, REG, probe(), env={})}
    assert any("TYPESAFE_API_KEY" in p for p in rows["decision.retry_or_stop"].problems)       # no key -> flagged, falls back
    rows = {r.role: r for r in check(sw.config, REG, probe(), env={"TYPESAFE_API_KEY": "x"})}
    assert rows["decision.retry_or_stop"].ok


def test_unknown_decision_point_and_missing_provider(tmp_path):
    with pytest.raises(ConfigError, match="unknown decision point"):
        L(tmp_path, "decision:\n  points:\n    nope: { engine: none }\n")
    with pytest.raises(ConfigError, match="needs a provider"):
        L(tmp_path, "decision:\n  points:\n    route: { engine: llm }\n")


def test_check_ok_and_missing_model_and_not_logged_in(tmp_path):
    c = load(None, global_path=NOGLOBAL).config
    assert all(r.ok for r in check(c, REG, probe(), env={}))
    rows = {r.role: r for r in check(c, REG, probe({"codex": False}), env={})}
    assert "/account login codex" in rows["main"].problems[0]
    c2 = L(tmp_path, "roles:\n  architect: { provider: claude, model: nope }\n").config
    assert "nope" in {r.role: r for r in check(c2, REG, probe(), env={})}["architect"].problems[0]


def test_write_role_needs_runtime_with_sandbox_capability(tmp_path):
    from agent_desk.plugins import Plugin, Registry
    reg = Registry()
    reg.register(Plugin("runtime", "codex", lambda: None, frozenset({"sandbox_write"})))
    reg.register(Plugin("runtime", "claude-cli", lambda: None, frozenset()))             # a runtime that cannot sandbox
    c = L(tmp_path, "roles:\n  worker: { provider: claude, tier: strong }\n").config
    rows = {r.role: r for r in check(c, reg, probe(), env={})}
    assert any("write sandbox" in p for p in rows["worker"].problems)
    assert {r.role: r for r in check(c, REG, probe(), env={})}["worker"].ok               # real claude-cli has it since Spike C


def test_fallback_is_checked_too(tmp_path):
    c = L(tmp_path, "roles:\n  worker: { provider: codex, tier: strong, fallback: { provider: claude, tier: strong } }\n").config
    rows = {r.role: r for r in check(c, REG, probe(), env={})}
    assert rows["worker.fallback"].ok and rows["worker"].ok
    rows = {r.role: r for r in check(c, REG, probe({"claude": False}), env={})}
    assert not rows["worker.fallback"].ok and "/account login claude" in rows["worker.fallback"].problems[0] and rows["worker"].ok


def test_snapshot_has_no_secret_values_and_stable_hash():
    l = load(None, global_path=NOGLOBAL)
    assert l.snapshot_hash() == load(None, global_path=NOGLOBAL).snapshot_hash()


def test_chat_defaults_and_the_chat_role_is_off_until_enabled(tmp_path):
    from pathlib import Path
    from agent_desk.config.loader import load
    c = load(tmp_path, global_path=Path("/nonexistent")).config
    assert (c.chat.route, c.chat.triage, c.chat.quick_enabled, c.chat.max_quick_files) == ("auto", "main", True, 3)
    assert c.roles["chat"].enabled is False and c.roles["chat"].access == "read"


def test_cascade_triage_needs_the_chat_role_enabled(tmp_path):
    import pytest
    from pathlib import Path
    from agent_desk.config.loader import ConfigError, load
    (tmp_path / ".agent-desk.yaml").write_text("chat: { triage: cascade }\n")
    with pytest.raises(ConfigError, match="chat role"):
        load(tmp_path, global_path=Path("/nonexistent"))
    (tmp_path / ".agent-desk.yaml").write_text("chat: { triage: cascade }\nroles:\n  chat: { enabled: true }\n")
    assert load(tmp_path, global_path=Path("/nonexistent")).config.chat.triage == "cascade"
