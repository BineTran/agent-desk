from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import yaml

from .config.check import ProviderInfo, check
from .config.loader import ConfigError, load

app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True)
HOME = Path.home() / ".agent-desk"
config_app = typer.Typer(help="Inspect and validate configuration")
app.add_typer(config_app, name="config")


@app.callback()
def main(ctx: typer.Context, file: Path = typer.Option(None, "--file", "-f", help="load a brief into the input box"),
         repo: Path = typer.Option(Path("."), "--repo"), set_: list[str] = typer.Option([], "--set"),
         home: Path = typer.Option(HOME, "--home", help="where sessions and the database live")):
    """Open Agent Desk in this repo. Subcommands: init, ls, resume, show, config, headless.

    Diagnose typing/IME problems: AGENT_DESK_KEYLOG=~/.agent-desk/keys.log agent-desk (logs raw keys, off by default)."""
    if ctx.invoked_subcommand:
        return
    from .plugins import default_registry
    from .runtime.registry import RuntimeRouter
    from .tui.app import AgentDeskApp
    l = _load(repo, set_)
    reg = default_registry(l.config.plugins)
    brief = file.read_text() if file else ""
    AgentDeskApp(repo, l, reg, lambda ld: RuntimeRouter(ld.config, reg), home, brief).run()


def _load(repo: Path, sets: list[str]):
    try:
        return load(repo.resolve(), sets)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2)


