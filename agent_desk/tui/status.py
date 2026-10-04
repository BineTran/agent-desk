"""Collects what the status bar / COMPONENTS screen need, from the same plugin sources as `config check`."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderStatus:
    name: str
    runtime: str
    logged_in: bool | None = None
    account: str = ""
    version: str = ""
    quota: dict = field(default_factory=dict)       # window -> percent used
    error: str = ""
    login_hint: str = ""
    account_name: str = ""                          # the account it runs as (runtimes with profile dirs)
    mismatch: str = ""                              # logged in as someone else than the pinned identity: runs refuse


@dataclass
class StatusInfo:
    providers: dict[str, ProviderStatus] = field(default_factory=dict)
    infos: dict = field(default_factory=dict)       # provider -> ProviderInfo (for config check)
    accounts: list = field(default_factory=list)    # accounts.Row per account (state, who, used by) for SETTINGS
    account_infos: dict = field(default_factory=dict)   # account -> ProviderInfo, for roles that picked their own account
    accounts_checked: bool = False

    def account_row(self, name: str):
        return next((r for r in self.accounts if r.name == name), None)


async def gather(router, cfg) -> StatusInfo:
    out = StatusInfo()
    for name, p in cfg.providers.items():
        if p.kind != "llm":
            continue
        ps = ProviderStatus(name, p.runtime)
        out.providers[name] = ps
        acc_name, acc = cfg.account_of(name)
        profiles = hasattr(router, "reg") and router.reg.has("runtime", p.runtime) and "profiles" in router.reg.get("runtime", p.runtime).capabilities
        try:
            info = await router.info(name)
            out.infos[name] = info
            if info:
                ps.logged_in, ps.account, ps.version, ps.login_hint = info.logged_in, info.account, info.version, info.login_hint
                if profiles:
                    ps.account_name, ps.login_hint = acc_name, f"/account login {acc_name}"
                    live = info.identity or info.account
                    if info.logged_in and acc.identity and live != acc.identity:
                        ps.mismatch = f"logged in as {live}, pinned to {acc.identity}"
            rt = router.runtime(name)
            if hasattr(rt, "quota"):
                ps.quota = await rt.quota()
        except Exception as e:                       # a broken provider must not break the UI
            ps.error = f"{type(e).__name__}: {e}"[:120]
    if hasattr(router, "reg"):
        from ..accounts import role_accounts, status
        for acc in role_accounts(cfg):
            try:
                out.account_infos[acc] = await router.for_account(acc).info()
            except Exception:
                pass
        try:
            out.accounts = await status(cfg, router, timeout=30)
            out.accounts_checked = True
        except Exception:
            pass
    return out
