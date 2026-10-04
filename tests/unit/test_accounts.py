"""Accounts: one login per profile dir, picked by providers; pinned identity; never the user's ~/.claude / ~/.codex."""
import asyncio
import os

import pytest
import yaml
from pathlib import Path

from pydantic import ValidationError
from typer.testing import CliRunner

from agent_desk import accounts as A
from agent_desk.config.check import ProviderInfo, check
from agent_desk.config.loader import load
from agent_desk.config.schema import Config, Provider
from agent_desk.contracts import Questions
from agent_desk.plugins import default_registry
from agent_desk.runtime.base import RunSpec
from agent_desk.runtime.claude_cli import ClaudeCliRuntime
from agent_desk.runtime.codex_app_server import CodexAppServerRuntime
from agent_desk.runtime.registry import RuntimeRouter
from agent_desk.roles import resolve

from ..workflow.conftest import NOGLOBAL
from .test_m7 import MainRt, mk

BASE = load(None, global_path=NOGLOBAL).config
REG = default_registry()


def cfg_with(accounts=None, providers=None, roles=None) -> Config:
    d = BASE.model_dump()
    d["accounts"] = accounts or {}
    for k, v in (providers or {}).items():
        d["providers"].setdefault(k, {}).update(v)
    for k, v in (roles or {}).items():
        d["roles"][k].update(v)
    return Config.model_validate(d)


class FakeRt:
    def __init__(self, home=None, ident="", logged=True):
        self.home, self.ident, self.logged, self.closed, self.runs = home, ident, logged, False, 0
    async def info(self): return ProviderInfo({}, True, self.logged, self.ident, "x", "", self.ident if self.logged else "")
    async def run(self, spec, emit, approve):
        from agent_desk.runtime.base import RunResult
        self.runs += 1
        return RunResult("completed", "{}")
    async def close(self): self.closed = True
    def login_command(self, email=None, browser=None): return ["true"], None, True
    def logout_command(self): return ["true"], None


# ---------------- schema ----------------
def test_implicit_account_never_uses_the_everyday_profile():
    name, acc = BASE.account_of("claude")
    assert name == "claude" and acc.path(name) == os.path.expanduser("~/.agent-desk/accounts/claude")
    assert BASE.account_of("codex")[1].path("codex") != os.path.expanduser("~/.codex")


def test_account_validation():
    with pytest.raises(ValidationError, match="unknown account"):
        cfg_with(providers={"claude": {"account": "nope"}})
    with pytest.raises(ValidationError, match="cannot use account"):
        cfg_with({"gpt": {"runtime": "codex"}}, {"claude": {"account": "gpt"}})
    with pytest.raises(ValidationError, match="same profile dir"):
        cfg_with({"a": {"runtime": "claude-cli", "home": "~/x"}, "b": {"runtime": "claude-cli", "home": "~/x"}})
    with pytest.raises(ValidationError, match="only for auth: login"):
        Provider(runtime="claude-cli", auth="env", env_key="K", account="a")
    with pytest.raises(ValidationError, match="account migrate"):
        Provider(runtime="claude-cli", home="~/x")
    c = cfg_with({"team": {"runtime": "claude-cli"}}, {"claude": {"account": "team"}, "claude-2": {"runtime": "claude-cli", "account": "team"}})
    assert c.account_of("claude")[0] == c.account_of("claude-2")[0] == "team"      # two providers may share one login


def test_fallback_to_another_account_of_the_same_runtime_is_refused():
    acc = {"team": {"runtime": "claude-cli"}}
    prov = {"claude-2": {"runtime": "claude-cli", "account": "team"}}
    with pytest.raises(ValidationError, match="another account of the same runtime"):
        cfg_with(acc, prov, {"architect": {"provider": "claude", "model": "opus", "tier": None, "fallback": {"provider": "claude-2", "model": "opus"}}})
    cfg_with(acc, prov, {"architect": {"provider": "claude", "model": "opus", "tier": None, "fallback": {"provider": "codex", "model": "gpt-6.1-sol"}}})


# ---------------- a role picks its own account ----------------
TEAM = {"personal": {"runtime": "claude-cli"}, "team": {"runtime": "claude-cli", "identity": "me@work"}}
ARCH_TEAM = {"architect": {"provider": "claude", "model": "opus", "tier": None, "account": "team"}}