@app.command("init")
def init(repo: Path = Path(".")):
    """Detect how this repo is verified and write a starter .agent-desk.yaml."""
    repo = repo.resolve()
    target = repo / ".agent-desk.yaml"
    if target.exists():
        typer.secho(f"{target} already exists; not touching it", fg="yellow")
        raise typer.Exit(1)
    checks: dict[str, str] = {}
    paths: dict[str, list[str]] = {}
    pj = repo / "package.json"
    if pj.exists():
        import json
        pkg = json.loads(pj.read_text())
        scripts = pkg.get("scripts", {})
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        pm = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "yarn" if (repo / "yarn.lock").exists() else "npm run"
        run = (lambda k: f"{pm} {k}") if pm != "npm run" else (lambda k: f"npm run {k}")
        exe = (lambda k: f"{pm} {k}") if pm != "npm run" else (lambda k: f"npx {k}")
        src = ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"]
        # scoped: only the files a session changed, so a repo with old lint/test debt is still usable
        if "@biomejs/biome" in deps:
            checks["lint"], paths["lint"] = exe("biome lint {changed}"), src
        elif "eslint" in deps:
            checks["lint"], paths["lint"] = exe("eslint {changed}"), src
        elif "lint" in scripts:
            checks["lint"] = run("lint")
        if "typecheck" in scripts:
            checks["typecheck"], paths["typecheck"] = run("typecheck"), ["**/*.ts", "**/*.tsx", "tsconfig*.json"]
        elif "typescript" in deps:
            checks["typecheck"], paths["typecheck"] = exe("tsc --noEmit -p ."), ["**/*.ts", "**/*.tsx", "tsconfig*.json"]
        if "jest" in deps:
            checks["unit"], paths["unit"] = exe("jest --findRelatedTests {changed} --passWithNoTests"), src
        elif "vitest" in deps:
            checks["unit"], paths["unit"] = exe("vitest related --run {changed} --passWithNoTests"), src
        elif "test" in scripts:
            checks["unit"] = run("test")
        if "build" in scripts:
            checks["build"], paths["build"] = run("build"), src + ["package.json", "tsconfig*.json"]
    elif (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        checks["unit"], paths["unit"] = "python3 -m pytest -q", ["**/*.py", "pyproject.toml"]
    elif (repo / "go.mod").exists():
        checks["unit"] = "go test ./..."
    elif (repo / "Cargo.toml").exists():
        checks["unit"] = "cargo test"
    lines = ["# Agent Desk — what counts as 'done' in this repo. Edit freely.", "verification:"]
    for k, v in checks.items():
        pth = (", paths: [" + ", ".join(f'"{p}"' for p in paths[k]) + "]") if k in paths else ""
        lines.append(f"  {k}: {{ command: \"{v}\"{pth}, required: {'false' if k == 'build' else 'true'} }}")
    if checks:
        lines.append("  # {changed} = the files the session changed (matching paths); a check only blocks on failures that are new vs the base commit")
    if not checks:
        lines[-1] = "verification: {}   # add checks, e.g. unit: { command: \"make test\", required: true }"
    install = next((c for f, c in (("pnpm-lock.yaml", "pnpm install --frozen-lockfile"), ("yarn.lock", "yarn install --frozen-lockfile"),
                                   ("package-lock.json", "npm ci"), ("uv.lock", "uv sync"), ("poetry.lock", "poetry install"))
                    if (repo / f).exists()), None)
    lines += ["# workspace: sessions run in a git worktree; gitignored deps (node_modules, .venv, .husky/_) are symlinked from here by default.",
              "# workspace: { setup: \"" + (install or "make deps") + "\" }   # or install a private copy per session instead"]
    lines += ["approval:", "  require_for: [git_push, destructive_filesystem, db_migration, deployment]",
              "# roles:            # change who runs what with one line each, e.g.", "#   architect: { provider: claude, model: opus }"]
    target.write_text("\n".join(lines) + "\n")
    typer.echo(f"wrote {target}\n" + "\n".join(f"  {k}: {v}" for k, v in checks.items() ) + ("" if checks else "  (no checks detected: add some)"))


account_app = typer.Typer(help="Subscription accounts: each is one login of a CLI (claude, codex) in its own profile dir (never ~/.claude or ~/.codex)")
app.add_typer(account_app, name="account")


def _accounts_ctx(repo: Path):
    from .plugins import default_registry
    from .runtime.registry import RuntimeRouter
    l = _load(repo, [])
    return l, RuntimeRouter(l.config, default_registry(l.config.plugins))


def _fail(e: Exception) -> None:
    typer.secho(str(e), fg="red", err=True)
    raise typer.Exit(2)


def _login(repo: Path, name: str, email: str | None = None, normal: bool = False) -> int:
    from .accounts import after_login, browser_shim, login_command, login_note, run_login
    from .config.writer import save
    l, router = _accounts_ctx(repo)
    browser, opens = (None, "your default browser") if normal else browser_shim(l.config.login.browser)
    try:
        argv, env, itself = login_command(l.config, router, name, email, browser)
    except (ConfigError, ValueError) as e:
        _fail(e)
    typer.echo(login_note(name, opens) + "\n")
    rc = run_login(argv, env, itself, browser)                         # the CLI keeps the token; we never see it

    async def check():
        try:
            return await after_login(l, router, name)
        finally:
            await router.close()
    msg, p = asyncio.run(check())
    if p is not None:
        save(p)
    typer.secho(msg, fg="green" if "logged in as" in msg else "yellow")
    return rc


@account_app.command("ls")
def account_ls(repo: Path = Path(".")):
    """Every account: state (ok / not logged in / MISMATCH / DUPLICATE), who it is logged in as, which providers/roles use it."""
    from .accounts import status, table
    l, router = _accounts_ctx(repo)

    async def go():
        try:
            typer.echo(table(await status(l.config, router)))
        finally:
            await router.close()
    asyncio.run(go())


@account_app.command("add")
def account_add(name: str, runtime: str = typer.Argument(None, help="the CLI it logs in to: claude-cli | codex (short: claude); optional when only one can"),
                scope: str = typer.Option("global", "--scope", help="global | repo"), repo: Path = Path("."),
                no_login: bool = typer.Option(False, "--no-login"), email: str = typer.Option(None, "--email", help="pre-fill the login page (claude)")):
    """Add an account, e.g. `account add team claude-cli`, then log it in. Pick it for a role in Settings (F3) or with `account use`."""
    from .accounts import plan_add
    from .config.writer import save
    from .plugins import default_registry
    l = _load(repo, [])
    try:
        p = plan_add(l, default_registry(l.config.plugins), name, runtime, scope)
    except ConfigError as e:
        _fail(e)
    save(p)
    typer.echo(f"added account {name} ({p.path})")
    if not no_login:
        raise typer.Exit(_login(repo, name, email))


@account_app.command("login")
def account_login(name: str, repo: Path = Path("."), email: str = typer.Option(None, "--email", help="pre-fill the login page (claude)"),
                  normal: bool = typer.Option(False, "--normal", help="use your normal browser (its existing session) instead of a private window")):
    """Log an account in with its official CLI (own profile dir, private browser window), then pin who it turned out to be."""
    raise typer.Exit(_login(repo, name, email, normal))


@account_app.command("pin")
def account_pin(name: str, repo: Path = Path(".")):
    """Pin who an already logged-in account is (runs then refuse if it ever changes)."""
    from .accounts import after_login
    from .config.writer import save
    l, router = _accounts_ctx(repo)

    async def go():
        try:
            return await after_login(l, router, name, undo_duplicate=False)
        finally:
            await router.close()
    msg, p = asyncio.run(go())
    if p is not None:
        save(p)
    typer.echo(msg)


@account_app.command("use")
def account_use(target: str = typer.Argument(..., help="a provider (every role on it follows) or a role (only that role)"), account: str = typer.Argument(...),
                scope: str = typer.Option("global", "--scope", help="global | repo"), repo: Path = Path(".")):
    """Pick who runs: `account use claude team` (the provider) or `account use architect team` (one role)."""
    from .accounts import plan_use
    from .config.writer import save
    from .plugins import default_registry
    l = _load(repo, [])
    try:
        p = plan_use(l, default_registry(l.config.plugins), target, account, scope)
    except ConfigError as e:
        _fail(e)
    save(p)
    typer.echo(f"{target} → account {account} ({p.path})")


@account_app.command("remove")
def account_remove(name: str, repo: Path = Path("."), keep_login: bool = typer.Option(False, "--keep-login", help="do not log the CLI out")):
    """Remove an account from config and log its CLI out (the profile dir itself is kept)."""
    import subprocess
    from .accounts import logout_command, plan_remove
    from .config.writer import save
    l, router = _accounts_ctx(repo)
    try:
        out = None if keep_login else logout_command(l.config, router, name)
        p = plan_remove(l, name)
    except ConfigError as e:
        _fail(e)
    if out:
        subprocess.run(out[0], env=out[1])
    save(p)
    typer.echo(f"removed account {name} ({p.path}); profile dir kept: {l.config.accounts[name].path(name)}")


@account_app.command("migrate")
def account_migrate(repo: Path = Path("."), yes: bool = typer.Option(False, "--yes", "-y")):
    """Convert the old `providers.<x>.home` layout into `accounts.<x>` (global and repo config)."""
    from .accounts import migrate_text
    from .config import loader
    for path in (loader.GLOBAL_PATH, repo.resolve() / ".agent-desk.yaml"):
        if not path.exists():
            continue
        new, notes = migrate_text(path.read_text())
        if not notes:
            continue
        typer.echo(f"{path}:\n  " + "\n  ".join(notes))
        if yes or typer.confirm(f"write {path}?"):
            path.write_text(new)
            typer.echo(f"saved {path}")


@app.command("ls")
def ls(home: Path = HOME):
    """List sessions."""
    import sqlite3
    db = home / "db" / "agent-desk.sqlite"
    if not db.exists():
        typer.echo("no sessions yet")
        return
    c = sqlite3.connect(db)
    typer.echo(f"{'ID':<8}{'STARTED':<18}{'REPO':<22}{'STATUS':<15}TASK")
    for sid, created, repo, status, title in c.execute("select id, created_at, repo, status, title from sessions order by created_at desc limit 30"):
        typer.echo(f"{sid:<8}{created[:16].replace('T', ' '):<18}{Path(repo).name:<22}{status:<15}{title.splitlines()[0][:60]}")


@app.command("resume")
def resume(session_id: str, home: Path = HOME, set_: list[str] = typer.Option([], "--set")):
    """Reopen an earlier session (same worktree, branch, memory and Main thread) and continue it."""
    import sqlite3
    db = home / "db" / "agent-desk.sqlite"
    row = sqlite3.connect(db).execute("select repo from sessions where id=?", (session_id,)).fetchone() if db.exists() else None
    if row is None:
        typer.secho(f"no session {session_id} (see `agent-desk ls`)", fg="red")
        raise typer.Exit(1)
    from .plugins import default_registry
    from .runtime.registry import RuntimeRouter
    from .tui.app import AgentDeskApp
    repo = Path(row[0])
    l = _load(repo, set_)
    reg = default_registry(l.config.plugins)
    AgentDeskApp(repo, l, reg, lambda ld: RuntimeRouter(ld.config, reg), home, reopen=session_id).run()


@app.command("show")
def show_session(session_id: str, home: Path = HOME, repo: Path = Path(".")):
    """Replay a finished session from its events (read-only): plan, decisions, verification, result."""
    import sqlite3

    from rich.console import Console

    from .contracts import Event
    from .tui import views
    from .tui.projection import Projection
    import json
    db = home / "db" / "agent-desk.sqlite"
    if not db.exists():
        typer.secho("no database", fg="red"); raise typer.Exit(1)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    srow = c.execute("select * from sessions where id=?", (session_id,)).fetchone()
    if not srow:
        typer.secho(f"no session {session_id}", fg="red"); raise typer.Exit(1)
    proj = Projection()
    for r in c.execute("select * from events where session_id=? order by seq", (session_id,)):
        proj.apply(Event(session_id=r["session_id"], seq=r["seq"], ts=r["ts"], source=r["source"], type=r["type"], task_id=r["task_id"],
                         agent_run_id=r["agent_run_id"], payload=json.loads(r["payload_json"])))
    proj.finish(srow["status"])
    con = Console()
    l = load(Path(srow["repo"]), [])
    con.print(views.header(proj, Path(srow["repo"]).name))
    con.print(views.plan_view(proj))
    con.print(views.run_view(proj, views.pv("codex", "")))
    con.print(views.result_view(proj, l.config, {}))


@config_app.command("show")
def show(repo: Path = Path("."), set_: list[str] = typer.Option([], "--set")):
    """Resolved config; every top-level value tagged with the layer it came from."""
    l = _load(repo, set_)
    typer.echo(yaml.safe_dump(l.snapshot(), sort_keys=False))
    typer.echo(f"# hash {l.snapshot_hash()}")
    for k, v in sorted(l.sources.items()):
        if v != "default":
            typer.echo(f"# {k}: {v}")


@config_app.command("set")
def set_cmd(items: list[str] = typer.Argument(..., help="key=value (YAML value) or key=- to remove"),
            scope: str = typer.Option("repo", "--scope", help="repo | global"), repo: Path = Path("."),
            yes: bool = typer.Option(False, "--yes", "-y", help="write without asking")):
    """Change config in .agent-desk.yaml (repo) or ~/.agent-desk/config.yaml (global); validated before writing, comments kept."""
    from .config.writer import DELETE, plan_change, save
    if scope not in ("repo", "global"):
        typer.secho("--scope must be repo or global", fg="red", err=True); raise typer.Exit(2)
    changes = {}
    for it in items:
        if "=" not in it:
            typer.secho(f"expected key=value, got {it!r}", fg="red", err=True); raise typer.Exit(2)
        k, v = it.split("=", 1)
        changes[k] = DELETE if v == "-" else yaml.safe_load(v)
    try:
        p = plan_change(_load(repo, []), scope, changes)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True); raise typer.Exit(2)
    typer.echo(p.diff() or "(no change)")
    from .config.check import structural
    from .plugins import default_registry
    if probs := structural(p.new.config, default_registry(p.new.config.plugins)):
        typer.secho("refused:\n  " + "\n  ".join(probs), fg="red", err=True); raise typer.Exit(2)
    if p.before == p.after or not (yes or typer.confirm(f"write {p.path}?")):
        raise typer.Exit(0)
    save(p)
    typer.echo(f"saved {p.path} · hash {p.new.snapshot_hash()}")


