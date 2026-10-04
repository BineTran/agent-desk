"""Strict config schema. Secrets are referenced by env var NAME only; subscription login is the default."""
from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

RoleName = Literal["main", "explorer", "worker", "researcher", "architect", "deployer", "debug"]
POINTS = ("route", "tier", "dangerous", "retry_or_stop")
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provider(Strict):
    runtime: str                                  # plugin name in the 'runtime' or 'decision' registry
    kind: Literal["llm", "decision"] = "llm"
    auth: Literal["login", "env"] = "login"
    env_key: str | None = None
    account: str | None = None                    # which login (accounts.<name>) it runs as; unset: the implicit account named like the provider

    @model_validator(mode="before")
    @classmethod
    def _old_home(cls, d):
        if isinstance(d, dict) and "home" in d:
            raise ValueError("providers.<name>.home was replaced by accounts (one login per account, picked with providers.<name>.account): "
                             "run `agent-desk account migrate`")
        return d

    @model_validator(mode="after")
    def _check(self):
        if self.account and self.auth != "login":
            raise ValueError("account is only for auth: login")
        if self.auth == "env" and not self.env_key:
            raise ValueError("auth: env needs env_key (the variable NAME)")
        if self.auth == "login" and self.env_key:
            raise ValueError("auth: login must not set env_key")
        if self.env_key and not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.env_key):
            raise ValueError("env_key must be an environment variable NAME, not a value")
        return self


ACCOUNTS_ROOT = "~/.agent-desk/accounts"


class Account(Strict):
    """One subscription login of an official CLI, kept in its own profile dir (the CLI holds the token, never Agent Desk)."""
    runtime: str
    home: str | None = None                       # unset: ~/.agent-desk/accounts/<name>
    identity: str | None = None                   # pinned after login (email/org); a different live identity blocks runs

    def path(self, name: str) -> str:
        return os.path.expanduser(self.home or f"{ACCOUNTS_ROOT}/{name}")


class Target(Strict):
    """A provider+model selector, used for roles and their fallbacks."""
    provider: str
    model: str | None = None
    tier: str | None = None
    tiers: list[str] | None = None
    default_tier: str | None = None
    effort: str | None = None
    account: str | None = None                    # which login runs it (accounts.<name>); unset: the provider's account

    @model_validator(mode="after")
    def _sel(self):
        if sum(x is not None for x in (self.model, self.tier, self.tiers)) != 1:
            raise ValueError("needs exactly one of model / tier / tiers")
        if self.tiers and self.default_tier not in self.tiers:
            raise ValueError("default_tier must be one of tiers")
        return self


class Limits(Strict):
    """Per-run budget. Over it, the runtime interrupts the agent and asks for a report with what it already has."""
    max_input_tokens: int | None = None          # cumulative input of the run (cached included): context x model calls
    max_tool_calls: int | None = None


class Role(Target):
    access: Literal["read", "write"] = "read"
    network: bool = False
    mcp: Literal["none", "all"] | list[str] = "none"    # MCP servers the role may use: none, all of the user's, or these names
    enabled: bool = True
    fallback: Target | None = None
    limits: Limits = Limits()


class Point(Strict):
    engine: str = "none"                          # decision plugin name: none | llm | jev | replay | ...
    provider: str | None = None
    model: str | None = None
    sharp_threshold: float = 0.8
    fallback: Literal["main", "user"] = "main"
    shadow: dict | None = None                    # {engine, provider, model}: recorded, never decides


class Decision(Strict):
    fallback: Literal["main", "user"] = "main"
    points: dict[str, Point] = {}

    @model_validator(mode="after")
    def _keys(self):
        for k in self.points:
            if k not in POINTS:
                raise ValueError(f"unknown decision point {k!r}; known: {', '.join(POINTS)}")
        return self


class Policy(Strict):
    subscription_only: bool = True
    allow_api_key: list[str] = []


class Scheduler(Strict):
    max_total_agents: int = 6
    max_writers: int = 1
    max_retries_per_task: int = 2


class Context(Strict):
    packet_max_tokens: int = 6000
    failure_tail_lines: int = 60
    main_rotate_at: float = 0.6


class Review(Strict):
    triggers: list[Literal["before_plan", "error_repeats", "before_done"]] = ["before_plan", "error_repeats", "before_done"]
    max_rounds: int = 2                       # per trigger: findings -> fixes -> re-review
    max_replans_per_task: int = 1


class Quota(Strict):
    on_limit: Literal["wait", "switch"] = "wait"


class Check(Strict):
    command: str                           # may contain {changed}: the changed files matching `paths`, shell-quoted
    required: bool = True
    paths: list[str] | None = None         # globs: the check only applies when a changed file matches (else: skipped)
    writes: bool = False                   # false: anything the command rewrites (lint --write) is reverted and reported


