import pytest

from agent_desk.config.check import ProviderInfo
from agent_desk.config.loader import load
from agent_desk.plugins import Plugin, default_registry
from agent_desk.runtime.mock import MockRuntime
from agent_desk.runtime.registry import RuntimeRouter
from agent_desk.tui.app import AgentDeskApp
from agent_desk.tui.settings import SettingsScreen

from ..workflow.conftest import Script, plan, task
from .test_app import SIZE, repo, until  # noqa: F401  (fixture)


class FakeAgy(MockRuntime):
    async def info(self):
        return ProviderInfo({"gem-flash-medium": [], "gem-pro-high": []}, False, True, "google", "agy 9")


def make(repo, tmp_path):
    loaded = load(repo, global_path=tmp_path / "home" / "config.yaml")
    reg = default_registry()
    reg.register(Plugin("runtime", "fake-agy", lambda: FakeAgy(Script(plan(task("t1")))), frozenset({"structured_output"})))
    rt = FakeAgy(Script(plan(task("t1"))))
    return AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")


async def open_settings(pilot, app):
    await pilot.press("ctrl+s")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
    await until(pilot, lambda: "antigravity" in app.status.infos)
    app.screen.status_updated()
    return app.screen


def body(scr):
    return str(scr.query_one("#s_body").render())


async def goto(pilot, scr, kind, key):
    for _ in range(40):
        it = scr.items[scr.sel]
        if (it.kind, it.key) == (kind, key):
            return
        await pilot.press("down")
    raise AssertionError(f"no row {kind} {key}")


async def test_role_change_via_keyboard_saves_repo(repo, tmp_path):
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "role", "explorer")
        await pilot.press("enter")
        assert scr.mode == "picker" and "choose a model" in body(scr)
        while scr.opts[scr.oc].value != ("antigravity", "tier", "strong", None):
            await pilot.press("down")
        await pilot.press("enter")
        assert scr.mode == "picker" and scr.pstep == "effort" and "effort for gem-pro-high" in body(scr) or "effort for" in body(scr)
        await pilot.press("right", "enter")              # low -> medium
        assert "● edited" in body(scr) and "1 change pending" in str(scr.query_one("#s_head").render())
        await pilot.press("s")
        assert scr.mode == "review" and "+" in body(scr) and "✓ valid" in body(scr)
        await pilot.press("enter")
        await until(pilot, lambda: scr.mode == "list")
        assert "antigravity" in (repo / ".agent-desk.yaml").read_text() and "verification" in (repo / ".agent-desk.yaml").read_text()
        assert app.loaded.config.roles["explorer"].provider == "antigravity" and app.loaded.sources["roles.explorer.provider"] == "repo"
        assert app.loaded.config.roles["explorer"].effort == "medium" and "effort: medium" in (repo / ".agent-desk.yaml").read_text()


async def test_write_role_shows_reason_and_cannot_pick(repo, tmp_path):
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "role", "worker")
        await pilot.press("enter")
        while scr.opts[scr.oc].value[0] != "antigravity":
            await pilot.press("down")
        assert "no write sandbox" in body(scr)
        await pilot.press("enter")
        assert not scr.m.pending and "no write sandbox" in str(scr.query_one("#s_keys").render())


async def test_add_provider_wizard_saves_global(repo, tmp_path):
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "add", "add")
        await pilot.press("enter")
        while scr.w["rt"] != "fake-agy":
            await pilot.press("down")
        for _ in range(len(scr.w["name"])):
            await pilot.press("backspace")
        await pilot.press("g", "e", "m", "enter")
        await until(pilot, lambda: scr.w["tiers"])
        assert scr.w["tiers"] == {"fast": "gem-flash-medium", "strong": "gem-pro-high"}
        await pilot.press("enter", "enter")
        assert scr.mode == "list" and "providers.gem" in scr.m.pending
        await pilot.press("s", "enter")
        await until(pilot, lambda: not scr.m.pending)
        g = (tmp_path / "home" / "config.yaml").read_text()
        assert "gem:" in g and "gem-pro-high" in g and app.loaded.config.tiers["gem"]["fast"] == "gem-flash-medium"


async def test_jev_wizard_and_mixed_review_then_disable(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "jev", "jev")
        await pilot.press("enter")
        assert "not set ✗" in body(scr) and "export TYPESAFE_API_KEY" in body(scr)
        await pilot.press("enter")                       # step 2: points (retry_or_stop preselected)
        await pilot.press("space")                       # + route
        await pilot.press("enter", "enter")
        assert "jev" in scr.m.pending
        await goto(pilot, scr, "role", "main")
        await pilot.press("e", "right", "enter")         # effort only: high -> xhigh on main (repo)
        assert scr.m.pending["roles.main"].changes == {"roles.main.effort": "xhigh"}
        await pilot.press("s")
        assert [p.scope for p in scr.previews] == ["global", "repo"]
        await pilot.press("enter")
        await until(pilot, lambda: not scr.m.pending)
        cfg = app.loaded.config
        assert cfg.decision.points["route"].engine == "jev" and cfg.decision.points["retry_or_stop"].engine == "jev"
        assert "typesafe" in cfg.policy.allow_api_key and cfg.roles["main"].effort == "xhigh"
        await goto(pilot, scr, "jev", "jev")
        await pilot.press("enter", "ctrl+x", "s", "enter")
        await until(pilot, lambda: "typesafe" not in app.loaded.config.providers)