@config_app.command("check")
def check_cmd(repo: Path = Path("."), set_: list[str] = typer.Option([], "--set"),
              checks: bool = typer.Option(False, "--checks", help="also run the verification commands on a clean checkout (doctor)"),
              refresh: bool = typer.Option(False, "--refresh", help="with --checks: ignore cached results"), home: Path = HOME):
    """Validate every role and decision point: plugin exists, logged in, model offered, capabilities, fallback."""
    from .accounts import role_accounts
    from .plugins import default_registry
    from .runtime.registry import RuntimeRouter
    l = _load(repo, set_)
    reg = default_registry(l.config.plugins)

    async def go():
        router = RuntimeRouter(l.config, reg)
        infos, acc_infos = {}, {}
        try:
            for name, p in l.config.providers.items():
                if p.kind == "llm" and reg.has("runtime", p.runtime):
                    try:
                        infos[name] = await router.info(name)
                    except Exception as e:
                        typer.secho(f"{name}: {e}", fg="red", err=True)
                        infos[name] = None
            for acc in role_accounts(l.config):
                try:
                    acc_infos[acc] = await router.for_account(acc).info()
                except Exception as e:
                    typer.secho(f"account {acc}: {e}", fg="red", err=True)
        finally:
            await router.close()
        return check(l.config, reg, lambda n: infos.get(n), account_probe=lambda a: acc_infos.get(a))

    rows = asyncio.run(go())
    pw = max([10] + [len(r.provider) + 2 for r in rows])
    typer.echo(f"{'role':<24}{'provider':<{pw}}{'model':<26}{'effort':<8}{'access':<7}{'auth':<7}status")
    bad = False
    for r in rows:
        bad |= not r.ok
        extra = ("  (" + "; ".join(n for n in r.notes if n) + ")") if r.notes and r.ok else ""
        typer.echo(f"{r.role:<24}{r.provider:<{pw}}{r.model:<26}{(r.effort or '-'):<8}{r.access:<7}{r.auth:<7}"
                   + ("ok" + extra if r.ok else "FAIL: " + "; ".join(r.problems)))
    pol = l.config.policy
    typer.echo(f"policy: subscription_only={'✓' if pol.subscription_only else 'off'}"
               + (f" · API-key exceptions: {', '.join(pol.allow_api_key)}" if pol.allow_api_key else " · no provider uses an API key"))
    if checks:
        bad |= _doctor(repo.resolve(), l.config, home, refresh)
    elif l.config.verification:
        typer.echo("verification: not run (add --checks to try every command on a clean checkout)")
    raise typer.Exit(1 if bad else 0)