class WorkspaceCfg(Strict):
    """How a session worktree gets the gitignored dependencies it needs to build/test."""
    link: list[str] = ["node_modules", ".venv", "venv", "vendor/bundle", ".husky/_"]   # symlinked from your checkout (fast, shared)
    setup: str | None = None                  # e.g. "yarn install --frozen-lockfile": real install in the worktree instead
    commit_message: str = "chore(agent-desk): {title} ({task})"   # {title} first letter lowercased, {task}, {session}; must pass your commit hooks


class LoginCfg(Strict):
    browser: Literal["auto", "edge", "chrome", "brave", "firefox", "print"] = "auto"   # private window for /account login


class Approval(Strict):
    require_for: list[str] = []
    env_gate: Literal["debug", "user", "deny"] = "debug"   # who decides when an agent reads env vars / secret files


class Config(Strict):
    policy: Policy = Policy()
    plugins: list[str] = []
    providers: dict[str, Provider]
    accounts: dict[str, Account] = {}
    tiers: dict[str, dict[str, str]] = {}
    roles: dict[RoleName, Role]
    decision: Decision = Decision()
    scheduler: Scheduler = Scheduler()
    context: Context = Context()
    quota: Quota = Quota()
    review: Review = Review()
    verification: dict[str, Check] = {}
    approval: Approval = Approval()
    login: LoginCfg = LoginCfg()
    workspace: WorkspaceCfg = WorkspaceCfg()

    def account_of(self, provider: str, account: str | None = None) -> tuple[str, Account]:
        """The account a provider runs as (or `account`, a role's own pick): the declared one, else an implicit one named like
        the provider (own dir, never ~/.claude)."""
        p = self.providers[provider]
        name = account or p.account or provider
        return name, self.accounts.get(name) or Account(runtime=p.runtime)

    def _target_tiers(self, where: str, t: Target) -> None:
        if t.provider not in self.providers:
            raise ValueError(f"{where}: unknown provider {t.provider!r}")
        if self.providers[t.provider].kind != "llm":
            raise ValueError(f"{where}: provider {t.provider!r} is not an llm provider")
        for x in ([t.tier] if t.tier else []) + (t.tiers or []):
            if x not in self.tiers.get(t.provider, {}):
                raise ValueError(f"{where}: tier {x!r} not defined for provider {t.provider!r}")
        if t.account:
            p = self.providers[t.provider]
            if p.auth != "login":
                raise ValueError(f"{where}: account is only for providers with auth: login ({t.provider!r} uses {p.auth})")
            if t.account not in self.accounts:
                raise ValueError(f"{where}: unknown account {t.account!r} (known: {', '.join(self.accounts) or 'none'})")
            if self.accounts[t.account].runtime != p.runtime:
                raise ValueError(f"{where}: provider {t.provider!r} ({p.runtime}) cannot run as account {t.account!r} "
                                 f"({self.accounts[t.account].runtime})")

    @model_validator(mode="after")
    def _refs(self):
        for name, r in self.roles.items():
            self._target_tiers(f"role {name}", r)
            if r.fallback:
                self._target_tiers(f"role {name} fallback", r.fallback)
        for pname, p in self.providers.items():
            if p.account:
                if p.account not in self.accounts:
                    raise ValueError(f"provider {pname!r}: unknown account {p.account!r} (known: {', '.join(self.accounts) or 'none'})")
                if self.accounts[p.account].runtime != p.runtime:
                    raise ValueError(f"provider {pname!r} ({p.runtime}) cannot use account {p.account!r} ({self.accounts[p.account].runtime})")
        homes: dict[str, str] = {}
        for aname, a in self.accounts.items():
            if (h := a.path(aname)) in homes:
                raise ValueError(f"accounts {homes[h]!r} and {aname!r} use the same profile dir {h}")
            homes[h] = aname
        for name, r in self.roles.items():
            if r.fallback and (r.fallback.provider, r.fallback.account) != (r.provider, r.account):
                a, b = self.providers[r.provider], self.providers[r.fallback.provider]
                if a.runtime == b.runtime and self.account_of(r.provider, r.account)[0] != self.account_of(r.fallback.provider, r.fallback.account)[0]:
                    raise ValueError(f"role {name}: fallback {r.fallback.provider!r} is another account of the same runtime ({a.runtime}); "
                                     "switching accounts on a quota hit is not allowed - fall back to another runtime or model")
        for pname, p in self.providers.items():
            if p.auth == "env" and self.policy.subscription_only and pname not in self.policy.allow_api_key:
                raise ValueError(f"provider {pname!r} uses an API key but policy.subscription_only is on; "
                                 f"add it to policy.allow_api_key to allow this exception")
        for k, pt in self.decision.points.items():
            if pt.provider and pt.provider not in self.providers:
                raise ValueError(f"decision point {k}: unknown provider {pt.provider!r}")
            if pt.engine != "none" and not pt.provider:
                raise ValueError(f"decision point {k}: engine {pt.engine!r} needs a provider")
        return self
