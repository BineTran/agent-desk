"""/account in the TUI and the account switch in SETTINGS."""
import contextlib

import yaml

from agent_desk.config.check import ProviderInfo
from agent_desk.config.loader import load
from agent_desk.plugins import default_registry
from agent_desk.runtime.registry import RuntimeRouter
from agent_desk.tui.app import AgentDeskApp
from agent_desk.tui.settings_model import SettingsModel

from .test_app import SIZE, repo, until  # noqa: F401


class Rt:
    def __init__(self, who, logged=True): self.who, self.logged, self.closed = who, logged, False
    async def info(self): return ProviderInfo({}, True, self.logged, self.who, "x", "", self.who if self.logged else "")
    def login_command(self, email=None, browser=None): return ["true"], None, True
    def logout_command(self): return ["true"], None
    async def close(self): self.closed = True


async def test_account_flow(repo, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path))                          # profile dirs land in tmp, never the real home
    g = tmp_path / "global.yaml"
    loaded = load(repo, global_path=g)
    reg = default_registry()
    rts = {"codex": Rt("me@example.com"), "claude": Rt("me@team · Acme"), "antigravity": Rt("google")}
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, rts), tmp_path / "home")
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    texts = lambda: "\n".join(m["text"] for m in app.proj.chat if m["who"] == "sys")
    async with app.run_test(size=SIZE) as pilot:
        await app._command("/account")
        await until(pilot, lambda: "me@team · Acme" in texts())
        # add an account whose browser session is the SAME login as claude: refused, not pinned
        rts["home"] = Rt("me@team · Acme")
        await app._command("/account add home claude")
        await until(pilot, lambda: "same login as claude → logged out again" in texts())
        assert "identity" not in yaml.safe_load(g.read_text())["accounts"]["home"]
        # log in again with the right account: pinned
        rts["home"].who = "me@home"
        await app._command("/account login home")
        await until(pilot, lambda: "home: logged in as me@home — pinned" in texts())
        assert yaml.safe_load(g.read_text())["accounts"]["home"]["identity"] == "me@home"
        # switch claude to it: live, written, old implicit login stays listed
        await app._command("/account use claude home")
        await until(pilot, lambda: "claude → account home" in texts())
        assert app.loaded.config.account_of("claude")[0] == "home" and "claude" in app.loaded.config.accounts
        await app._command("/account remove home")
        await until(pilot, lambda: "used by claude" in texts())
        await app._command("/account use claude claude")
        await app._command("/account remove home")
        await until(pilot, lambda: "removed account home" in texts())
        assert "home" not in app.loaded.config.accounts
        await app._command("/account help")
        await until(pilot, lambda: "/account use <provider|role> <account>" in texts())


def test_settings_cycles_accounts(tmp_path):
    reg = default_registry()
    loaded = load(None, global_path=tmp_path / "g.yaml", overrides=["accounts.team={runtime: claude-cli}"])
    from agent_desk.tui.status import StatusInfo
    m = SettingsModel(loaded, reg, StatusInfo())
    assert m.account_of("claude") == "claude" and m.accounts_for("claude") == ["team", "claude"]
    m.stage_account("claude", "team")
    st = m.pending["providers.claude.account"]
    assert st.changes["providers.claude.account"] == "team" and st.changes["accounts.claude"] == {"runtime": "claude-cli"}
    assert m.account_of("claude") == "team" and m.account_of("antigravity") is None
    m.stage_account("claude", "claude")
    assert "providers.claude.account" not in m.pending


async def test_settings_adds_an_account_and_a_role_picks_it(repo, tmp_path, monkeypatch):  # noqa: F811
    from agent_desk.tui.settings import SettingsScreen
    monkeypatch.setenv("HOME", str(tmp_path))
    g = tmp_path / "global.yaml"
    loaded = load(repo, global_path=g)
    reg = default_registry()
    rts = {"codex": Rt("me@example.com"), "claude": Rt("me@home"), "antigravity": Rt("google"), "team": Rt("me@work · Acme")}
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, rts), tmp_path / "home")
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        scr = app.screen
        body = lambda: str(scr.query_one("#s_body").render())
        assert "Accounts" in body() and "+ add account" in body()
        scr.sel = next(i for i, it in enumerate(scr.items) if it.kind == "add_account")
        await pilot.press("enter")
        assert scr.mode == "acct" and "Add account" in body()
        while scr.w["rt"] != "claude-cli":
            await pilot.press("down")
        await pilot.press(*"team", "enter")
        await until(pilot, lambda: "team" in app.loaded.config.accounts and app.loaded.config.accounts["team"].identity)
        assert app.loaded.config.accounts["team"].runtime == "claude-cli"
        assert yaml.safe_load(g.read_text())["accounts"]["team"]["identity"] == "me@work · Acme"
        await until(pilot, lambda: app.status.account_row("team") is not None)
        # the new login shows up as its own group in the architect's model picker
        scr.sel = next(i for i, it in enumerate(scr.items) if (it.kind, it.key) == ("role", "architect"))
        await pilot.press("enter")
        assert "claude@team" in body() and "me@work" in body()
        while scr.opts[scr.oc].value != ("claude", "tier", "max", "team"):
            await pilot.press("down")
        await pilot.press("enter", "enter", "s")
        assert "account: team" in body()
        await pilot.press("enter")
        await until(pilot, lambda: scr.mode == "list")
        assert app.loaded.config.roles["architect"].account == "team" and app.loaded.config.roles["architect"].provider == "claude"
        assert "role:architect" in [u for u in __import__("agent_desk.accounts", fromlist=["users"]).users(app.loaded.config, "team")]