def _doctor(repo: Path, cfg, home: Path, refresh: bool = False) -> bool:
    """Run each verification command on a clean checkout of HEAD; True if a required one is broken there."""
    from .baseline import BaselineRunner, doctor
    from .gitws import git

    async def go():
        head = await git(repo, "rev-parse", "HEAD")
        runner = BaselineRunner(repo, head, home / "doctor" / head[:12], home / "baseline", cfg.workspace)
        try:
            return head, await doctor(runner, cfg.verification, refresh=refresh)
        finally:
            await runner.close()

    if not cfg.verification:
        typer.echo("verification: none configured"); return False
    head, rows = asyncio.run(go())
    typer.echo(f"verification on a clean checkout of {head[:7]}:")
    broken = False
    for r in rows:
        ok = r["state"] in ("ok", "works (existing failures)")
        broken |= (not ok and r["state"] != "untested" and r.get("required", True))
        typer.secho(f"  {('✓' if r['state'] == 'ok' else '•') if ok else '✗'} {r['name']:<10} {r['state']}" + (f" — {r['detail']}" if r.get("detail") else ""),
                    fg=("green" if r["state"] == "ok" else "yellow") if ok else "yellow" if r["state"] == "untested" else "red")
        typer.echo(f"      $ {r['command']}"[:200])
        for n in r.get("notes") or []:
            typer.secho(f"      ! {n}", fg="yellow")
    if broken:
        typer.echo("  sessions only block on NEW failures, but a check that is broken on a clean checkout verifies nothing:\n"
                   "  scope it (paths + {changed}) or fix the command; e.g. lint: { command: \"yarn biome lint {changed}\", paths: [\"**/*.ts\"] }")
    return broken


