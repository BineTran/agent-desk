"""SETTINGS state, without Textual: rows, picker options, staged changes, review. The screen only renders and routes keys."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..config.check import resolve_model, structural
from ..config.schema import POINTS
from ..config.writer import DELETE, Preview, plan_many
from .views import source_of

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
TIER_NAMES = ["fast", "strong", "max"]
JEV = "typesafe"
RUN_KEYS: set[str] = set()                 # env vars whose key was typed in SETTINGS (this process only)
WHY = {"route": "which reader takes a task", "tier": "fast or strong model for a task",
       "dangerous": "a risky command: allow or ask you", "retry_or_stop": "a task failed: retry, replan or stop"}
FAST_HINTS = ["flash-medium", "flash", "haiku", "luna", "mini", "fast"]
STRONG_HINTS = ["pro-high", "pro", "sonnet", "sol", "strong"]


@dataclass
class Item:
    kind: str                     # role | point | provider | jev | add | account | add_account
    key: str


def group_key(value: tuple | None, fallback: str = "") -> str:
    """Picker group of a role choice (prov, kind, val, account): the provider, or provider@account for a role's own login."""
    if not value:
        return fallback
    acc = value[3] if len(value) > 3 else None
    return f"{value[0]}@{acc}" if acc else value[0]


@dataclass
class Option:
    label: str
    value: Any
    disabled: str = ""            # why it cannot be picked ("" = selectable)
    group: str = ""               # header shown above the first option of a group
    note: str = ""


@dataclass
class Staged:
    group: str                    # what the row is (roles.explorer, decision.route, providers.x, jev)
    title: str                    # one line for the review list
    changes: dict[str, Any]
    scope: str


def default_scope(group: str) -> str:
    return "repo" if group.startswith(("roles.", "decision.")) else "global"


def suggest_tiers(models: list[str]) -> dict[str, str]:
    def pick(hints: list[str], avoid: str | None = None) -> str | None:
        for h in hints:
            for m in models:
                if h in m and m != avoid:
                    return m
        return None
    fast = pick(FAST_HINTS)
    strong = pick(STRONG_HINTS, fast)
    return {k: v for k, v in (("fast", fast), ("strong", strong)) if v}


