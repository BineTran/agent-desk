"""SETTINGS (F3 / Ctrl+S / /settings): one keyboard-first list of roles, decision points and providers.

Enter opens a small picker under the row; edits are staged (● edited) until `s` opens the review, where each change gets
its target (session | repo | global) and the YAML diff; Enter writes everything at once. Spec: prototype/agent-desk-settings.html.
State lives in SettingsModel (settings_model.py); this file renders and routes keys.
"""
from __future__ import annotations

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..config.loader import ConfigError
from ..config.schema import POINTS
from ..config.writer import DELETE, commit_many
from .settings_model import EFFORTS, JEV, TIER_NAMES, WHY, Item, SettingsModel, group_key, suggest_tiers
from .views import SOURCE_STYLE

PCOLOR = {"codex": "bold cyan", "claude": "bold magenta"}


def pm(provider: str, model: str = "", account: str | None = None) -> Text:
    t = Text(provider, style=PCOLOR.get(provider, "bold #7aa2f7"))
    if account:
        t.append(f"@{account}", style="magenta")
    if model:
        t.append(f" · {model}")
    return t

SCOPES = ("session", "repo", "global")
DIM = "bright_black"


def _cell(t: str | Text, w: int, style: str = "") -> Text:
    t = Text(t, style=style) if isinstance(t, str) else t
    t.truncate(w - 1, overflow="ellipsis")
    t.pad_right(w - t.cell_len)
    return t


class SettingsScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical { width: 96%; height: 94%; border: round #4a5a7a; background: #0c1018; padding: 0 1; }
    SettingsScreen #s_head { height: 1; }
    SettingsScreen #s_scroll { height: 1fr; }
    SettingsScreen #s_keys { color: #6f7c90; border-top: dashed #2a3650; height: auto; max-height: 4; }
    """

    def __init__(self, app_ref):
        super().__init__()
        self.a = app_ref
        self.m = SettingsModel(app_ref.loaded, app_ref.registry, app_ref.status)
        self.sel = 0
        self.mode = "list"                  # list | picker | review | add | jev
        self.oc = 0                         # cursor inside a picker / wizard list
        self.opts: list = []
        self.eff: str | None = None
        self.filter = ""                    # role picker: typed text narrows the model list
        self.pstep = "model"                # role picker: model -> effort
        self.expanded: set[str] = set()     # providers whose full model list is shown
        self.more_sel: str | None = None    # cursor on a provider's "+ N more" row
        self.effs: list = []
        self.eff_only = False               # opened with `e`: esc closes instead of going back to models
        self.eff_warn = ""
        self.chips: list[tuple[int, int, object]] = []   # clickable chips on the effort line: (x0, x1, value)
        self.fb = "main"
        self.shadow: tuple | None = None
        self.flash = Text("")
        self.lines: dict[int, tuple] = {}   # rendered line -> ("item", i) | ("opt", i)
        self.discard_armed = False
        # review
        self.rsel = 0
        self.previews: list = []
        self.problems: list[str] = []
        self.rerror = ""
        # wizards
        self.step = 0
        self.w: dict = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="s_head")
            with VerticalScroll(id="s_scroll", can_focus=False):
                yield Static(id="s_body")
            yield Static(id="s_keys")

    def on_mount(self) -> None:
        self.refresh_view()

    def status_updated(self) -> None:
        self.m.status = self.a.status
        self.refresh_view()

    # ================================================================ rendering
    @property
    def items(self) -> list[Item]:
        return self.m.items()

    def refresh_view(self) -> None:
        n = len(self.m.pending)
        head = Text.assemble(("SETTINGS", "bold"), (f"  {self.a.repo.name}   ", DIM))
        head.append(f"● {n} change{'s' if n != 1 else ''} pending" if n else "no changes", style="yellow" if n else DIM)
        self.query_one("#s_head", Static).update(head)
        body = {"review": self._review_text, "add": self._add_text, "jev": self._jev_text, "acct": self._acct_text}.get(self.mode, self._list_text)()
        self.query_one("#s_body", Static).update(body)
        self.query_one("#s_keys", Static).update(self._keys_text())

    def _keys_text(self) -> Text:
        k = {"list": "↑↓ move · enter change · e effort · u undo row · s review and save · esc close",
             "picker": "↑↓ choose · enter pick · esc cancel",
             "review": "↑↓ change · 1 session · 2 repo · 3 global · enter save all · esc back",
             "add": "↑↓ runtime · type to rename · enter next · esc cancel" if self.step == 0 else "↑↓ tier · ←→ model · enter next · esc back",
             "jev": "enter next · esc cancel",
             "acct": "↑↓ CLI · type the name · enter add and log in · esc cancel"}[self.mode]
        if self.mode == "picker":
            it = self.items[self.sel]
            k = {"role": ("←→ effort · enter save · esc " + ("cancel" if self.eff_only else "back to models")) if self.pstep == "effort"
                 else "↑↓ model · enter on + more (or →) to see all models · type to search · enter next: effort · esc cancel",
                 "point": "↑↓ engine · ←→ fallback · h shadow · enter pick · esc cancel",
                 "provider": "a switch account · t edit tiers · r refresh models · x remove · esc close",
                 "account": "l log in · p pin · u use for its provider · x remove (twice) · esc close"}.get(it.kind, k)
        if self.mode == "jev" and self.step == 0:
            k = "↑↓ field · type or paste · enter " + ("apply key" if self.w.get("key_only") else "apply and next") + " · ctrl+x disable jev · esc cancel"
        if self.mode == "jev" and self.step == 1:
            k = "↑↓ point · space toggle · m decide/shadow · enter next · esc back"
        return Text.assemble(self.flash, "\n" if self.flash.plain else "", (k, DIM))

    def _list_text(self) -> Text:
        out = Text()
        self.lines = {}
        line = 0
        titles = {"role": "Roles — who does the work (and as which account)", "point": "Decision points — rules always run first → engine (if sure) → fallback",
                  "provider": "Providers and engines", "account": "Accounts — one login each, in its own profile dir (never ~/.claude, ~/.codex)",
                  "add_account": "Accounts — one login each, in its own profile dir (never ~/.claude, ~/.codex)"}
        sections, seen = {}, set()
        for i, it in enumerate(self.items):
            t = titles.get(it.kind)
            if t and t not in seen:
                sections[i] = t
                seen.add(t)
        for i, it in enumerate(self.items):
            if i in sections:
                out.append(("\n" if i else "") + sections[i] + "\n", style="#9fb3d1 underline")
                line += 2 if i else 1
            on = i == self.sel
            row = self._row(it, on)
            if on:
                row.stylize("on #1b2640")
            out.append_text(row)
            out.append("\n")
            self.lines[line] = ("item", i)
            line += 1
            if on and self.mode == "picker":
                pt = self._picker_text(it, line)
                out.append_text(pt)
                line += pt.plain.count("\n")
        return out

    def _src(self, group: str) -> Text:
        s = self.m.source(group)
        if s == "edited":
            return Text("● edited", style="yellow")
        return Text(f" {s} ", style=SOURCE_STYLE.get(s, DIM))

    def _row(self, it: Item, on: bool) -> Text:
        mark = Text("▸ " if on else "  ", style="yellow")
        if it.kind == "role":
            v = self.m.role_view(it.key)
            val = pm(v["provider"], v["model"], v.get("account"))
            return Text.assemble(mark, _cell(it.key, 14), _cell(v["access"], 7, DIM), _cell(val, 50), _cell(v["effort"] or "—", 9), self._src(f"roles.{it.key}"))
        if it.kind == "point":
            v = self.m.point_view(it.key)
            eng = Text("(no engine)", style=DIM) if v["engine"] == "none" else Text(f"{v['engine']} · {v['provider']}" + (f" · {v['model']}" if v["model"] else ""),
                                                                                    style="green" if v["engine"] == "jev" else "cyan")
            val = Text.assemble(("rules → ", DIM), eng, (" → ", DIM), (v["fallback"], "yellow"))
            if v["shadow"]:
                val.append(f"  (shadow {v['shadow']})", style=DIM)
            thr = "" if v["engine"] == "none" else f"≥{v['threshold']}"
            return Text.assemble(mark, _cell(it.key, 21), _cell(val, 50), _cell(thr, 9, DIM), self._src(f"decision.{it.key}"))
        if it.kind == "provider":
            st = self.m.pending.get(f"providers.{it.key}")
            if st and st.changes.get(f"providers.{it.key}") is DELETE:
                return Text.assemble(mark, _cell(it.key, 21, "strike"), _cell("will be removed", 59, "red"), self._src(f"providers.{it.key}"))
            txt, sty = self.m.provider_state(it.key)
            rt = self.m.runtime_of(it.key)
            staged = self.m.pending.get(f"providers.{it.key}.account")
            acc = Text(f"→ account {self.m.account_of(it.key)} (staged) ", style="yellow") if staged else Text("")
            val = Text.assemble(acc, (txt, sty), (f" · {rt}" + ("" if self.m.can_write(it.key) else " · read-only"), DIM))
            return Text.assemble(mark, _cell(pm(it.key), 21), _cell(val, 59), self._src(f"providers.{it.key}"))
        if it.kind == "jev":
            jp = self.m.jev_provider()
            if jp:
                var = self._jev_var_staged()
                txt, sty = self.m.key_state(var)
                val = Text.assemble(("on", "green"), f" · {var} ", (txt, sty), ("  · k set key" if sty != "green" else "", DIM))
            else:
                val = Text("decision engine · off — enter to set up", style=DIM)
            return Text.assemble(mark, _cell(Text("jev", style="green" if jp else DIM), 21), _cell(val, 59), self._src("jev") if "jev" in self.m.pending else Text(""))
        if it.kind == "account":
            from ..accounts import accounts
            acc = accounts(self.m.cfg, self.a.registry).get(it.key)
            row = self.m.status.account_row(it.key) if hasattr(self.m.status, "account_row") else None
            txt, sty = self.m.account_state(it.key)
            used = ", ".join(row.used_by) if row and row.used_by else ", ".join(self._users(it.key)) or "unused"
            val = Text.assemble((txt, sty), (f" · {acc.runtime if acc else '?'} · used by {used}", DIM))
            return Text.assemble(mark, _cell(Text(it.key, style="bold"), 21), _cell(val, 59), self._src(f"accounts.{it.key}"))
        if it.kind == "add_account":
            from ..accounts import profile_runtimes
            return Text.assemble(mark, _cell(Text("+ add account", style="cyan"), 21), Text(" · ".join(profile_runtimes(self.a.registry)), style=DIM))
        return Text.assemble(mark, _cell(Text("+ add provider", style="cyan"), 21), Text(" · ".join(r for r in self.a.registry.names("runtime") if r != "mock"), style=DIM))

    def _users(self, account: str) -> list[str]:
        from ..accounts import users
        return users(self.m.cfg, account)

    def _jev_var_staged(self) -> str:
        st = self.m.pending.get("jev")
        if st and isinstance(st.changes.get(f"providers.{JEV}"), dict):
            return st.changes[f"providers.{JEV}"]["env_key"]
        return self.m.jev_var()

    # ---------------------------------------------------------------- pickers
    def _open_picker(self) -> None:
        it = self.items[self.sel]
        if it.kind == "jev":
            return self._start_jev()
        if it.kind == "add":
            return self._start_add()
        if it.kind == "add_account":
            return self._start_add_account()
        self.mode = "picker"
        if it.kind == "role":
            self.opts = self.m.role_options(it.key)
            cur = self.m.role_current(it.key)
            want = self.m.model_of_choice(it.key, cur)
            self.oc = next((i for i, o in enumerate(self.opts) if o.value == cur), None)
            if self.oc is None:                     # e.g. model: gpt-6.1-sol listed as "tier strong": match by resolved model
                self.oc = next((i for i, o in enumerate(self.opts) if o.value and group_key(o.value) == group_key(cur) and o.value[1] != "tiers"
                                and self.m.model_of_choice(it.key, o.value) == want), 0)
            self.eff = self.m.role_view(it.key)["effort"]
            self.filter = ""
            self.pstep, self.eff_only, self.eff_warn = "model", False, ""
            self.expanded, self.more_sel = set(), None
        elif it.kind == "point":
            self.opts = self.m.engine_options()
            v = self.m.point_view(it.key)
            cur = ("none", None, None) if v["engine"] == "none" else (v["engine"], v["provider"], v["model"])
            self.oc = next((i for i, o in enumerate(self.opts) if o.value == cur), 0)
            self.fb = v["fallback"]
            self.shadow = None
            pt = self.m.cfg.decision.points.get(it.key)
            st = self.m.pending.get(f"decision.{it.key}")
            sh = st.changes.get(f"decision.points.{it.key}.shadow") if st else (pt.shadow if pt else None)
            self.shadow = (sh["engine"], sh["provider"], sh.get("model")) if isinstance(sh, dict) else None
        else:
            self.opts = []
        self.refresh_view()

    def _open_effort(self, only: bool = False) -> None:
        """Second step of the role picker: efforts the chosen model accepts."""
        it = self.items[self.sel]
        o = self.opts[self.oc]
        model = self.m.model_of_choice(it.key, o.value)
        self.effs = [None] + self.m.efforts_for(o.value[0], model)
        self.eff_warn = ""
        if self.eff not in self.effs:
            self.eff_warn = f"{self.eff} is not supported by {model} — reset to default"
            self.eff = None
        self.pstep, self.eff_only = "effort", only
        self.refresh_view()

    def _picker_text(self, it: Item, first_line: int) -> Text:
        t = Text()
        pad = "      "
        line = first_line
        if it.kind == "role":
            t.append(f"{pad}╭─ {it.key}: choose a model", style="#4a5a7a")
            t.append(f"   filter: {self.filter}▏\n" if self.filter else "   (type to search all models)\n", style="yellow" if self.filter else DIM); line += 1
            shown = self._visible_opts()
            hidden: dict[str, int] = {}
            for i, o in enumerate(self.opts):
                hidden[group_key(o.value)] = hidden.get(group_key(o.value), 0) + (i not in shown)
            group = None
            for i, o in enumerate(self.opts):
                prov = group_key(o.value, o.group)
                if prov != group:
                    if group is not None:
                        line = self._more_row(t, pad, group, hidden, line)
                    group = prov
                    if any(j in shown for j, x in enumerate(self.opts) if group_key(x.value, x.group) == prov):
                        txt, sty = self.m.group_state(prov)
                        name, _, acc = prov.partition("@")
                        t.append(f"{pad}│ ", style="#4a5a7a"); t.append_text(pm(name, "", acc or None))
                        if not acc and (dflt := self.m.account_of(name)):
                            t.append(f" · {dflt}", style="magenta")
                        t.append(f"  {txt}", style=sty)
                        if o.disabled:
                            t.append(f"  — {o.disabled}", style="red")
                        t.append("\n"); line += 1
                if i not in shown:
                    continue
                cur = i == self.oc
                row = Text(f"{pad}│   {'▸' if cur else ' '} ")
                row.append_text(_cell(o.label, 40, DIM if o.disabled else ""))
                if o.note:
                    row.append(o.note, style=DIM)
                if cur:
                    row.stylize("on #1b2640", len(pad) + 2)
                t.append_text(row); t.append("\n")
                self.lines[line] = ("opt", i); line += 1
            if group is not None:
                line = self._more_row(t, pad, group, hidden, line)
            if self.pstep == "effort":
                o = self.opts[self.oc]
                row = Text(f"{pad}│ ▸ effort for {self.m.model_of_choice(it.key, o.value)}  ", style="bold")
                self.chips = []
                for e in self.effs:
                    x0 = row.cell_len
                    row.append(f" {e or 'default'} ", style="reverse bold" if e == self.eff else "")
                    self.chips.append((x0, row.cell_len, e))
                    row.append(" ")
                row.stylize("on #1b2640", len(pad) + 2)
                t.append_text(row); t.append("\n")
                self.lines[line] = ("eff", 0); line += 1
                if self.eff_warn:
                    t.append(f"{pad}│   {self.eff_warn}\n", style="yellow"); line += 1
            else:
                cur_eff = self.eff or "default"
                t.append(f"{pad}│ effort {cur_eff} · pick a model, then choose the effort\n", style=DIM); line += 1
            t.append(f"{pad}╰─\n", style="#4a5a7a")
        elif it.kind == "point":
            t.append(f"{pad}╭─ {it.key}: {WHY[it.key]}\n", style="#4a5a7a"); line += 1
            for i, o in enumerate(self.opts):
                cur = i == self.oc
                row = Text(f"{pad}│   {'▸' if cur else ' '} {o.label}")
                if o.note:
                    row.append(f"  {o.note}", style=DIM)
                if cur:
                    row.stylize("on #1b2640", len(pad) + 2)
                t.append_text(row); t.append("\n")
                self.lines[line] = ("opt", i); line += 1
            t.append(f"{pad}│ when not sure  ", style="#4a5a7a")
            for f in ("main", "user"):
                t.append(f" {f} ", style="reverse" if f == self.fb else DIM)
            t.append("   shadow ", style="#4a5a7a")
            t.append(f" {' · '.join(x for x in self.shadow if x) if self.shadow else 'off'} ", style="reverse" if self.shadow else DIM)
            t.append(f"\n{pad}╰─\n", style="#4a5a7a")
        elif it.kind == "provider":
            tiers = self.m.tiers_of(it.key)
            models = self.m.models_of(it.key)
            t.append(f"{pad}╭─ {it.key} · {self.m.runtime_of(it.key)}\n", style="#4a5a7a")
            t.append(f"{pad}│ tiers  " + ("  ".join(f"{k} {v}" for k, v in tiers.items()) or "none") + "\n")
            t.append(f"{pad}│ {len(models)} models known · " + ("can run write roles" if self.m.can_write(it.key) else "read roles only (no write sandbox)") + "\n", style=DIM)
            t.append(f"{pad}│ [t] edit tiers   [r] refresh models   [x] remove\n{pad}╰─\n", style="#4a5a7a")
        elif it.kind == "account":
            from ..accounts import accounts
            acc = accounts(self.m.cfg, self.a.registry).get(it.key)
            row = self.m.status.account_row(it.key) if hasattr(self.m.status, "account_row") else None
            txt, sty = self.m.account_state(it.key)
            t.append(f"{pad}╭─ account {it.key} · {acc.runtime if acc else '?'}\n", style="#4a5a7a")
            t.append(f"{pad}│ "); t.append(txt + "\n", style=sty)
            if acc:
                t.append(f"{pad}│ profile {acc.path(it.key)} · pinned {acc.identity or '—'}\n", style=DIM)
            used = (row.used_by if row else None) or self._users(it.key)
            t.append(f"{pad}│ used by {', '.join(used) or 'nothing yet — enter on a role and pick the ' + it.key + ' group'}\n", style=DIM)
            provs = self._providers_for_account(it.key)
            u = f"   [u] run provider {provs[0]} as {it.key}" if provs else ""
            t.append(f"{pad}│ [l] log in (private window)   [p] pin who it is{u}   [x] remove\n{pad}╰─\n", style="#4a5a7a")
        return t

    def _providers_for_account(self, name: str) -> list[str]:
        """Providers of the account's runtime that do not run as it yet (u switches the first one)."""
        from ..accounts import accounts
        acc = accounts(self.m.cfg, self.a.registry).get(name)
        return [p for p in self.m.llm_providers() if acc and p in self.m.cfg.providers and self.m.runtime_of(p) == acc.runtime
                and self.m.account_of(p) != name]