def test_role_account_validation():
    c = cfg_with(TEAM, {"claude": {"account": "personal"}}, ARCH_TEAM)
    assert c.account_of("claude", c.roles["architect"].account)[0] == "team" and c.account_of("claude")[0] == "personal"
    with pytest.raises(ValidationError, match="unknown account"):
        cfg_with(TEAM, None, {"architect": {"provider": "claude", "model": "opus", "tier": None, "account": "nope"}})
    with pytest.raises(ValidationError, match="cannot run as account"):
        cfg_with({"gpt": {"runtime": "codex"}}, None, {"architect": {"provider": "claude", "model": "opus", "tier": None, "account": "gpt"}})
    with pytest.raises(ValidationError, match="another account of the same runtime"):
        cfg_with(TEAM, {"claude": {"account": "personal"}},
                 {"architect": {"provider": "claude", "model": "opus", "tier": None, "account": "team", "fallback": {"provider": "claude", "model": "opus"}}})


def test_resolve_and_router_run_a_role_as_its_own_account():
    c = cfg_with(TEAM, {"claude": {"account": "personal"}}, ARCH_TEAM)
    arch = resolve(c, "architect")
    assert arch.provider == "claude" and arch.account == "team"
    r = RuntimeRouter(c, REG)
    a, b = r.runtime("claude"), r.runtime("claude", "team")
    assert a is not b and a.env["CLAUDE_CONFIG_DIR"].endswith("/accounts/personal") and b.env["CLAUDE_CONFIG_DIR"].endswith("/accounts/team")
    assert r.runtime("claude", "personal") is a                          # the provider's own account: same instance
    assert r.for_account("team") is b


async def test_pin_is_checked_per_account():
    c = cfg_with(TEAM, {"claude": {"account": "personal"}}, ARCH_TEAM)
    r = RuntimeRouter(c, REG)
    personal, team = FakeRt(ident="me@home"), FakeRt(ident="someone@else")
    r._rt["claude"], r._home["claude"] = personal, c.accounts["personal"].path("personal")
    r._rt["claude@team"], r._home["claude@team"] = team, c.accounts["team"].path("team")
    async def emit(*a): pass
    async def ok(a): return True
    res = await r.run(RunSpec("a1", resolve(c, "architect"), "/w", "p"), emit, ok)
    assert res.status == "failed" and "pinned to me@work" in res.error and team.runs == 0
    team.ident = "me@work"
    assert (await r.run(RunSpec("a2", resolve(c, "architect"), "/w", "p"), emit, ok)).status == "completed" and team.runs == 1
    await r.forget_account("team")
    assert team.closed and "claude" in r._rt


def test_check_uses_the_role_account_login():
    c = cfg_with(TEAM, {"claude": {"account": "personal"}}, ARCH_TEAM)
    models = {"opus": [], "sonnet": [], "haiku": [], "gpt-6.1-sol": ["high", "medium"], "gpt-6-astra": ["medium"], "gpt-5.6-luna": ["low"]}
    prov = lambda n: ProviderInfo(models, True, True, "me@home", "", "", "me@home")
    rows = {x.role: x for x in check(c, REG, prov, env={}, account_probe=lambda a: ProviderInfo({}, True, False, "", "", "", ""))}
    assert rows["architect"].provider == "claude@team" and any("/account login team" in p for p in rows["architect"].problems)
    rows = {x.role: x for x in check(c, REG, prov, env={}, account_probe=lambda a: ProviderInfo({}, True, True, "me@work", "", "", "me@work"))}
    assert rows["architect"].ok, rows["architect"].problems


def test_a_layer_moving_a_role_to_another_provider_drops_its_account(tmp_path):
    g = tmp_path / "g.yaml"
    g.write_text(yaml.safe_dump({"accounts": TEAM, "roles": ARCH_TEAM}))
    (tmp_path / ".agent-desk.yaml").write_text(yaml.safe_dump({"roles": {"architect": {"provider": "codex", "model": "gpt-6-astra"}}}))
    c = load(tmp_path, global_path=g).config
    assert c.roles["architect"].provider == "codex" and c.roles["architect"].account is None


