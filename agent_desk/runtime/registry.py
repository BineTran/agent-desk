"""RuntimeRouter: one runtime instance per provider, created lazily from the plugin registry."""
from __future__ import annotations

import asyncio

from ..config.schema import Config
from ..plugins import Registry
from .base import ApprovalHandler, EventSink, RunResult, RunSpec


class RuntimeRouter:
    """Instances are keyed by provider and rebuilt when the provider's account (profile dir) changes in the live config."""

    def __init__(self, cfg: Config, reg: Registry, overrides: dict | None = None):
        self.cfg, self.reg = cfg, reg
        self._fixed: dict[str, object] = overrides if overrides is not None else {}   # injected (tests), live: never rebuilt
        self._rt: dict[str, object] = {}                     # provider | provider@account | @account -> instance built here
        self._home: dict[str, str | None] = {}
        self._by_run: dict[str, tuple[str, str | None]] = {}  # run id -> (provider, account)
        self._verified: dict[str, str] = {}                  # instance key -> pinned identity confirmed on its current instance

    def _build(self, key: str, runtime: str, account: str, acc) -> object:
        plugin = self.reg.get("runtime", runtime)
        if "profiles" not in plugin.capabilities:
            if acc.home or account in self.cfg.accounts:
                raise ValueError(f"{key}: runtime {runtime!r} cannot use a separate account")
            home = None                                      # e.g. agy: no profile dirs, uses its own login
        else:
            home = acc.path(account)
        if key in self._rt and self._home.get(key, home) == home:
            return self._rt[key]
        if (old := self._rt.pop(key, None)) is not None:     # account switched under us: never keep the old login
            try:
                asyncio.get_running_loop().create_task(old.close())
            except RuntimeError:
                pass
        self._rt[key] = plugin.factory(home=home) if home else plugin.factory()
        self._home[key] = home
        self._verified.pop(key, None)
        return self._rt[key]

    def _key(self, provider: str, account: str | None = None) -> str:
        """Instance key: the provider alone when it runs as its own account, else provider@account (a role's own pick)."""
        if not account or account == self.cfg.account_of(provider)[0]:
            return provider
        return f"{provider}@{account}"

    def runtime(self, provider: str, account: str | None = None):
        if provider in self._fixed:
            return self._fixed[provider]
        key = self._key(provider, account)
        name, acc = self.cfg.account_of(provider, account)
        return self._build(key, self.cfg.providers[provider].runtime, name, acc)

    def _account_of_key(self, key: str) -> str | None:
        if key.startswith("@"):
            return key[1:]
        prov, _, acc = key.partition("@")
        return acc or (self.cfg.account_of(prov)[0] if prov in self.cfg.providers else None)

    def for_account(self, account: str):
        """The runtime logged in as `account` (login, status), shared with a provider/role using it when there is one."""
        if account in self._fixed:
            return self._fixed[account]
        for p in self.cfg.providers:
            if self.cfg.providers[p].kind == "llm" and self.cfg.account_of(p)[0] == account:
                return self.runtime(p)
        for k in self._rt:
            if self._account_of_key(k) == account and not k.startswith("@"):
                prov = k.partition("@")[0]
                if prov in self.cfg.providers:
                    return self.runtime(prov, account)
        acc = self.cfg.accounts[account]
        return self._build(f"@{account}", acc.runtime, account, acc)

    async def _wrong_account(self, provider: str, rt, account: str | None = None) -> str | None:
        """A pinned account that is now logged in as someone else must never run (e.g. re-logged in the browser's session)."""
        name, acc = self.cfg.account_of(provider, account)
        key = self._key(provider, account)
        if not acc.identity or provider in self._fixed or self._verified.get(key) == acc.identity or not hasattr(rt, "info"):
            return None
        info = await rt.info()
        live = (info.identity or info.account) if info and info.logged_in else ""
        if live != acc.identity:
            return (f"account {name!r} (provider {provider}) is logged in as {live or 'nobody'} but pinned to {acc.identity}: "
                    f"/account login {name}, or pick another account for the role in Settings (F3)")
        self._verified[key] = acc.identity
        return None

    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult:
        account = getattr(spec.role, "account", None)
        self._by_run[spec.run_id] = (spec.role.provider, account)
        try:
            rt = self.runtime(spec.role.provider, account)
            if err := await self._wrong_account(spec.role.provider, rt, account):
                return RunResult("failed", error=err)
            return await rt.run(spec, emit, approve)
        finally:
            self._by_run.pop(spec.run_id, None)

    async def cancel(self, run_id: str) -> None:
        if (p := self._by_run.get(run_id)) is not None:
            await self.runtime(*p).cancel(run_id)

    async def info(self, provider: str):
        rt = self.runtime(provider)
        return await rt.info() if hasattr(rt, "info") else None

    async def forget_account(self, account: str) -> None:
        """Drop every instance logged in as `account` (after a login/logout) so the next call starts fresh."""
        keys = [k for k in list(self._rt) if self._account_of_key(k) == account]
        for k in keys:
            self._home.pop(k, None)
            self._verified.pop(k, None)
            await self._rt.pop(k).close()

    async def close(self) -> None:
        for rt in [*self._rt.values(), *self._fixed.values()]:
            await rt.close()