    def _more_row(self, t: Text, pad: str, prov: str, hidden: dict, line: int) -> int:
        """'+ N more' / '− show fewer' under a provider: selectable, enter or click toggles."""
        if self.filter or (not hidden.get(prov) and prov not in self.expanded):
            return line
        cur = self.more_sel == prov
        label = "− show fewer" if prov in self.expanded else f"+ {hidden[prov]} more models"
        row = Text(f"{pad}│   {'▸' if cur else ' '} {label}", style="cyan" if cur else DIM)
        if cur:
            row.stylize("on #1b2640", len(pad) + 2)
        t.append_text(row); t.append("\n")
        self.lines[line] = ("more", prov)
        return line + 1

    def _visible_opts(self) -> set[int]:
        """Without a filter: tier models + the current pick + expanded providers; with one: every model containing it."""
        if self.filter:
            return {i for i, o in enumerate(self.opts) if o.value and self.filter in o.label.lower()}
        return {i for i, o in enumerate(self.opts) if o.value and (o.note or o.value[1] == "tiers" or group_key(o.value) in self.expanded)} | {
            self.oc} | {i for i, o in enumerate(self.opts) if not o.value}

    def _nav(self) -> list:
        """Cursor stops in display order: model options and each provider's more-row."""
        vis = self._visible_opts()
        out, group = [], None
        for i, o in enumerate(self.opts):
            prov = group_key(o.value, o.group)
            if prov != group and group is not None:
                out.append(("more", group))
            group = prov
            if i in vis:
                out.append(("opt", i))
        if group is not None:
            out.append(("more", group))
        hidden_any = {p for p in {group_key(o.value, o.group) for o in self.opts}
                      if any(i not in vis for i, o in enumerate(self.opts) if group_key(o.value, o.group) == p)}
        return [x for x in out if x[0] == "opt" or (not self.filter and (x[1] in hidden_any or x[1] in self.expanded))]