def test_add_by_runtime_and_use_for_a_role(tmp_path):
    from agent_desk.config.writer import commit
    loaded = load(None, global_path=tmp_path / "g.yaml")
    assert A.plan_add(loaded, REG, "w", "claude-cli").new.config.accounts["w"].runtime == "claude-cli"
    assert A.plan_add(loaded, REG, "w", "codex").new.config.accounts["w"].runtime == "codex"
    with pytest.raises(Exception, match="which CLI.*claude-cli.*codex|which CLI.*codex.*claude-cli"):
        A.plan_add(loaded, REG, "w")
    with pytest.raises(Exception, match="cannot keep separate accounts"):
        A.plan_add(loaded, REG, "w", "antigravity")
    with pytest.raises(Exception, match="account name"):
        A.plan_add(loaded, REG, "Team X", "claude-cli")
    commit(loaded, A.plan_add(loaded, REG, "team", "codex"))           # architect runs on codex by default
    commit(loaded, A.plan_use(loaded, REG, "architect", "team"))
    c = loaded.config
    assert c.roles["architect"].account == "team" and c.account_of("codex")[0] == "codex"
    assert resolve(c, "architect").account == "team" and resolve(c, "worker").account is None
    assert A.users(c, "team") == ["role:architect"]
    with pytest.raises(Exception, match="used by role:architect"):
        A.plan_remove(loaded, "team")
    with pytest.raises(Exception, match="is claude-cli"):
        A.plan_use(loaded, REG, "architect", "claude")
    commit(loaded, A.plan_use(loaded, REG, "architect", "codex"))        # back to the provider's own account
    assert loaded.config.roles["architect"].account is None


# ---------------- router ----------------
def test_router_rebuilds_when_the_account_changes():
    c = cfg_with({"team": {"runtime": "claude-cli"}, "home2": {"runtime": "claude-cli"}}, {"claude": {"account": "team"}})
    r = RuntimeRouter(c, REG)
    a = r.runtime("claude")
    assert a.env["CLAUDE_CONFIG_DIR"].endswith("/.agent-desk/accounts/team") and r.runtime("claude") is a
    c.providers["claude"].account = "home2"                 # what apply_live does
    b = r.runtime("claude")
    assert b is not a and b.env["CLAUDE_CONFIG_DIR"].endswith("/accounts/home2")
    assert r.for_account("home2") is b and r.for_account("team") is not b
    assert r.runtime("antigravity").__class__.__name__ == "AgyCliRuntime"   # no profile dirs: its own login, no account


async def test_runs_refuse_a_pinned_account_logged_in_as_someone_else():
    c = cfg_with({"team": {"runtime": "claude-cli", "identity": "me@work · Acme"}}, {"claude": {"account": "team"}})
    r = RuntimeRouter(c, REG)
    fake = FakeRt(ident="me@home")
    r._rt["claude"], r._home["claude"] = fake, c.accounts["team"].path("team")
    import dataclasses
    role = dataclasses.replace(resolve(c, "worker"), provider="claude", model="sonnet")
    async def emit(*a): pass
    async def ok(a): return True
    res = await r.run(RunSpec("w1", role, "/w", "p"), emit, ok)
    assert res.status == "failed" and "pinned to me@work" in res.error and fake.runs == 0
    fake.ident = "me@work · Acme"
    r._verified.clear()
    assert (await r.run(RunSpec("w2", role, "/w", "p"), emit, ok)).status == "completed"


def test_config_check_reports_mismatch():
    c = cfg_with({"team": {"runtime": "claude-cli", "identity": "me@work"}}, {"claude": {"account": "team"}},
                 {"worker": {"provider": "claude", "model": "sonnet", "tiers": None, "default_tier": None}})
    rows = {x.role: x for x in check(c, REG, lambda n: ProviderInfo({"sonnet": [], "gpt-6.1-sol": ["high", "medium"], "gpt-6-astra": ["medium"], "gpt-5.6-luna": ["low"]},
                                                                     True, True, "me@home", "", "", "me@home"), env={})}
    assert any("pinned to me@work" in p for p in rows["worker"].problems)


# ---------------- runtimes ----------------
def test_claude_and_codex_profile_env(monkeypatch):
    seen = []

    class P:
        async def communicate(self): return b'{"loggedIn": true, "email": "me@x.io", "orgName": "Org", "subscriptionType": "pro"}', b""

    async def fake(*argv, **kw):
        seen.append(kw.get("env"))
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    info = asyncio.run(ClaudeCliRuntime(home="/tmp/acc").info())
    assert all(e["CLAUDE_CONFIG_DIR"] == "/tmp/acc" for e in seen) and info.identity == "me@x.io · Org"
    assert CodexAppServerRuntime(home="/tmp/cx").rpc.env["CODEX_HOME"] == "/tmp/cx"


