"""Subscription accounts: one login of an official CLI each, in its own profile dir (never the user's ~/.claude / ~/.codex).

Providers pick an account (`providers.<p>.account`) and a role may pick its own (`roles.<r>.account`), so who a role runs as is one
config line, `/account use`, or a pick in Settings (F3).
The CLI does the login and keeps the token; Agent Desk only points it at a directory and pins who it turned out to be."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config.loader import ConfigError, Loaded
from .config.schema import Account, Config
from .config.writer import DELETE, Preview, plan_change

# bundle id / config name -> (app, private-window flag)
PRIVATE = {"com.microsoft.edgemac": ("Microsoft Edge", "--inprivate"), "com.google.chrome": ("Google Chrome", "--incognito"),
           "com.brave.browser": ("Brave Browser", "--incognito"), "org.mozilla.firefox": ("Firefox", "-private-window")}
NAMES = {"edge": "com.microsoft.edgemac", "chrome": "com.google.chrome", "brave": "com.brave.browser", "firefox": "org.mozilla.firefox"}


def default_browser() -> str | None:
    """Bundle id of the macOS default https handler (None when unknown)."""
    import plistlib
    p = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    try:
        handlers = plistlib.loads(p.read_bytes()).get("LSHandlers", [])
    except Exception:
        return None
    return next((h.get("LSHandlerRoleAll") for h in handlers if h.get("LSHandlerURLScheme") == "https"), None)


def private_browser(choice: str = "auto", installed=None) -> tuple[str, str] | None:
    """(app, flag) that opens a window WITHOUT the existing claude.ai/chatgpt session; None: just print the URL."""
    installed = installed or (lambda app: Path(f"/Applications/{app}.app").exists())
    if choice == "print":
        return None
    if choice != "auto":
        return PRIVATE[NAMES[choice]]
    order = [default_browser()] + list(PRIVATE)          # default browser first; Safari/unknown -> another installed one
    return next((PRIVATE[b] for b in order if b in PRIVATE and installed(PRIVATE[b][0])), None)


def browser_shim(choice: str = "auto", root: Path | None = None) -> tuple[str, str]:
    """Write the $BROWSER script the CLIs call with the sign-in URL. Returns (path, what it opens)."""
    root = root or Path.home() / ".agent-desk" / "bin"
    root.mkdir(parents=True, exist_ok=True)
    pb = private_browser(choice)
    body = (f'open -na "{pb[0]}" --args {pb[1]} "$1"' if pb else
            'echo; echo "Open this URL in a private/incognito window and sign in with the account you want:"; echo "  $1"; echo')
    path = root / "private-browser"
    path.write_text(f"#!/bin/sh\n# written by Agent Desk: sign-in URLs open without your browser's existing session\n{body}\n")
    path.chmod(0o755)
    return str(path), (f"{pb[0]} private window" if pb else "the URL (open it in a private window)")


def login_note(name: str, opens: str) -> str:
    return f"Logging in account {name}: opening {opens} (no existing session) — sign in with the account you want for {name}."


def run_login(argv: list[str], env: dict | None, opens_itself: bool, browser: str | None) -> int:
    """Run the official login in this terminal. A device flow (codex) prints its URL: open that one in the private browser."""
    import re
    import subprocess
    if opens_itself or not browser:
        return subprocess.run(argv, env=env).returncode
    p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    opened = False
    for line in p.stdout:
        print(line, end="", flush=True)
        if not opened and (m := re.search(r"https://\S+", re.sub(r"\x1b\[[0-9;]*m", "", line))):
            subprocess.run([browser, m.group(0)])
            opened = True
    return p.wait()


def accounts(cfg: Config, reg) -> dict[str, Account]:
    """Every account: the declared ones plus the implicit one of each provider that has none (runtimes with profile dirs only)."""
    out = dict(cfg.accounts)
    for p, pc in cfg.providers.items():
        if pc.kind == "llm" and reg.has("runtime", pc.runtime) and "profiles" in reg.get("runtime", pc.runtime).capabilities:
            name, acc = cfg.account_of(p)
            out.setdefault(name, acc)
    return out


def profile_runtimes(reg) -> list[str]:
    """Runtimes whose CLI can keep a login in its own profile dir (the only ones that can have accounts)."""
    return [r for r in reg.names("runtime") if "profiles" in reg.get("runtime", r).capabilities]


def users(cfg: Config, account: str) -> list[str]:
    """Providers running as `account`, plus roles (role:<name>, role:<name>.fallback) that picked it themselves."""
    out = [p for p, pc in cfg.providers.items() if pc.kind == "llm" and cfg.account_of(p)[0] == account]
    for name, r in cfg.roles.items():
        for label, t in ((name, r), (f"{name}.fallback", r.fallback)):
            if t is not None and t.account and cfg.account_of(t.provider, t.account)[0] == account:
                out.append(f"role:{label}")
    return out


def role_accounts(cfg: Config) -> list[str]:
    """Accounts roles picked themselves (other than their provider's): they need their own login probe."""
    out = []
    for r in cfg.roles.values():
        for t in (r, r.fallback):
            if t is not None and t.account and t.account != cfg.account_of(t.provider)[0] and t.account not in out:
                out.append(t.account)
    return out


ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def resolve_runtime(cfg: Config, reg, like: str | None) -> str:
    """The runtime a new account logs in to: a runtime name (claude-cli, codex), its short form (claude), or a provider/account
    of that runtime. None is fine when only one runtime can hold accounts."""
    choices = profile_runtimes(reg)
    hint = " | ".join(choices) or "none installed"
    if like is None:
        if len(choices) == 1:
            return choices[0]
        raise ConfigError(f"which CLI does the account log in to? one of: {hint}")
    for rt in (like, f"{like}-cli"):
        if rt in choices:
            return rt
    if like in cfg.providers or like in cfg.accounts:
        rt = cfg.providers[like].runtime if like in cfg.providers else cfg.accounts[like].runtime
        if rt in choices:
            return rt
        raise ConfigError(f"{like!r} runs {rt}, which cannot keep separate accounts (choose: {hint})")
    raise ConfigError(f"unknown runtime {like!r} (choose: {hint})")


def plan_add(loaded: Loaded, reg, name: str, like: str | None = None, scope: str = "global") -> Preview:
    cfg = loaded.config
    if not ACCOUNT_NAME.fullmatch(name):
        raise ConfigError(f"account name {name!r}: use lowercase letters, digits, - or _ (e.g. team, work-2)")
    if name in cfg.accounts:
        raise ConfigError(f"account {name!r} already exists; log it in with: /account login {name}")
    return plan_change(loaded, scope, {f"accounts.{name}": {"runtime": resolve_runtime(cfg, reg, like)}})


def plan_use(loaded: Loaded, reg, target: str, account: str, scope: str = "global") -> Preview:
    """`target` is a provider (every role on it follows) or a role (only that role runs as `account`)."""
    cfg = loaded.config
    known = accounts(cfg, reg)
    if target in cfg.roles:
        r = cfg.roles[target]
        if account not in known:
            raise ConfigError(f"unknown account {account!r} (known: {', '.join(known)}); add one: /account add {account} {cfg.providers[r.provider].runtime}")
        if known[account].runtime != cfg.providers[r.provider].runtime:
            raise ConfigError(f"role {target} runs on {r.provider} ({cfg.providers[r.provider].runtime}); account {account!r} is {known[account].runtime}")
        default = cfg.account_of(r.provider)[0]
        changes: dict = {f"roles.{target}.account": DELETE if account == default else account}
        if account != default:
            changes.update(keep_implicit(cfg, known, r.provider, account, keep_current=False))
        return plan_change(loaded, scope, changes)
    if target not in cfg.providers:
        raise ConfigError(f"unknown provider or role {target!r} (providers: {', '.join(cfg.providers)}; roles: {', '.join(cfg.roles)})")
    if account not in known:
        raise ConfigError(f"unknown account {account!r} (known: {', '.join(known)}); add one: /account add {account} {cfg.providers[target].runtime}")
    changes = {f"providers.{target}.account": account}
    changes.update(keep_implicit(cfg, known, target, account))
    return plan_change(loaded, scope, changes)


def keep_implicit(cfg: Config, known: dict[str, Account], provider: str, account: str, keep_current: bool = True) -> dict:
    """Implicit accounts touched by a switch become explicit: the target (so the provider can point at it) and the one being
    left (its login dir stays logged in, so it must stay listed and selectable)."""
    out = {}
    for n in {account, *([cfg.account_of(provider)[0]] if keep_current else [])}:
        if n not in cfg.accounts and n in known:
            a = known[n]
            out[f"accounts.{n}"] = {"runtime": a.runtime, **({"identity": a.identity} if a.identity else {})}
    return out


def _scope_of(loaded: Loaded, key: str) -> str:
    src = next((v for k, v in loaded.sources.items() if k == key or k.startswith(key + ".")), "global")
    return src if src in ("global", "repo", "session") else "global"


def plan_remove(loaded: Loaded, name: str) -> Preview:
    cfg = loaded.config
    if name not in cfg.accounts:
        raise ConfigError(f"no declared account {name!r}" + (" (it is implicit: nothing to remove)" if any(cfg.account_of(p)[0] == name for p in cfg.providers) else ""))
    if used := users(cfg, name):
        raise ConfigError(f"account {name!r} is used by {', '.join(used)}; switch them first: /account use {used[0].removeprefix('role:').split('.')[0]} <other>")
    return plan_change(loaded, _scope_of(loaded, f"accounts.{name}"), {f"accounts.{name}": DELETE})


def _is_system(rt, acc: Account, name: str) -> bool:
    sys_home = getattr(rt, "system_home", None)
    return bool(sys_home) and os.path.realpath(acc.path(name)) == os.path.realpath(os.path.expanduser(sys_home))


def _account(cfg: Config, router, name: str) -> tuple[Account, object]:
    known = accounts(cfg, router.reg)
    if name not in known:
        raise ConfigError(f"unknown account {name!r} (known: {', '.join(known)}); add one: /account add {name} <{' | '.join(profile_runtimes(router.reg))}>")
    return known[name], router.for_account(name)


def login_command(cfg: Config, router, name: str, email: str | None = None, browser: str | None = None) -> tuple[list[str], dict | None, bool]:
    """(argv, env, opens_url_itself) of the account's own login; its profile dir is created first (codex refuses a missing CODEX_HOME).
    browser: a $BROWSER script (the private-window shim); None = the normal default browser."""
    acc, rt = _account(cfg, router, name)
    if _is_system(rt, acc, name):
        raise ConfigError(f"account {name!r} points at your everyday profile ({acc.path(name)}): Agent Desk never logs in there; use your own CLI")
    Path(acc.path(name)).mkdir(parents=True, exist_ok=True)
    return rt.login_command(email, browser)


def logout_command(cfg: Config, router, name: str) -> tuple[list[str], dict | None] | None:
    acc, rt = _account(cfg, router, name)
    if _is_system(rt, acc, name) or not hasattr(rt, "logout_command") or not Path(acc.path(name)).exists():
        return None
    return rt.logout_command()


@dataclass
class Row:
    name: str
    runtime: str
    home: str
    identity: str = ""          # live
    pinned: str | None = None
    used_by: list[str] = field(default_factory=list)
    state: str = "?"            # ok | not logged in | MISMATCH | DUPLICATE | error
    detail: str = ""


async def status(cfg: Config, router, timeout: float = 60) -> list[Row]:
    known = accounts(cfg, router.reg)
    tilde = os.path.expanduser("~")

    async def one(name: str, acc: Account) -> Row:
        r = Row(name, acc.runtime, acc.path(name).replace(tilde, "~", 1), pinned=acc.identity, used_by=users(cfg, name))
        if name not in getattr(router, "_fixed", {}) and not Path(acc.path(name)).is_dir():
            r.state, r.detail = "not logged in", f"/account login {name}"   # never logged in: don't start a CLI on a missing dir
            return r
        try:
            info = await asyncio.wait_for(router.for_account(name).info(), timeout)
        except Exception as e:
            r.state, r.detail = "error", str(e)[:80]
            return r
        if not info.logged_in:
            r.state, r.detail = "not logged in", f"/account login {name}"
            return r
        r.identity, r.detail = info.identity or info.account, info.account
        r.state = "MISMATCH" if acc.identity and r.identity != acc.identity else "ok"
        return r
    rows = list(await asyncio.gather(*(one(n, a) for n, a in known.items())))
    by_id: dict[str, list[Row]] = {}
    for r in rows:
        if r.identity:
            by_id.setdefault(r.identity, []).append(r)
    for same in by_id.values():
        if len(same) > 1:
            owner = [r for r in same if r.pinned == r.identity]       # the account pinned to this login keeps it
            for r in same:
                if r.state == "ok" and r not in owner:
                    r.state = "DUPLICATE"
                    r.detail = f"same login as {', '.join(x.name for x in same if x is not r)}"
    return rows


def table(rows: list[Row]) -> str:
    cells = [("ACCOUNT", "RUNTIME", "STATE", "LOGGED IN AS", "USED BY", "PROFILE")]
    for r in rows:
        who = r.identity or r.detail
        if r.state == "MISMATCH":
            who = f"{r.identity}  (pinned: {r.pinned})"
        elif r.state in ("DUPLICATE", "error"):
            who = f"{r.identity}  ({r.detail})" if r.identity else r.detail
        cells.append((r.name, r.runtime, r.state, who, ",".join(r.used_by) or "-", r.home))
    w = [max(len(c[i]) for c in cells) + 2 for i in range(5)]
    return "\n".join("".join(f"{c[i]:<{w[i]}}" for i in range(5)) + c[5] for c in cells)


async def after_login(loaded: Loaded, router, name: str, scope: str = "global", undo_duplicate: bool = True) -> tuple[str, Preview | None]:
    """Probe who the account turned out to be and pin it. If it is the same login as another account (the browser reused its
    session), log it straight out again so the wrong credentials never stay in this profile."""
    await router.forget_account(name)
    rows = await status(loaded.config, router)
    me = next(r for r in rows if r.name == name)
    if me.state in ("not logged in", "error"):
        return f"{name}: {me.state} {me.detail}".strip(), None
    others = [r.name for r in rows if r.name != name and r.identity and r.identity == me.identity]
    if others and undo_duplicate:
        msg = f"{name} got {me.identity} — the same login as {', '.join(others)}"
        if undo_duplicate and (out := logout_command(loaded.config, router, name)):
            p = await asyncio.create_subprocess_exec(*out[0], env=out[1], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
            await router.forget_account(name)
            msg += " → logged out again"
        return msg + f". Run /account login {name}: it opens a private window; sign in there with the other account. Not pinned.", None
    acc = accounts(loaded.config, router.reg)[name]
    if acc.identity == me.identity:
        return f"{name}: logged in as {me.identity} (pinned)", None
    also = f" ({', '.join(others)} has the same login and stays DUPLICATE until re-logged in)" if others else ""
    changes = {f"accounts.{name}.identity": me.identity} if name in loaded.config.accounts else \
        {f"accounts.{name}": {"runtime": acc.runtime, "identity": me.identity}}
    return f"{name}: logged in as {me.identity} — pinned; runs stop if it ever changes{also}", plan_change(loaded, scope, changes)


def migrate_text(text: str) -> tuple[str, list[str]]:
    """Old layout -> new: `providers.X: {runtime, home}` becomes `accounts.X` (and provider X + its tiers go away).
    Roles that pointed at X are moved to the provider of the same runtime, which then runs as account X."""
    import io

    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
    y = YAML()
    y.preserve_quotes = True
    doc = y.load(text) if text.strip() else None
    provs = (doc or {}).get("providers") or {}
    old = [n for n, p in provs.items() if isinstance(p, dict) and "home" in p]
    if not old:
        return text, []
    notes = []
    doc.setdefault("accounts", CommentedMap())
    for n in old:
        p = provs[n]
        acc = CommentedMap(runtime=p["runtime"])
        if os.path.expanduser(p["home"]) != os.path.expanduser(f"~/.agent-desk/accounts/{n}"):
            acc["home"] = p["home"]
        doc["accounts"][n] = acc
        del provs[n]
        if n in (doc.get("tiers") or {}):
            del doc["tiers"][n]
        notes.append(f"providers.{n} (home {p['home']}) -> accounts.{n}; pick it with: agent-desk account use <provider> {n}")
        for role, r in (doc.get("roles") or {}).items():
            if isinstance(r, dict) and r.get("provider") == n:
                notes.append(f"roles.{role} pointed at provider {n}: set it again (e.g. account use claude {n})")
                del r["provider"]
    if not provs:
        del doc["providers"]
    if not doc.get("tiers", True):
        del doc["tiers"]
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue(), notes