    def _toggle_more(self, prov: str) -> None:
        if prov in self.expanded:
            self.expanded.discard(prov)
        else:
            self.expanded.add(prov)
        self.refresh_view()

    def _move_opt(self, d: int) -> None:
        if self.items[self.sel].kind != "role":
            self.oc = (self.oc + d) % len(self.opts)
            return
        nav = self._nav()
        cur = ("more", self.more_sel) if self.more_sel else ("opt", self.oc)
        pos = nav.index(cur) if cur in nav else -1
        kind, v = nav[(pos + d) % len(nav)]
        if kind == "more":
            self.more_sel = v
        else:
            self.more_sel, self.oc = None, v

    def _pick(self) -> None:
        it = self.items[self.sel]
        if it.kind == "role":
            o = self.opts[self.oc] if self.opts else None
            if o is None or o.value is None:
                return self._say("no model to pick", "red")
            if o.disabled:
                return self._say(o.disabled, "red")
            if self.pstep == "model":
                return self._open_effort()
            self.m.stage_role(it.key, o.value, self.eff)
        elif it.kind == "point":
            o = self.opts[self.oc]
            if o.value[0] == "jev-setup":
                return self._start_jev(points=[it.key])
            self.m.stage_point(it.key, o.value, self.fb, self.shadow)
        self.mode = "list"
        self.refresh_view()

    # ---------------------------------------------------------------- review
    def _open_review(self) -> None:
        if not self.m.pending:
            return self._say("nothing to save yet — enter on a row to change it", DIM)
        self.mode = "review"
        self.rsel = 0
        self._recompute()

    def _recompute(self) -> None:
        self.rerror, self.previews, self.problems = "", [], []
        try:
            self.previews, self.problems = self.m.review()
        except ConfigError as e:
            self.rerror = str(e)
        self.refresh_view()

    def _review_text(self) -> Text:
        t = Text()
        t.append("Review and save", style="bold"); t.append("  · each change goes to one place · nothing is written until enter\n\n", style=DIM)
        for i, st in enumerate(self.m.pending.values()):
            cur = i == self.rsel
            row = Text(f"{'▸' if cur else ' '} ")
            row.append_text(_cell(st.title, 64, "yellow"))
            for s in SCOPES:
                row.append(f" {s} ", style=("reverse " if s == st.scope else "") + SOURCE_STYLE.get(s, ""))
                row.append(" ")
            if cur:
                row.stylize("on #1b2640")
            t.append_text(row); t.append("\n")
        t.append("\n")
        if self.rerror:
            t.append("cannot save:\n" + self.rerror + "\n", style="red")
            return t
        for p in self.previews:
            if p.path is None:
                t.append("session only (memory, not written):\n", style="cyan")
                t.append("".join(f"  {k} = {'(removed)' if v is DELETE else v}\n" for k, v in p.changes.items()))
                continue
            for line in (p.diff() or f"{p.path}: no change").splitlines():
                t.append(line + "\n", style="green" if line.startswith("+") else "red" if line.startswith("-") else DIM if line.startswith("@@") else "bold")
            t.append("\n")
        if self.problems:
            t.append("blocked:\n  " + "\n  ".join(self.problems) + "\n", style="red")
        elif self.previews:
            t.append(f"✓ valid · config hash {self.a.loaded.snapshot_hash()} → {self.previews[-1].new.snapshot_hash()} · enter to save\n", style="green")
        return t