# ---------------- accounts service ----------------
def router_with(c, fakes):
    r = RuntimeRouter(c, REG, fakes)
    return r


async def test_status_flags_duplicates_and_not_logged_in(tmp_path):
    c = cfg_with({"team": {"runtime": "claude-cli"}}, {"claude": {"account": "team"}})
    r = router_with(c, {"team": FakeRt(ident="me@home"), "codex": FakeRt(logged=False)})
    rows = {x.name: x for x in await A.status(c, r)}
    assert rows["codex"].state == "not logged in" and rows["team"].state == "ok" and rows["team"].used_by == ["claude"]
    assert "claude" not in rows                                  # claude now runs as team: no implicit account left
    r2 = router_with(c, {"team": FakeRt(ident="me@home"), "codex": FakeRt(ident="me@home")})
    assert {x.name: x.state for x in await A.status(c, r2)} == {"team": "DUPLICATE", "codex": "DUPLICATE"}
    assert "ACCOUNT" in A.table(list(rows.values())).splitlines()[0]


async def test_after_login_pins_or_refuses_a_duplicate(tmp_path):
    loaded = load(None, global_path=tmp_path / "g.yaml")
    r = router_with(loaded.config, {"claude": FakeRt(ident="me@home"), "codex": FakeRt(ident="me@gpt")})
    msg, p = await A.after_login(loaded, r, "claude")
    assert p is not None and "pinned" in msg and p.new.config.accounts["claude"].identity == "me@home"
    r = router_with(loaded.config, {"claude": FakeRt(ident="same"), "codex": FakeRt(ident="same")})
    msg, p = await A.after_login(loaded, r, "claude")
    assert p is None and "same login as codex" in msg and "logged out again" in msg
    msg, p = await A.after_login(loaded, r, "claude", undo_duplicate=False)     # /account pin: this one owns the login
    assert p is not None and "codex has the same login" in msg


async def test_the_pinned_account_keeps_its_login_in_status():
    c = cfg_with({"team": {"runtime": "claude-cli"}, "mine": {"runtime": "claude-cli", "identity": "me@home"}}, {"claude": {"account": "mine"}})
    r = router_with(c, {"team": FakeRt(ident="me@home"), "mine": FakeRt(ident="me@home"), "codex": FakeRt(logged=False)})
    st = {x.name: x.state for x in await A.status(c, r)}
    assert st["mine"] == "ok" and st["team"] == "DUPLICATE"


def test_use_add_remove(tmp_path):
    loaded = load(None, global_path=tmp_path / "g.yaml")
    p = A.plan_add(loaded, REG, "team", "claude")
    assert p.new.config.accounts["team"].runtime == "claude-cli"
    from agent_desk.config.writer import commit
    commit(loaded, p)
    p = A.plan_use(loaded, REG, "claude", "team")
    commit(loaded, p)
    assert loaded.config.account_of("claude")[0] == "team"
    with pytest.raises(Exception, match="used by claude"):
        A.plan_remove(loaded, "team")
    commit(loaded, A.plan_use(loaded, REG, "claude", "claude"))           # back to the implicit one (made explicit)
    commit(loaded, A.plan_remove(loaded, "team"))
    assert "team" not in loaded.config.accounts
    with pytest.raises(Exception, match="cannot use account|unknown account"):
        A.plan_use(loaded, REG, "codex", "claude")


def test_login_refuses_the_everyday_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c = cfg_with({"mine": {"runtime": "claude-cli", "home": str(tmp_path / ".claude")}}, {"claude": {"account": "mine"}})
    with pytest.raises(Exception, match="never logs in there"):
        A.login_command(c, RuntimeRouter(c, REG), "mine")
    c2 = cfg_with({"team": {"runtime": "claude-cli", "home": str(tmp_path / "t")}})
    argv, env, itself = A.login_command(c2, RuntimeRouter(c2, REG), "team", "a@b.c", browser="/x/shim")
    assert argv[-2:] == ["--email", "a@b.c"] and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "t") and (tmp_path / "t").is_dir()
    assert env["BROWSER"] == "/x/shim" and itself                     # claude opens the URL through $BROWSER
    argv, env, itself = A.login_command(c2, RuntimeRouter(c2, REG), "team")
    assert "BROWSER" not in env


