"""config check: validate every role/decision point against plugins, login state and capabilities. Pure; I/O injected."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from ..plugins import Registry
from .schema import Config, Target


@dataclass
class ProviderInfo:
    models: dict[str, list[str]]                # model -> supported efforts (empty = unrestricted)
    web_search: bool = False
    logged_in: bool = True
    account: str = ""
    version: str = ""
    login_hint: str = ""                        # the command that fixes "not logged in"
    identity: str = ""                          # who it is logged in as (email/org): pinned per account, compared before runs


@dataclass
class RoleCheck:
    role: str
    provider: str
    model: str
    effort: str | None
    access: str
    auth: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def resolve_model(cfg: Config, t: Target, tier: str | None = None) -> str:
    if t.model:
        return t.model
    return cfg.tiers[t.provider][tier or t.tier or t.default_tier]


def _models(cfg: Config, t: Target) -> list[str]:
    return [resolve_model(cfg, t, x) for x in (t.tiers or [None])]


def _check_target(cfg: Config, reg: Registry, probe, env, name: str, t: Target, access: str, network: bool, label: str,
                  account_probe=None) -> RoleCheck:
    p = cfg.providers[t.provider]
    models = _models(cfg, t)
    rc = RoleCheck(label, f"{t.provider}@{t.account}" if t.account else t.provider, "/".join(dict.fromkeys(models)), t.effort, access, p.auth)
    if not reg.has("runtime", p.runtime):
        rc.problems.append(f"no runtime plugin {p.runtime!r} (available: {', '.join(reg.names('runtime'))})")
        return rc
    plug = reg.get("runtime", p.runtime)
    if access == "write" and "sandbox_write" not in plug.capabilities:
        rc.problems.append(f"runtime {p.runtime} cannot enforce a write sandbox (role needs access: write)")
    if p.auth == "env" and not env.get(p.env_key or ""):
        rc.problems.append(f"env {p.env_key} is not set")
    info = probe(t.provider)
    if info is None:
        rc.problems.append(f"provider {t.provider} unreachable")
        return rc
    profiles = "profiles" in plug.capabilities
    acc_name, acc = cfg.account_of(t.provider, t.account)
    if t.account and not profiles:
        rc.problems.append(f"runtime {p.runtime} cannot use a separate account")
    models_info = info
    if t.account and account_probe is not None and acc_name != cfg.account_of(t.provider)[0]:
        info = account_probe(acc_name) or info           # the role's own login; the model catalog stays the provider's
    rc.notes.append(" ".join(x for x in (info.account, info.version) if x))
    if profiles:
        rc.notes.insert(0, f"account {acc_name}")
    if p.auth == "login" and not info.logged_in:
        hint = f"/account login {acc_name}" if profiles else info.login_hint
        rc.problems.append(f"{rc.provider} is not logged in" + (f" — run: {hint}" if hint else ""))
    elif profiles and acc.identity and (info.identity or info.account) and (info.identity or info.account) != acc.identity:
        rc.problems.append(f"account {acc_name} is logged in as {info.identity or info.account} but pinned to {acc.identity} — /account login {acc_name}")
    info = models_info
    for m in models:
        if m not in info.models:
            rc.problems.append(f"model {m} not offered by {t.provider}")
        elif t.effort and info.models[m] and t.effort not in info.models[m]:
            rc.problems.append(f"effort {t.effort} not supported by {m}")
    if network and not info.web_search:
        rc.problems.append(f"{t.provider} has no web search (role needs network)")
    return rc


def check(cfg: Config, reg: Registry, probe: Callable[[str], ProviderInfo | None], env: dict[str, str] | None = None,
          account_probe: Callable[[str], ProviderInfo | None] | None = None) -> list[RoleCheck]:
    """probe: provider -> info (models, login of the provider's account); account_probe: account -> info, for roles that
    run as another account than their provider's."""
    env = os.environ if env is None else env
    out: list[RoleCheck] = []
    for name, r in cfg.roles.items():
        if not r.enabled:
            continue
        targets = [(r, name)] + ([(r.fallback, f"{name}.fallback")] if r.fallback else [])
        for t, label in targets:
            rc = _check_target(cfg, reg, probe, env, name, t, r.access, r.network, label, account_probe)
            rt = cfg.providers[t.provider].runtime
            if r.mcp != "none" and reg.has("runtime", rt) and "mcp" not in reg.get("runtime", rt).capabilities:
                rc.problems.append(f"runtime {rt} cannot give MCP servers to an agent (role has mcp: {r.mcp})")
            out.append(rc)
    for pname, prov in cfg.providers.items():
        if prov.account and reg.has("runtime", prov.runtime) and "profiles" not in reg.get("runtime", prov.runtime).capabilities:
            rc = RoleCheck(f"provider.{pname}", pname, "-", None, "-", prov.auth)
            rc.problems.append(f"runtime {prov.runtime} cannot use a separate account")
            out.append(rc)
    for k, pt in cfg.decision.points.items():
        rc = RoleCheck(f"decision.{k}", pt.provider or "-", pt.model or "-", None, "-", cfg.providers[pt.provider].auth if pt.provider else "-")
        if not reg.has("decision", pt.engine):
            rc.problems.append(f"no decision plugin {pt.engine!r} (available: {', '.join(reg.names('decision'))})")
        elif pt.provider:
            plug = reg.get("decision", pt.engine)
            prov = cfg.providers[pt.provider]
            want = "decision" if "provider:decision" in plug.capabilities else "llm"
            if prov.kind != want:
                rc.problems.append(f"engine {pt.engine} needs a provider of kind {want}, {pt.provider} is {prov.kind}")
            elif prov.auth == "env" and not env.get(prov.env_key or ""):
                rc.problems.append(f"env {prov.env_key} is not set")
                rc.notes.append(f"decision falls back to {pt.fallback} until it is set")
            elif prov.kind == "llm":
                info = probe(pt.provider)
                if info is None or (prov.auth == "login" and not info.logged_in):
                    rc.problems.append(f"{pt.provider} is not logged in" + (f" — run: {info.login_hint}" if info and info.login_hint else ""))
                elif pt.model and pt.model not in info.models:
                    rc.problems.append(f"model {pt.model} not offered by {pt.provider}")
        out.append(rc)
    return out


class _AnyModel(dict):
    def __contains__(self, k) -> bool:
        return True

    def __getitem__(self, k):
        return []


def structural(cfg: Config, reg: Registry) -> list[str]:
    """Problems no login or env var can fix (missing plugin, no write sandbox, wrong provider kind): block a save."""
    probe = lambda n: ProviderInfo(_AnyModel(), True, True)
    env = _AnyEnv()
    return [f"{r.role}: {p}" for r in check(cfg, reg, probe, env) for p in r.problems]


class _AnyEnv(dict):
    def get(self, k, d=None):
        return "set"