    def _save(self) -> None:
        if self.rerror or self.problems or not self.previews:
            return self._say("fix the problems above first", "red")
        try:
            commit_many(self.a.loaded, self.previews, self.a.session)
        except Exception as e:
            return self._say(f"save failed: {e}", "red")
        where = ", ".join("this session" if p.path is None else str(p.path).replace(str(self.a.repo) + "/", "") for p in self.previews)
        for p in self.previews:
            self.a.config_saved(p)
        self.m.pending.clear()
        self.m.status = self.a.status
        self.mode = "list"
        self._say(f"saved to {where} · applied live", "green")

    # ---------------------------------------------------------------- add provider wizard
    def _start_add(self, name: str | None = None) -> None:
        runtimes = [r for r in self.a.registry.names("runtime") if r != "mock"]
        if name:
            rt = self.m.runtime_of(name)
            self.w = {"runtimes": runtimes, "rt": rt, "name": name, "edit": True, "tiers": self.m.tiers_of(name), "models": self.m.models_of(name), "state": ""}
            self.mode, self.step, self.oc = "add", 1, 0
            self.refresh_view()
            return self._fetch()
        self.w = {"runtimes": runtimes, "rt": runtimes[0], "name": self._auto_name(runtimes[0]), "edit": False, "tiers": {}, "models": [], "state": ""}
        self.mode, self.step, self.oc = "add", 0, 0
        self.refresh_view()

    def _auto_name(self, rt: str) -> str:
        base = {"agy-cli": "antigravity", "claude-cli": "claude"}.get(rt, rt)
        name, i = base, 2
        while name in self.m.cfg.providers or f"providers.{name}" in self.m.pending:
            name, i = f"{base}{i}", i + 1
        return name

    def _add_text(self) -> Text:
        w = self.w
        t = Text()
        t.append("Edit provider" if w["edit"] else "Add provider", style="bold"); t.append(f"  · step {self.step + 1}/3\n\n", style=DIM)
        if self.step == 0:
            t.append("runtime (installed plugins)\n", style=DIM)
            for i, r in enumerate(w["runtimes"]):
                cap = self.a.registry.get("runtime", r).capabilities
                row = Text(f"{'▸' if i == self.oc else ' '} {r}  ")
                row.append("write sandbox" if "sandbox_write" in cap else "read-only", style=DIM)
                if i == self.oc:
                    row.stylize("on #1b2640")
                t.append_text(row); t.append("\n")
            t.append("\nname  "); t.append(f" {w['name']}▏", style="reverse"); t.append("   auth: login (the CLI's own subscription)\n", style=DIM)
        elif self.step == 1:
            t.append(f"{w['name']} · {w['rt']}   "); t.append((w["state"] or "fetching models…") + "\n\n", style="green" if "logged in" in w["state"] else DIM if not w["state"] else "red")
            for i, tier in enumerate(TIER_NAMES):
                row = Text(f"{'▸' if i == self.oc else ' '} tier {tier:<7}")
                m = w["tiers"].get(tier)
                row.append(f" ◂ {m or '(none)'} ▸", style="yellow" if m else DIM)
                if i == self.oc:
                    row.stylize("on #1b2640")
                t.append_text(row); t.append("\n")
            t.append(f"\n{len(w['models'])} models · ←→ change the model of the selected tier\n", style=DIM)
        else:
            t.append("will stage:\n")
            t.append(f"+ providers.{w['name']}: {{ runtime: {w['rt']}, auth: login }}\n", style="green")
            for k, v in w["tiers"].items():
                t.append(f"+ tiers.{w['name']}.{k}: {v}\n", style="green")
            t.append("\nthen pick it for a role in Roles (enter on the role)\n", style=DIM)
        return t

    @work(exclusive=True, group="fetch")
    async def _fetch(self) -> None:
        w = self.w
        try:
            plug = self.a.registry.get("runtime", w["rt"])
            own = False
            if w["name"] in self.m.cfg.providers and self.m.cfg.providers[w["name"]].runtime == w["rt"]:
                rt = self.a.router.runtime(w["name"])            # the provider's own login (shared instance: not closed here)
            elif "profiles" in plug.capabilities:                # a new provider: its implicit account's dir, never ~/.claude
                from ..config.schema import Account
                rt, own = plug.factory(home=Account(runtime=w["rt"]).path(w["name"])), True
            else:
                rt, own = plug.factory(), True
            info = await rt.info() if hasattr(rt, "info") else None
            if own and hasattr(rt, "close"):
                await rt.close()
        except Exception as e:
            w["state"] = f"failed: {e}"[:120]
            return self.refresh_view()
        if info is None:
            w["state"] = "this runtime does not list models"
        else:
            w["models"] = list(dict.fromkeys(list(w["tiers"].values()) + list(info.models)))
            self.m.fetched[w["name"]] = list(info.models)
            w["state"] = f"● logged in · {info.account} · {info.version}" if info.logged_in else f"✗ not logged in — {info.login_hint}"
            if not w["tiers"]:
                w["tiers"] = suggest_tiers(w["models"])
        self.refresh_view()