async def test_session_scope_and_esc_guards_unsaved(repo, tmp_path):
    app = make(repo, tmp_path)
    before = (repo / ".agent-desk.yaml").read_text()
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "point", "retry_or_stop")
        await pilot.press("enter")
        while scr.opts[scr.oc].value != ("none", None, None):
            await pilot.press("down")
        await pilot.press("enter", "s", "1")
        assert list(scr.m.pending.values())[0].scope == "session"
        await pilot.press("escape")                      # back from review, change still staged
        await pilot.press("escape")                      # warns
        assert isinstance(app.screen, SettingsScreen) and "unsaved" in str(scr.query_one("#s_keys").render())
        await pilot.press("s", "enter")
        await until(pilot, lambda: not scr.m.pending)
        assert app.loaded.config.decision.points["retry_or_stop"].engine == "none"
        assert app.loaded.sources["decision.points.retry_or_stop.engine"] == "session"
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
    assert (repo / ".agent-desk.yaml").read_text() == before and not (tmp_path / "home" / "config.yaml").exists()


async def test_effort_step_back_and_codex_model_limits(repo, tmp_path):
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        app.status.infos["codex"] = ProviderInfo({"gpt-5.6-luna": ["low", "medium"], "gpt-6.1-sol": ["low", "medium", "high"]}, True, True)
        await goto(pilot, scr, "role", "main")           # main: gpt-6.1-sol, effort high
        await pilot.press("enter")
        while scr.opts[scr.oc].value != ("codex", "tier", "fast", None):
            await pilot.press("down")
        await pilot.press("enter")
        assert scr.effs == [None, "low", "medium"] and scr.eff is None and "not supported" in body(scr)
        await pilot.press("escape")                      # back to the model list, picker still open
        assert scr.mode == "picker" and scr.pstep == "model"
        await pilot.press("escape")
        assert scr.mode == "list" and not scr.m.pending


async def test_jev_key_is_env_only_and_never_shown(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    secret = "tsk_live_" + "Q" * 24
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        await goto(pilot, scr, "jev", "jev")
        await pilot.press("k")
        assert scr.mode == "jev" and scr.w["field"] == "key"
        await pilot.press(*list(secret[:5]))
        app.screen.post_message(__import__("textual.events", fromlist=["Paste"]).Paste(secret[5:]))
        await pilot.pause(0.1)
        assert secret not in body(scr) and "•" in body(scr) and f"({len(secret)} chars)" in body(scr)
        await pilot.press("enter")
        assert scr.mode == "list" and __import__("os").environ["TYPESAFE_API_KEY"] == secret
        assert "this run only" in body(scr) or scr.m.key_state("TYPESAFE_API_KEY")[0].startswith("set ✓ (this run")
        assert secret not in body(scr) and not scr.m.pending
    from agent_desk.events.redact import redact_text
    assert redact_text(f"x {secret} y") == "x [REDACTED] y"
    for f in (repo / ".agent-desk.yaml", tmp_path / "home" / "config.yaml"):
        assert not f.exists() or secret not in f.read_text()


async def test_more_row_expands_all_models_of_a_provider(repo, tmp_path):
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_settings(pilot, app)
        app.status.infos["antigravity"] = ProviderInfo({m: [] for m in ("gem-flash-medium", "gem-pro-high", "claude-opus-5-5-high", "gpt-oss-120b")}, False, True)
        await goto(pilot, scr, "role", "explorer")
        await pilot.press("enter")
        assert "claude-opus-5-5-high" not in body(scr) and "more models" in body(scr)
        for _ in range(30):
            if scr.more_sel == "antigravity":
                break
            await pilot.press("down")
        await pilot.press("enter")
        assert "claude-opus-5-5-high" in body(scr) and "show fewer" in body(scr)
        while not (scr.more_sel is None and scr.opts[scr.oc].value == ("antigravity", "model", "claude-opus-5-5-high", None)):
            await pilot.press("down")
        await pilot.press("enter", "enter")
        assert scr.m.pending["roles.explorer"].changes["roles.explorer.model"] == "claude-opus-5-5-high"


async def test_composer_handles_vietnamese_ime_sequences(repo, tmp_path):
    from textual import events
    from agent_desk.tui.composer import Composer
    app = make(repo, tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        c = app.query_one("#composer", Composer)
        await pilot.press("a", "backspace", "á", "n", "h")
        assert c.text == "ánh"
        c.load_text("")
        await pilot.press("a", "ctrl+h", "á")                       # IME backspace as BS (0x08)
        assert c.text == "á"
        c.load_text("vie")
        c.move_cursor((0, 3))
        app.post_message(events.Paste("\x08ệt"))                     # burst: backspace + replacement
        await pilot.pause(0.1)
        assert c.text == "việt"
        c.load_text("")
        await pilot.press(*"Tiếng Việt có dấu")
        assert c.text == "Tiếng Việt có dấu"