def test_codex_private_login_uses_the_device_flow():
    rt = CodexAppServerRuntime(home="/tmp/cx")
    assert rt.login_command(browser="/x/shim") == (["codex", "login", "--device-auth"], rt.env, False)
    assert rt.login_command() == (["codex", "login"], rt.env, True)


def test_private_browser_choice(monkeypatch):
    monkeypatch.setattr(A, "default_browser", lambda: "com.microsoft.edgemac")
    every = lambda app: True
    assert A.private_browser("auto", every) == ("Microsoft Edge", "--inprivate")
    monkeypatch.setattr(A, "default_browser", lambda: "com.apple.safari")          # Safari: no CLI private window
    assert A.private_browser("auto", lambda app: app == "Firefox") == ("Firefox", "-private-window")
    assert A.private_browser("auto", lambda app: False) is None
    assert A.private_browser("chrome", every) == ("Google Chrome", "--incognito") and A.private_browser("print", every) is None


def test_shim_and_device_flow_url_is_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "private_browser", lambda choice="auto", installed=None: ("Microsoft Edge", "--inprivate"))
    path, opens = A.browser_shim(root=tmp_path)
    assert 'open -na "Microsoft Edge" --args --inprivate "$1"' in Path(path).read_text() and os.access(path, os.X_OK)
    log = tmp_path / "opened"
    fake = tmp_path / "fake-browser"
    fake.write_text(f'#!/bin/sh\necho "$1" >> {log}\n'); fake.chmod(0o755)
    rc = A.run_login(["sh", "-c", "echo 'Open \033[94mhttps://auth.example/device\033[0m and enter ABCD'; echo again https://x.y"],
                     None, False, str(fake))
    assert rc == 0 and log.read_text().splitlines() == ["https://auth.example/device"]      # first URL only, ANSI stripped


def test_migrate_old_home_layout():
    old = ("providers:\n  personal:\n    runtime: claude-cli\n    auth: login\n    home: ~/.agent-desk/accounts/personal\n"
           "  other:\n    runtime: codex\n    home: ~/elsewhere\ntiers:\n  personal: {fast: haiku}\n  other: {fast: x}\n")
    new, notes = A.migrate_text(old)
    d = yaml.safe_load(new)
    assert d["accounts"] == {"personal": {"runtime": "claude-cli"}, "other": {"runtime": "codex", "home": "~/elsewhere"}}
    assert "providers" not in d and "tiers" not in d and len(notes) == 2
    assert A.migrate_text(new) == (new, [])


# ---------------- reasoner ----------------
async def test_main_never_resumes_a_thread_on_another_account():
    c = cfg_with({"a1": {"runtime": "codex"}, "a2": {"runtime": "codex"}}, {"codex": {"account": "a1"}})
    rt = MainRt()
    r, ev = await mk(rt)
    r.cfg = c
    await r.ask("q", Questions, "q1")
    await r.ask("q", Questions, "q2")
    assert rt.specs[1].thread_id == "th-1"
    c.providers["codex"].account = "a2"                      # same provider, other login
    await r.ask("q", Questions, "q3")
    assert rt.specs[2].thread_id is None and "Session memory" in rt.specs[2].prompt
    assert any(t == "main.rotated" and "codex@a1 -> codex@a2" in p["reason"] for t, p in ev)


# ---------------- cli ----------------
def test_cli_add_use(tmp_path, monkeypatch):
    import agent_desk.cli as cli
    from agent_desk.config import loader
    g = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "load", lambda repo, sets: loader.load(repo, sets, g))
    calls = []
    monkeypatch.setattr(cli, "_login", lambda repo, n, email=None: calls.append(n) or 0)
    R = CliRunner()
    assert R.invoke(cli.app, ["account", "add", "gpt2", "codex", "--repo", str(tmp_path)]).exit_code == 0 and calls == ["gpt2"]
    r = R.invoke(cli.app, ["account", "use", "codex", "gpt2", "--repo", str(tmp_path)])
    assert r.exit_code == 0, r.output
    d = yaml.safe_load(g.read_text())
    assert d["accounts"]["gpt2"] == {"runtime": "codex"} and d["providers"]["codex"]["account"] == "gpt2"