    def _add_key(self, key: str, ch: str | None) -> bool:
        w = self.w
        if self.step == 0:
            if key in ("up", "down"):
                self.oc = (self.oc + (1 if key == "down" else -1)) % len(w["runtimes"])
                w["rt"] = w["runtimes"][self.oc]
                w["name"] = self._auto_name(w["rt"])
            elif key == "backspace":
                w["name"] = w["name"][:-1]
            elif key == "enter":
                if not w["name"] or w["name"] in self.m.cfg.providers:
                    self._say("choose a new provider name", "red"); return True
                self.step, self.oc = 1, 0
                w["state"] = ""
                self._fetch()
            elif ch and (ch.isalnum() or ch in "-_"):
                w["name"] += ch.lower()
            else:
                return False
        elif self.step == 1:
            if key in ("up", "down"):
                self.oc = (self.oc + (1 if key == "down" else -1)) % len(TIER_NAMES)
            elif key in ("left", "right") and w["models"]:
                tier = TIER_NAMES[self.oc]
                choices = [None] + w["models"]
                i = choices.index(w["tiers"].get(tier)) if w["tiers"].get(tier) in choices else 0
                nxt = choices[(i + (1 if key == "right" else -1)) % len(choices)]
                if nxt:
                    w["tiers"][tier] = nxt
                else:
                    w["tiers"].pop(tier, None)
            elif key == "enter":
                if not w["tiers"]:
                    self._say("pick at least one tier model (roles select models through tiers or by name)", "red"); return True
                self.step = 2
            else:
                return False
        else:
            if key != "enter":
                return False
            self.m.stage_provider(w["name"], w["rt"], {k: w["tiers"][k] for k in TIER_NAMES if k in w["tiers"]})
            self.mode = "list"
            self.sel = next((i for i, it in enumerate(self.items) if it.kind == "provider" and it.key == w["name"]), self.sel)
            self._say(f"{w['name']} staged — press s to review and save", "yellow")
        self.refresh_view()
        return True

    # ---------------------------------------------------------------- accounts
    def _start_add_account(self) -> None:
        from ..accounts import profile_runtimes
        rts = profile_runtimes(self.a.registry)
        if not rts:
            return self._say("no installed runtime can keep separate accounts", "red")
        self.w = {"runtimes": rts, "rt": rts[0], "name": self._auto_account(rts[0]), "typed": False}
        self.mode, self.oc = "acct", 0
        self.refresh_view()

    def _auto_account(self, rt: str) -> str:
        base = {"claude-cli": "claude"}.get(rt, rt)
        names = set(self.m.account_names())
        name, i = f"{base}-2", 3
        while name in names:
            name, i = f"{base}-{i}", i + 1
        return name

    def _acct_text(self) -> Text:
        w = self.w
        t = Text()
        t.append("Add account", style="bold"); t.append("  · a second (third…) login of a CLI, kept in its own profile dir\n\n", style=DIM)
        t.append("CLI\n", style=DIM)
        for i, r in enumerate(w["runtimes"]):
            row = Text(f"{'▸' if i == self.oc else ' '} {r}  ")
            row.append(", ".join(a for a in self.m.account_names() if self._runtime_of_account(a) == r) or "no account yet", style=DIM)
            if i == self.oc:
                row.stylize("on #1b2640")
            t.append_text(row); t.append("\n")
        t.append("\nname  "); t.append(f" {w['name']}▏", style="reverse")
        t.append(f"   profile ~/.agent-desk/accounts/{w['name']}\n\n", style=DIM)
        t.append("enter: adds it to your global config, then the CLI's own login opens in a private browser window;\n"
                 "sign in there with the account you want. Then pick it for a role: enter on the role → the "
                 f"{w['name']} group.\n", style=DIM)
        return t

    def _runtime_of_account(self, name: str) -> str | None:
        from ..accounts import accounts
        a = accounts(self.m.cfg, self.a.registry).get(name)
        return a.runtime if a else None

    def _acct_key(self, key: str, ch: str | None) -> bool:
        from ..accounts import ACCOUNT_NAME
        w = self.w
        if key in ("up", "down"):
            self.oc = (self.oc + (1 if key == "down" else -1)) % len(w["runtimes"])
            w["rt"] = w["runtimes"][self.oc]
            if not w["typed"]:
                w["name"] = self._auto_account(w["rt"])
        elif key == "backspace":
            w["name"], w["typed"] = w["name"][:-1], True
        elif key == "enter":
            name = w["name"]
            if not ACCOUNT_NAME.fullmatch(name) or name in self.m.account_names():
                self._say("choose a new name: lowercase letters, digits, - or _", "red"); return True
            self.mode = "list"
            self._say(f"adding {name} — the login runs in this terminal…", "yellow")
            self._account_job(self.a.add_account(name, w["rt"]), name)
        elif ch and (ch.isalnum() or ch in "-_"):
            w["name"], w["typed"] = (w["name"] if w["typed"] else "") + ch.lower(), True
        else:
            return False
        self.refresh_view()
        return True

    def _account_key(self, name: str, ch: str | None) -> bool:
        if ch == "l":
            self._say(f"logging {name} in — the login runs in this terminal…", "yellow")
            self._account_job(self.a._account_login([name]), name)
        elif ch == "p":
            self._say(f"checking who {name} is logged in as…", DIM)
            self._account_job(self.a._account_pin([name]), name)
        elif ch == "u":
            provs = self._providers_for_account(name)
            if not provs:
                self._say(f"no other provider of this CLI to switch — pick {name} for a role instead (enter on a role)", "yellow"); return True
            self.m.stage_account(provs[0], name)
            self.mode = "list"
            self._say(f"{provs[0]} runs as {name} (staged; every role on {provs[0]} without its own account follows) — s to save", "yellow")
        elif ch == "x":
            if not self.w.get("rm_armed") == name:
                self.w = {"rm_armed": name}
                self._say(f"x again to remove account {name} (its CLI is logged out; the profile dir is kept)", "yellow"); return True
            self.w = {}
            self.mode = "list"
            self._account_job(self.a._account_remove([name]), None)
        else:
            return False
        self.refresh_view()
        return True

    def _account_job(self, coro, select: str | None) -> None:
        """Run an /account action (login suspends the app: the CLI owns the terminal), then refresh what SETTINGS shows."""
        async def go():
            try:
                await coro
            except Exception as e:
                self._say(f"{type(e).__name__}: {e}"[:200], "red")
                return self.refresh_view()
            await self.a._load_status()
            if select:
                self.sel = next((i for i, it in enumerate(self.items) if it.kind == "account" and it.key == select), self.sel)
            row = self.m.status.account_row(select) if select and hasattr(self.m.status, "account_row") else None
            self._say(f"{select}: {row.state}" + (f" · {row.identity}" if row and row.identity else "") if row else "done",
                      "green" if row is None or row.state == "ok" else "yellow")
            self.refresh_view()
        self.a.run_worker(go(), exclusive=False)