@app.command("headless")
def headless(task: str, repo: Path = Path("."), set_: list[str] = typer.Option([], "--set"),
             home: Path = Path.home() / ".agent-desk"):
    """Run a task end-to-end with no UI (accepts recommended answers, auto-approves the plan, refuses risky commands)."""
    from .graph import open_session, run_graph
    from .plugins import default_registry
    from .runtime.registry import RuntimeRouter
    from .ui import HeadlessUI
    l = _load(repo, set_)

    async def go():
        rt = RuntimeRouter(l.config, default_registry(l.config.plugins))
        s = await open_session(repo.resolve(), task, l, rt, HeadlessUI(), home)

        async def tail():
            async for e in s.bus.subscribe(s.sid):
                typer.echo(f"{e.ts[11:19]} {e.source:<9} {e.type:<22} {str(e.payload)[:110]}")
                if e.type == "final.summary" or (e.type == "completion.gate" and e.payload["unmet"]):
                    return
        t = asyncio.create_task(tail())
        try:
            out = await run_graph(s)
            await asyncio.sleep(0.3)
        finally:
            t.cancel()
            await s.close()
        typer.echo(f"\n{out}  session={s.sid}  branch={s.ws.branch}\nworktree={s.ws.path}")
        return out
    out = asyncio.run(go())
    raise typer.Exit(0 if out == "COMPLETED" else 1)


if __name__ == "__main__":
    app()