@dataclass
class SettingsModel:
    loaded: Any
    registry: Any
    status: Any
    pending: dict[str, Staged] = field(default_factory=dict)
    fetched: dict[str, list[str]] = field(default_factory=dict)     # provider -> models discovered in a wizard

    # ------------------------------------------------------------ reading
    @property
    def cfg(self):
        return self.loaded.config

    def items(self) -> list[Item]:
        return ([Item("role", r) for r in self.cfg.roles] + [Item("point", p) for p in POINTS]
                + [Item("provider", n) for n, p in self.cfg.providers.items() if p.kind == "llm"]
                + [Item("provider", n) for n in self._staged_providers() if n not in self.cfg.providers]
                + [Item("jev", "jev"), Item("add", "add")]
                + [Item("account", n) for n in self.account_names()] + [Item("add_account", "add_account")])

    def account_names(self) -> list[str]:
        from ..accounts import accounts
        return list(accounts(self.cfg, self.registry))

    def _staged_providers(self) -> list[str]:
        return [g.split(".", 1)[1] for g, s in self.pending.items() if g.startswith("providers.") and s.changes.get(g) is not DELETE]

    def models_of(self, provider: str) -> list[str]:
        info = self.status.infos.get(provider)
        found = list(self.fetched.get(provider) or (list(info.models) if info else []))
        return list(dict.fromkeys(list(self.tiers_of(provider).values()) + found))

    def tiers_of(self, provider: str) -> dict[str, str]:
        st = self.pending.get(f"providers.{provider}")
        if st and f"tiers.{provider}" in st.changes and st.changes[f"tiers.{provider}"] is not DELETE:
            return dict(st.changes[f"tiers.{provider}"])
        return dict(self.cfg.tiers.get(provider, {}))

    def runtime_of(self, provider: str) -> str:
        st = self.pending.get(f"providers.{provider}")
        if st and isinstance(st.changes.get(f"providers.{provider}"), dict):
            return st.changes[f"providers.{provider}"]["runtime"]
        return self.cfg.providers[provider].runtime

    def can_write(self, provider: str) -> bool:
        rt = self.runtime_of(provider)
        return self.registry.has("runtime", rt) and "sandbox_write" in self.registry.get("runtime", rt).capabilities

    def llm_providers(self) -> list[str]:
        out = [n for n, p in self.cfg.providers.items() if p.kind == "llm" and self.pending.get(f"providers.{n}", Staged("", "", {}, "")).changes.get(f"providers.{n}") is not DELETE]
        return out + [n for n in self._staged_providers() if n not in out]

    def source(self, group: str) -> str:
        key = group.replace("decision.", "decision.points.")
        touched = group in self.pending or any(k == key or k.startswith(key + ".") for st in self.pending.values() for k in st.changes)
        return "edited" if touched else source_of(self.loaded.sources, key)

    def role_view(self, role: str) -> dict:
        """provider, model label, effort, access — staged value if any."""
        r = self.cfg.roles[role]
        st = self.pending.get(f"roles.{role}")
        if st:
            ch = st.changes
            prov = ch.get(f"roles.{role}.provider", r.provider)
            acc = ch.get(f"roles.{role}.account", r.account)
            acc = None if acc is DELETE else acc
            sel = ch.get(f"roles.{role}.model") or ch.get(f"roles.{role}.tier")
            if f"roles.{role}.tiers" in ch:
                model = "/".join(self.tiers_of(prov).get(t, t) for t in ch[f"roles.{role}.tiers"])
            elif f"roles.{role}.tier" in ch:
                model = self.tiers_of(prov).get(sel, sel)
            elif f"roles.{role}.model" in ch:
                model = sel
            else:
                model = self._model_label(role)
            eff = ch.get(f"roles.{role}.effort", r.effort)
            eff = None if eff is DELETE else eff
            return {"provider": prov, "model": model, "effort": eff, "access": r.access, "account": acc}
        return {"provider": r.provider, "model": self._model_label(role), "effort": r.effort, "access": r.access, "account": r.account}

    def _model_label(self, role: str) -> str:
        r = self.cfg.roles[role]
        if r.tiers:
            return "/".join(resolve_model(self.cfg, r, t) for t in r.tiers)
        return resolve_model(self.cfg, r)

    def point_view(self, k: str) -> dict:
        pt = self.cfg.decision.points.get(k)
        eng = "none" if not pt or pt.engine == "none" else pt.engine
        prov, model = (pt.provider, pt.model) if pt else (None, None)
        fb = pt.fallback if pt else "main"
        thr = pt.sharp_threshold if pt else 0.8
        shadow = (pt.shadow or {}).get("engine") if pt else None
        pre = f"decision.points.{k}."
        ch = {kk: v for st in self.pending.values() for kk, v in st.changes.items() if kk.startswith(pre)}
        if ch:
            eng = ch.get(pre + "engine", eng)
            prov = None if ch.get(pre + "provider", prov) is DELETE else ch.get(pre + "provider", prov)
            model = None if ch.get(pre + "model", model) is DELETE else ch.get(pre + "model", model)
            fb = ch.get(pre + "fallback", fb)
            sh = ch.get(pre + "shadow", (pt.shadow if pt else None))
            shadow = None if sh is DELETE or not sh else sh.get("engine")
        return {"engine": eng, "provider": prov, "model": model, "fallback": fb, "threshold": thr, "shadow": shadow}

    def jev_provider(self) -> str | None:
        st = self.pending.get("jev")
        if st:
            return None if st.changes.get(f"providers.{JEV}") is DELETE else JEV
        return next((n for n, p in self.cfg.providers.items() if p.runtime == "jev"), None)

    def jev_var(self) -> str:
        jp = next((n for n, p in self.cfg.providers.items() if p.runtime == "jev"), None)
        return (self.cfg.providers[jp].env_key if jp else None) or "TYPESAFE_API_KEY"

    @staticmethod
    def env_set(var: str) -> bool:
        return bool(var and os.environ.get(var))

    @staticmethod
    def key_state(var: str) -> tuple[str, str]:
        """(text, style): where the key for `var` comes from. Never the value."""
        if not (var and os.environ.get(var)):
            return ("not set ✗", "red")
        if var in RUN_KEYS:
            return ("set ✓ (this run only — lost when agent-desk exits)", "yellow")
        return ("set ✓ (from your shell)", "green")

    def apply_key(self, var: str, key: str, session=None) -> None:
        """Put the key in this process's environment only (never on disk) and make running decision engines re-read it."""
        from ..events.redact import register_secret
        os.environ[var] = key
        RUN_KEYS.add(var)
        register_secret(key)
        pipe = getattr(session, "pipeline", None) if session is not None else None
        if pipe is not None:
            pipe.ctx.env[var] = key
            pipe._engines.clear()

    def efforts_for(self, provider: str, model: str | None) -> list[str]:
        """Efforts the model accepts: the runtime's per-model list (codex) or the generic list."""
        info = self.status.infos.get(provider)
        lst = list(info.models.get(model) or []) if info and model and hasattr(info.models, "get") else []
        return lst or list(EFFORTS)

    def model_of_choice(self, role: str, choice: tuple) -> str | None:
        prov, kind, val = choice[:3]
        tiers = self.tiers_of(prov)
        if kind == "tier":
            return tiers.get(val)
        if kind == "tiers":
            return tiers.get(self.cfg.roles[role].default_tier)
        return val

    # ------------------------------------------------------------ picker options
    def role_options(self, role: str) -> list[Option]:
        """One group per provider, then one per other account of its runtime (same models, another login)."""
        r = self.cfg.roles[role]
        out: list[Option] = []
        for prov in self.llm_providers():
            reason = "" if r.access == "read" or self.can_write(prov) else f"no write sandbox — cannot run {role} (write)"
            tiers = self.tiers_of(prov)
            default = self.account_of(prov)
            others = [a for a in self.accounts_for(prov) if a != default] if default is not None else []
            for acc in [None] + others:
                gk = f"{prov}@{acc}" if acc else prov
                first = True
                if r.tiers and all(t in tiers for t in r.tiers):
                    out.append(Option(f"tiers {'+'.join(r.tiers)} (default {r.default_tier})", (prov, "tiers", None, acc), reason, gk))
                    first = False
                for t, m in tiers.items():
                    out.append(Option(m, (prov, "tier", t, acc), reason, gk if first else "", f"tier {t}"))
                    first = False
                for m in self.models_of(prov):
                    if m not in tiers.values():
                        out.append(Option(m, (prov, "model", m, acc), reason, gk if first else "", ""))
                        first = False
                if first:
                    out.append(Option("(no models known — refresh the provider)", None, "no models", gk))
        return out

    def group_state(self, gk: str) -> tuple[str, str]:
        """(text, style) for a picker group: the provider's login, or the login of the account a role would run as."""
        prov, _, acc = gk.partition("@")
        if not acc:
            return self.provider_state(prov)
        return self.account_state(acc)

    def account_state(self, name: str) -> tuple[str, str]:
        row = self.status.account_row(name) if hasattr(self.status, "account_row") else None
        if row is None:
            return ("checking…", "bright_black") if not getattr(self.status, "accounts_checked", False) else ("not checked", "bright_black")
        if row.state == "ok":
            return (f"● login {row.identity}" + ("" if row.pinned else " (not pinned)"), "green")
        if row.state == "MISMATCH":
            return (f"✗ logged in as {row.identity}, pinned to {row.pinned} — l to log in again", "red")
        if row.state == "DUPLICATE":
            return (f"✗ {row.identity}: {row.detail} — l to log in again", "red")
        if row.state == "not logged in":
            return ("✗ not logged in — l to log in", "red")
        return (f"✗ {row.detail or row.state}", "red")

    def provider_state(self, prov: str) -> tuple[str, str]:
        """(text, style) for a provider's login state."""
        ps = self.status.providers.get(prov)
        if ps is None:
            return ("new — not checked yet", "bright_black") if prov not in self.cfg.providers else ("checking…", "bright_black")
        acct = f"[{ps.account_name}] " if ps.account_name else ""
        if ps.mismatch:
            return (f"✗ {acct}wrong account: {ps.mismatch}", "red")
        if ps.logged_in:
            return (f"● {acct}login {ps.account}".strip(), "green")
        if ps.error:
            return (f"✗ {ps.error}", "red")
        return (f"✗ not logged in — {ps.login_hint}", "red") if ps.logged_in is False else ("checking…", "bright_black")

    def role_current(self, role: str) -> tuple | None:
        r = self.cfg.roles[role]
        st = self.pending.get(f"roles.{role}")
        ch = st.changes if st else {}
        prov = ch.get(f"roles.{role}.provider", r.provider)
        acc = ch.get(f"roles.{role}.account", r.account)
        acc = None if acc is DELETE or acc == self.account_of(prov) else acc
        if f"roles.{role}.tiers" in ch or (not st and r.tiers):
            return (prov, "tiers", None, acc)
        if f"roles.{role}.tier" in ch or (not st and r.tier):
            return (prov, "tier", ch.get(f"roles.{role}.tier", r.tier), acc)
        return (prov, "model", ch.get(f"roles.{role}.model", r.model), acc)

    def engine_options(self) -> list[Option]:
        out = [Option("none (rules only)", ("none", None, None))]
        for e in self.registry.names("decision"):
            if e in ("none", "replay"):
                continue
            want = "decision" if "provider:decision" in self.registry.get("decision", e).capabilities else "llm"
            provs = [n for n, p in self.cfg.providers.items() if p.kind == want]
            if e == "jev":
                jp = self.jev_provider()
                provs = [jp] if jp else []
                if not provs:
                    out.append(Option("jev — set up first (enter)", ("jev-setup", None, None), "", "", "calibrated"))
                    continue
            for n in provs:
                if want == "llm":                  # a small/fast model per provider: the engine answers one bounded question
                    tiers = self.tiers_of(n)
                    for model in dict.fromkeys(x for x in (tiers.get("fast"), tiers.get("strong")) if x):
                        out.append(Option(f"{e} · {n} · {model}", (e, n, model), "", "", "fast" if model == tiers.get("fast") else ""))
                else:
                    out.append(Option(f"{e} · {n}", (e, n, None), "", "", "calibrated" if e == "jev" else ""))
        return out

    # ------------------------------------------------------------ staging
    def _stage(self, group: str, title: str, changes: dict) -> None:
        old = self.pending.get(group)
        self.pending[group] = Staged(group, title, changes, old.scope if old else default_scope(group))

    def stage_role(self, role: str, choice: tuple, effort: str | None) -> None:
        r = self.cfg.roles[role]
        prov, kind, val = choice[:3]
        acc = choice[3] if len(choice) > 3 else None
        ch: dict = {}
        orig = self._orig_role(role)
        same = (prov, kind, val) == orig[:3] or (prov == orig[0] and kind != "tiers" and orig[1] != "tiers"
                                                 and self.model_of_choice(role, (prov, kind, val)) == self.model_of_choice(role, orig))
        if not same:
            ch[f"roles.{role}.provider"] = prov
            if kind == "tiers":
                ch[f"roles.{role}.tiers"] = list(r.tiers or [])
                ch[f"roles.{role}.default_tier"] = r.default_tier
            else:
                ch[f"roles.{role}.{kind}"] = val
        if acc != orig[3]:
            if acc:
                from ..accounts import accounts, keep_implicit
                ch[f"roles.{role}.account"] = acc
                ch.update(keep_implicit(self.cfg, accounts(self.cfg, self.registry), prov, acc, keep_current=False))
            else:
                ch[f"roles.{role}.account"] = None       # null, not a delete: it may come from another layer (see by_scope)
        elif prov != r.provider and (acc or r.account):
            ch[f"roles.{role}.account"] = acc            # moving provider: say which login explicitly (none: its default)
        if effort != r.effort:
            ch[f"roles.{role}.effort"] = effort if effort else DELETE
        if not ch:
            self.pending.pop(f"roles.{role}", None)
            return
        who = f"{prov}@{acc}" if acc else prov
        self._stage(f"roles.{role}", f"{role} → {who} · {val or 'tiers'}" + (f" · effort {effort}" if effort else ""), ch)

    def _orig_role(self, role: str) -> tuple:
        r = self.cfg.roles[role]
        acc = r.account if r.account and r.account != self.account_of(r.provider) else None
        return ((r.provider, "tiers", None, acc) if r.tiers else (r.provider, "tier", r.tier, acc) if r.tier
                else (r.provider, "model", r.model, acc))

    def stage_point(self, k: str, engine: tuple, fallback: str, shadow: tuple | None = None) -> None:
        pt = self.cfg.decision.points.get(k)
        e, prov, model = engine
        pre = f"decision.points.{k}."
        ch: dict = {}
        cur_e = ("none", None, None) if not pt or pt.engine == "none" else (pt.engine, pt.provider, pt.model)
        if (e, prov, model) != cur_e:
            ch[pre + "engine"] = e
            ch[pre + "provider"] = prov if prov else DELETE
            ch[pre + "model"] = model if model else DELETE
        if fallback != (pt.fallback if pt else "main"):
            ch[pre + "fallback"] = fallback
        cur_sh = (pt.shadow.get("engine"), pt.shadow.get("provider"), pt.shadow.get("model")) if pt and pt.shadow else None
        if shadow != cur_sh:
            ch[pre + "shadow"] = ({"engine": shadow[0], "provider": shadow[1], **({"model": shadow[2]} if len(shadow) > 2 and shadow[2] else {})}
                                  if shadow else DELETE)
        if not ch:
            self.pending.pop(f"decision.{k}", None)
            return
        label = "none" if e == "none" else f"{e} · {prov}" + (f" · {model}" if model else "")
        self._stage(f"decision.{k}", f"{k} → {label} → {fallback}" + (f" (shadow {shadow[0]})" if shadow else ""), ch)

    def account_of(self, provider: str) -> str | None:
        """The account staged or configured for a provider (None: runtime without profile dirs, or unknown)."""
        st = self.pending.get(f"providers.{provider}.account")
        if st:
            return st.changes[f"providers.{provider}.account"]
        if provider not in self.cfg.providers:
            return None
        plug = self.registry.get("runtime", self.cfg.providers[provider].runtime) if self.registry.has("runtime", self.cfg.providers[provider].runtime) else None
        return self.cfg.account_of(provider)[0] if plug and "profiles" in plug.capabilities else None

    def accounts_for(self, provider: str) -> list[str]:
        from ..accounts import accounts
        rt = self.runtime_of(provider)
        return [n for n, a in accounts(self.cfg, self.registry).items() if a.runtime == rt]

    def stage_account(self, provider: str, account: str) -> None:
        """Switch which login a provider runs as; every role on that provider follows."""
        cur = self.cfg.account_of(provider)[0]
        if account == cur:
            self.pending.pop(f"providers.{provider}.account", None)
            return
        from ..accounts import accounts, keep_implicit
        ch: dict = {f"providers.{provider}.account": account, **keep_implicit(self.cfg, accounts(self.cfg, self.registry), provider, account)}
        self._stage(f"providers.{provider}.account", f"{provider} runs as account {account}", ch)

    def stage_provider(self, name: str, runtime: str, tiers: dict[str, str], auth: str = "login", env_key: str | None = None) -> None:
        prov = {"runtime": runtime, "auth": auth}
        if name in self.cfg.providers and self.cfg.providers[name].account and auth == "login" and runtime == self.cfg.providers[name].runtime:
            prov["account"] = self.cfg.providers[name].account          # editing tiers keeps the chosen login
        if auth == "env":
            prov["env_key"] = env_key
        ch: dict = {f"providers.{name}": prov}
        if tiers:
            ch[f"tiers.{name}"] = dict(tiers)
        allow = list(self.cfg.policy.allow_api_key)
        if auth == "env" and self.cfg.policy.subscription_only and name not in allow:
            ch["policy.allow_api_key"] = allow + [name]
        verb = "edit" if name in self.cfg.providers else "add"
        self._stage(f"providers.{name}", f"{verb} provider {name} ({runtime})" + (" · " + ", ".join(f"{k} {v}" for k, v in tiers.items()) if tiers else ""), ch)

    def stage_remove_provider(self, name: str) -> None:
        if name not in self.cfg.providers:
            self.pending.pop(f"providers.{name}", None)
            return
        ch: dict = {f"providers.{name}": DELETE, f"tiers.{name}": DELETE}
        if name in self.cfg.policy.allow_api_key:
            ch["policy.allow_api_key"] = [x for x in self.cfg.policy.allow_api_key if x != name]
        self._stage(f"providers.{name}", f"remove provider {name}", ch)

    def stage_jev(self, var: str, points: list[str], mode: str) -> None:
        allow = list(self.cfg.policy.allow_api_key)
        ch: dict = {f"providers.{JEV}": {"kind": "decision", "runtime": "jev", "auth": "env", "env_key": var}}
        if JEV not in allow and self.cfg.policy.subscription_only:
            ch["policy.allow_api_key"] = allow + [JEV]
        for k in POINTS:
            pre = f"decision.points.{k}."
            pt = self.cfg.decision.points.get(k)
            if k in points and mode == "shadow":
                ch[pre + "shadow"] = {"engine": "jev", "provider": JEV}
            elif k in points:
                ch[pre + "engine"] = "jev"
                ch[pre + "provider"] = JEV
            elif pt and pt.provider == JEV:
                ch[pre + "engine"] = "none"
                ch[pre + "provider"] = DELETE
        self._stage("jev", f"enable jev ({var}) for {', '.join(points) or 'no point'}" + (" as shadow" if mode == "shadow" else ""), ch)
        for k in points:                                 # the jev group owns these points now
            self.pending.pop(f"decision.{k}", None)

    def stage_jev_off(self) -> None:
        jp = next((n for n, p in self.cfg.providers.items() if p.runtime == "jev"), None)
        if not jp:
            self.pending.pop("jev", None)
            return
        ch: dict = {f"providers.{jp}": DELETE, "policy.allow_api_key": [x for x in self.cfg.policy.allow_api_key if x != jp]}
        for k, pt in self.cfg.decision.points.items():
            if pt.provider == jp:
                ch[f"decision.points.{k}.engine"] = "none"
                ch[f"decision.points.{k}.provider"] = DELETE
            if pt.shadow and pt.shadow.get("provider") == jp:
                ch[f"decision.points.{k}.shadow"] = DELETE
        self._stage("jev", f"disable jev ({jp})", ch)

    def undo(self, item: Item) -> None:
        group = {"role": f"roles.{item.key}", "point": f"decision.{item.key}", "provider": f"providers.{item.key}", "jev": "jev"}.get(item.kind)
        if group:
            self.pending.pop(group, None)

    def group_of(self, item: Item) -> str | None:
        return {"role": f"roles.{item.key}", "point": f"decision.{item.key}", "provider": f"providers.{item.key}", "jev": "jev"}.get(item.kind)

    # ------------------------------------------------------------ review
    def by_scope(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for st in self.pending.values():
            ch = {k: (DELETE if v is None and k.endswith(".account") and self.loaded.sources.get(k) in (st.scope, None) else v)
                  for k, v in st.changes.items()}
            out.setdefault(st.scope, {}).update(ch)
        return out

    def review(self) -> tuple[list[Preview], list[str]]:
        """Previews per file (validated together) and problems that block the save. Raises ConfigError."""
        previews = plan_many(self.loaded, self.by_scope())
        probs = structural(previews[-1].new.config, self.registry) if previews else []
        return previews, probs