    # ---------------------------------------------------------------- jev wizard
    def _start_jev(self, points: list[str] | None = None, field: str = "var") -> None:
        on = self.m.jev_provider()
        cur = [k for k in POINTS if self.m.point_view(k)["engine"] == "jev" or self.m.point_view(k)["shadow"] == "jev"]
        self.w = {"var": self._jev_var_staged(), "points": set(points or cur or ["retry_or_stop"]),
                  "mode": "shadow" if any(self.m.point_view(k)["shadow"] == "jev" for k in cur) else "decide", "on": bool(on),
                  "field": field, "key": "", "key_only": field == "key"}
        self.mode, self.step, self.oc = "jev", 0, 0
        self.refresh_view()

    def _jev_text(self) -> Text:
        w = self.w
        t = Text()
        t.append("Set up Jev", style="bold"); t.append(f"  · step {self.step + 1}/3 · calibrated decision engine: answers the decision points, never writes code\n\n", style=DIM)
        if self.step == 0:
            t.append("Jev reads its API key from an environment variable. A key typed here goes into this run's environment only:\n"
                     "it is never written to a file, a log or the screen.\n\n", style=DIM)
            fv, fk = w["field"] == "var", w["field"] == "key"
            t.append("▸ " if fv else "  "); t.append("variable  "); t.append(f" {w['var']}{'▏' if fv else ''} ", style="reverse" if fv else "")
            txt, sty = self.m.key_state(w["var"]); t.append(f"   {txt}\n", style=sty)
            t.append("▸ " if fk else "  "); t.append("key       ")
            masked = ("•" * min(len(w["key"]), 24) + (f"  ({len(w['key'])} chars)" if w["key"] else "")) or ("type or paste the key" if fk else "—")
            t.append(f" {masked}{'▏' if fk else ''} ", style=("reverse" if fk else "") + ("" if w["key"] else " dim"))
            t.append("   enter applies it now" if w["key"] else "", style=DIM); t.append("\n\n")
            t.append(f"to keep it after agent-desk exits, add to ~/.zshrc:  export {w['var'] or 'TYPESAFE_API_KEY'}=<your key>\n"
                     "until a key is set, the Jev points fall back to main/user — safe to enable now.\n", style=DIM)
            if w["on"]:
                t.append("\nJev is on · ctrl+x to disable it\n", style="yellow")
        elif self.step == 1:
            t.append("Use Jev for\n", style=DIM)
            for i, k in enumerate(POINTS):
                row = Text(f"{'▸' if i == self.oc else ' '} {'☑' if k in w['points'] else '☐'} {k:<15}")
                row.append(WHY[k], style=DIM)
                if i == self.oc:
                    row.stylize("on #1b2640")
                t.append_text(row); t.append("\n")
            t.append("\nmode  ")
            for m in ("decide", "shadow"):
                t.append(f" {m} ", style="reverse" if w["mode"] == m else DIM)
            t.append("   " + ("records Jev's opinion next to the current engine; does not decide" if w["mode"] == "shadow" else "Jev decides when sure (≥ threshold), else the fallback") + "\n", style=DIM)
        else:
            t.append("will stage:\n")
            t.append(f"+ providers.{JEV}: {{ kind: decision, runtime: jev, auth: env, env_key: {w['var']} }}\n", style="green")
            if JEV not in self.m.cfg.policy.allow_api_key:
                t.append(f"+ policy.allow_api_key: [{JEV}]   (API-key exception to subscription_only)\n", style="green")
            for k in POINTS:
                if k in w["points"]:
                    t.append(f"+ decision.{k}.{'shadow' if w['mode'] == 'shadow' else 'engine'}: jev\n", style="green")
        return t

    def _jev_key(self, key: str, ch: str | None) -> bool:
        w = self.w
        if self.step == 0:
            f = w["field"]
            if key in ("up", "down", "tab", "shift+tab"):
                w["field"] = "key" if f == "var" else "var"
            elif key == "backspace":
                w[f] = w[f][:-1]
            elif key == "enter":
                import re
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", w["var"] or ""):
                    self._say("a variable NAME like TYPESAFE_API_KEY (A-Z, 0-9, _)", "red"); return True
                if w["key"]:
                    self.m.apply_key(w["var"], w["key"].strip(), self.a.session)
                    w["key"] = ""
                    self.flash = Text(f"key set for this run in {w['var']} — Jev can use it now", style="green")
                    if w["key_only"]:
                        self.mode = "list"
                        self.refresh_view()
                        return True
                self.step, self.oc = 1, 0
            elif key == "ctrl+x" and w["on"]:
                self.m.stage_jev_off()
                self.mode = "list"
                self._say("jev will be disabled — press s to review and save", "yellow")
                return True
            elif ch and f == "key" and ch.isprintable() and not ch.isspace():
                w["key"] += ch
            elif ch and f == "var" and (ch.isalnum() or ch == "_"):
                w["var"] += ch.upper()
            else:
                return False
        elif self.step == 1:
            if key in ("up", "down"):
                self.oc = (self.oc + (1 if key == "down" else -1)) % len(POINTS)
            elif key == "space":
                k = POINTS[self.oc]
                w["points"] ^= {k}
            elif ch == "m":
                w["mode"] = "shadow" if w["mode"] == "decide" else "decide"
            elif key == "enter":
                self.step = 2
            else:
                return False
        else:
            if key != "enter":
                return False
            self.m.stage_jev(w["var"], [k for k in POINTS if k in w["points"]], w["mode"])
            self.mode = "list"
            self._say("jev staged — press s to review and save", "yellow")
        self.refresh_view()
        return True

    # ================================================================ input
    def _say(self, msg: str, style: str = "") -> None:
        self.flash = Text(msg, style=style)
        self.refresh_view()

    def on_paste(self, event: events.Paste) -> None:
        if self.mode == "jev" and self.step == 0:
            f = self.w["field"]
            txt = "".join(event.text.split())
            self.w[f] += txt if f == "key" else txt.upper()
            event.stop()
            self.refresh_view()

    def on_key(self, event: events.Key) -> None:
        key, ch = event.key, event.character if event.is_printable else None
        handled = self._handle(key, ch)
        if handled:
            event.stop()
            event.prevent_default()

    def _handle(self, key: str, ch: str | None) -> bool:
        if key != "escape":
            self.discard_armed = False
        if key not in ("escape",) and self.flash.plain and self.mode != "list":
            self.flash = Text("")
        if key == "escape":
            return self._escape()
        if self.mode == "add":
            return self._add_key(key, ch)
        if self.mode == "acct":
            return self._acct_key(key, ch)
        if self.mode == "jev":
            return self._jev_key(key, ch)
        if self.mode == "review":
            n = len(self.m.pending)
            if key in ("up", "down"):
                self.rsel = (self.rsel + (1 if key == "down" else -1)) % n
            elif ch in ("1", "2", "3"):
                list(self.m.pending.values())[self.rsel].scope = SCOPES[int(ch) - 1]
                self._recompute()
                return True
            elif key == "enter":
                self._save()
                return True
            else:
                return False
            self.refresh_view()
            return True
        it = self.items[self.sel]
        if self.mode == "picker":
            if it.kind == "provider":
                if ch == "a":
                    accs = self.m.accounts_for(it.key)
                    cur = self.m.account_of(it.key)
                    if cur is None or len(accs) < 2:
                        self._say(f"no other account for {self.m.runtime_of(it.key)} — add one below: + add account", "yellow"); return True
                    self.m.stage_account(it.key, accs[(accs.index(cur) + 1) % len(accs)] if cur in accs else accs[0])
                    self._recompute()
                elif ch == "x":
                    self.m.stage_remove_provider(it.key); self.mode = "list"
                elif ch == "t":
                    self._start_add(it.key); return True
                elif ch == "r":
                    self.w = {"rt": self.m.runtime_of(it.key), "name": it.key, "tiers": self.m.tiers_of(it.key), "models": [], "state": "", "edit": True, "runtimes": []}
                    self._fetch(); self._say("refreshing models…", DIM); return True
                else:
                    return False
            elif it.kind == "account":
                return self._account_key(it.key, ch)
            elif it.kind == "role" and self.pstep == "effort":
                if key in ("left", "right", "up", "down"):
                    i = self.effs.index(self.eff) if self.eff in self.effs else 0
                    self.eff = self.effs[(i + (1 if key in ("right", "down") else -1)) % len(self.effs)]
                    self.eff_warn = ""
                elif key == "enter":
                    self._pick(); return True
                else:
                    return False
            elif key in ("up", "down") and self.opts:
                self._move_opt(1 if key == "down" else -1)
            elif it.kind == "role" and key == "enter" and self.more_sel:
                self._toggle_more(self.more_sel); return True
            elif it.kind == "role" and key in ("right", "left"):
                prov = self.more_sel or (group_key(self.opts[self.oc].value) if self.opts[self.oc].value else None)
                if prov:
                    (self.expanded.add if key == "right" else self.expanded.discard)(prov)
            elif it.kind == "role" and (key == "backspace" or (ch and (ch.isalnum() or ch in "-._"))):
                self.filter = self.filter[:-1] if key == "backspace" else self.filter + ch.lower()
                self.more_sel = None
                vis = sorted(self._visible_opts() - ({self.oc} if self.filter else set()))
                if vis and self.oc not in vis:
                    self.oc = vis[0]
            elif key in ("left", "right") and it.kind == "point":
                self.fb = "user" if self.fb == "main" else "main"
            elif ch == "h" and it.kind == "point":
                shadows = [None] + [o.value for o in self.opts if o.value[0] not in ("none", "jev-setup")]
                self.shadow = shadows[(shadows.index(self.shadow) + 1) % len(shadows)] if self.shadow in shadows else None
            elif key == "enter":
                self._pick(); return True
            else:
                return False
            self.refresh_view()
            return True
        # list
        n = len(self.items)
        if key in ("up", "down"):
            self.sel = (self.sel + (1 if key == "down" else -1)) % n
            self.flash = Text("")
        elif key == "enter":
            self.flash = Text("")
            self._open_picker(); return True
        elif ch == "e" and it.kind == "role":
            self.flash = Text("")
            self._open_picker()
            self._open_effort(only=True); return True
        elif ch == "k" and it.kind == "jev":
            self._start_jev(field="key"); return True
        elif ch == "u":
            self.m.undo(it)
        elif ch == "s":
            self._open_review(); return True
        else:
            return False
        self.refresh_view()
        self._scroll_to_sel()
        return True

    def _scroll_to_sel(self) -> None:
        line = next((ln for ln, v in self.lines.items() if v == ("item", self.sel)), 0)
        self.query_one("#s_scroll", VerticalScroll).scroll_to(y=max(0, line - 4), animate=False)

    def _escape(self) -> bool:
        if self.mode == "picker" and self.pstep == "effort" and not self.eff_only:
            self.pstep, self.eff_warn = "model", ""
            self.refresh_view()
            return True
        if self.mode in ("picker", "review", "add", "jev", "acct"):
            if self.mode in ("add", "jev") and self.step > 0 and not (self.mode == "add" and self.w.get("edit")):
                self.step -= 1
            else:
                self.mode = "list"
            self.flash = Text("")
            self.refresh_view()
            return True
        if self.m.pending and not self.discard_armed:
            self.discard_armed = True
            self._say(f"{len(self.m.pending)} unsaved change(s) — esc again to discard, s to review and save", "yellow")
            return True
        self.dismiss(True)
        return True

    def on_click(self, event: events.Click) -> None:
        if self.mode not in ("list", "picker") or event.widget is not self.query_one("#s_body", Static):
            return
        hit = self.lines.get(event.y)
        if not hit:
            return
        kind, i = hit
        if kind == "item":
            if i == self.sel and self.mode == "list":
                self._open_picker()
            else:
                self.sel, self.mode = i, "list"
                self.refresh_view()
        elif kind == "more":
            self.more_sel = i
            self._toggle_more(i)
        elif kind == "opt":
            self.oc, self.more_sel = i, None
            self.pstep = "model"
            self._pick()
        elif kind == "eff":
            for x0, x1, e in self.chips:
                if x0 <= event.x < x1:
                    self.eff, self.eff_warn = e, ""
                    self.refresh_view()
