"""Agent Desk TUI. A thin shell: all state comes from Projection (events), all actions go to Session/Studio."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable

from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.css.query import NoMatches
from rich.panel import Panel
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, Static

from .. import gitws
from ..config.check import check
from ..config.loader import Loaded
from ..context import builder
from ..context.inputs import parse_mentions
from ..context.render import render as render_memory
from ..graph import ReopenError, load_session, open_session, run_graph
from ..plugins import Registry
from ..reasoner import render_packet
from ..roles import resolve
from ..runtime.base import Approval
from ..studio import StudioError
from ..ui import HeadlessUI
from . import views
from .composer import COMMANDS, Composer
from .projection import Projection
from .screens import ChoiceScreen, TextScreen
from .status import StatusInfo, gather
from .widgets import ChatLog, KeyCard, QuestionCard, SelectList

KEYLOG = os.environ.get("AGENT_DESK_KEYLOG", "")

CSS = """
Screen { background: #0c1018; }
#header { height: 1; padding: 0 1; }
#statusbar { height: auto; max-height: 3; padding: 0 1; border-bottom: dashed #2a3650; }
#body { height: 1fr; }
#chat { width: 1fr; padding: 0 1; border-right: solid #2a3650; }
#chat .msg { margin-bottom: 1; }
#chat .bubble { border-left: thick #f0c25a; padding: 0 1; }
#planscroll { width: 1fr; padding: 0 1; }
#cards { height: auto; max-height: 50%; }
#cards > * { height: auto; }
VerticalScroll { scrollbar-size: 1 1; }
#sugg { height: auto; max-height: 9; padding: 0 1; background: #111827; display: none; }
#run { height: 1fr; }
#runmain { height: auto; }
#arch { width: 30; }
#center { width: 1fr; height: auto; }
#runlog { height: auto; }
Composer { height: auto; min-height: 3; max-height: 9; border: round #3b4a68; background: #0b1220; }
Composer:focus { border: round #f0c25a; }
#chips { height: auto; padding: 0 1; }
#hint { height: 1; padding: 0 1; color: #6f7c90; }
"""


class TuiUI:
    """Implements the harness UI protocol with futures resolved by user actions."""

    def __init__(self, app: "AgentDeskApp"):
        self.app = app

    async def answer_questions(self, qs):
        self.app.clarify = list(qs.questions)
        self.app.clarify_answers = {}
        self.app.clarify_future = asyncio.get_running_loop().create_future()
        self.app.busy = ""                                      # waiting for the user is never "busy"
        self.app.dirty = True
        try:
            return await self.app.clarify_future
        finally:
            self.app.clarify = []

    async def run_studio(self, studio) -> bool:
        self.app.studio_future = asyncio.get_running_loop().create_future()
        self.app.busy = ""                                      # e.g. reopened while planning: no new event would clear it
        self.app.dirty = True
        return await self.app.studio_future

    async def resolve_proposal(self, studio) -> bool:
        self.app.proposal_future = asyncio.get_running_loop().create_future()
        self.app.busy = ""
        self.app.dirty = True
        try:
            return await self.app.proposal_future
        finally:
            self.app.proposal_future = None

    async def _push_block(self, a: Approval) -> Text:
        """For a git push: which branch goes where, and which commits. Resolved from git, not from what the agent says."""
        try:
            pv = await gitws.push_preview(a.cwd or ".", a.command or "") if a.kind != "read" else None
        except Exception:
            pv = None
        if pv is None:
            return Text("")
        if "unknown" in pv:
            return Text(f"\npush       target unclear: {pv['unknown']}\n", style="bold yellow")
        t = Text.assemble(("\npush       ", "bright_black"), (pv["local"], "bold"), " → ", (f"{pv['remote']}/{pv['target']}", "bold magenta"),
                          (f"  {pv['url']}" if pv["url"] else "  (remote not configured)", "bright_black"),
                          (" · new branch" if pv["new_branch"] else "", "yellow"), f" · {len(pv['commits'])} commit(s)\n")
        for c in pv["commits"]:
            t.append(f"           {c}\n")
        return t

    async def approve_command(self, a: Approval, advice: str) -> bool:
        what = "file" if a.kind == "read" else "command"
        body = Text.assemble(("⚠ APPROVAL REQUIRED\n\n", "bold yellow"), ("run        ", "bright_black"), (a.run_id + "\n", ""),
                             (f"{what:<11}", "bright_black"), ((a.command or "?") + "\n", "bold"), ("cwd        ", "bright_black"), ((a.cwd or "?") + "\n", ""),
                             *((("why        ", "bright_black"), (a.reason + "\n", "")) if a.reason else ()),
                             *((("advice     ", "bright_black"), (advice + "\n", "yellow")) if advice else ()),
                             await self._push_block(a),
                             ("\n[a] approve once    [r] reject", "bold"))
        self.app.proj.approvals += 0
        r = await self.app.push_screen_wait(ChoiceScreen(body, {"a": "yes", "r": "no"}, "approval"))
        return r == "yes"


class AgentDeskApp(App):
    CSS = CSS
    BINDINGS = [Binding("ctrl+q", "quit", "quit"), Binding("f2", "components", "config"), Binding("f3", "settings", "settings"), Binding("ctrl+s", "settings", "settings", show=False, priority=True), Binding("escape", "nav", "navigate", show=False),
                Binding("tab", "toggle_focus", "focus", show=False, priority=True), Binding("ctrl+d", "diff", "diff", show=False),
                Binding("ctrl+n", "home", "new task", show=False, priority=True)]

    def __init__(self, repo: Path, loaded: Loaded, registry: Registry, router_factory: Callable, home: Path, brief: str = "",
                 reopen: str | None = None):
        super().__init__()
        self.repo, self.loaded, self.registry, self.router_factory, self.home, self.brief = repo.resolve(), loaded, registry, router_factory, home, brief
        self.reopen = reopen                       # session id to reopen on start (`agent-desk resume <id>`)
        self.recent: list[tuple] = []              # (id, status, title, created) of this repo's earlier sessions
        self.proj = Projection()
        self.session = None
        self.router = None
        self.status = StatusInfo()
        self.dirty = True
        self.busy = ""
        self.t0 = time.monotonic()
        self.facts: dict = {}
        self.notes: list[str] = []
        self.clarify: list = []
        self.clarify_answers: dict = {}
        self.clarify_future: asyncio.Future | None = None
        self.studio_future: asyncio.Future | None = None
        self.proposal_future: asyncio.Future | None = None
        self.last_main_answer = ""
        self.result_seen: tuple = (0, False)      # (chat length, Main answering) last scrolled into view on the RESULT screen
        self.outcome: str | None = None
        self.repo_problem = ""
        self.frame = 0
        self.answering: str | None = None          # question id the composer's next message answers (free text from a card)
        self.card_ids: set[str] = set()            # cards that already grabbed focus once

    async def on_event(self, event) -> None:
        if KEYLOG and isinstance(event, (events.Key, events.Paste)):
            self._keylog(event)
        await super().on_event(event)

    def _keylog(self, event) -> None:
        """AGENT_DESK_KEYLOG=<file>: raw keys for diagnosing input methods. Never while a secret field is active."""
        if getattr(self.screen, "mode", "") == "jev":
            return
        try:
            comp = self.q("#composer", Composer).text[-40:]
        except Exception:
            comp = "?"
        line = (f"{time.monotonic():.3f} paste {event.text!r}" if isinstance(event, events.Paste)
                else f"{time.monotonic():.3f} key={event.key!r} char={event.character!r} printable={event.is_printable}")
        with open(os.path.expanduser(KEYLOG), "a") as f:
            f.write(f"{line} | composer before={comp!r}\n")

    def q(self, selector, expect=None):
        """Always query the base screen: modals (F2, approvals) sit on top and must not break repainting."""
        return self.screen_stack[0].query_one(selector, expect) if expect else self.screen_stack[0].query_one(selector)

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Static(id="statusbar")
        with ContentSwitcher(initial="home", id="body"):
            yield VerticalScroll(Static(id="home_txt"), id="home")
            with Horizontal(id="plan"):
                yield ChatLog(id="chat")
                yield VerticalScroll(SelectList({"c", "enter", "p", "d"}, id="planlist"), Static(id="plan_extra"), id="planscroll")
            with VerticalScroll(id="run"):
                with Horizontal(id="runmain"):
                    yield Static(id="arch")
                    with Vertical(id="center"):
                        yield Static(id="run_top")
                        yield SelectList({"enter", "i", "d", "m", "c"}, forward=True, id="agents")
                        yield Static(id="run_bottom")
                yield Static(id="runlog")
            yield VerticalScroll(Static(id="result_txt"), id="result")
        with Vertical(id="cards"):
            yield QuestionCard(id="qcard")
            yield KeyCard({"a", "x", "e"}, id="pcard")
            yield KeyCard({"f", "w"}, id="quotacard")
        yield Static(id="sugg")
        yield Composer(self.repo, id="composer")
        yield Static(id="chips")
        yield Static(id="hint")

    def focus_composer(self) -> None:
        self.q("#composer", Composer).focus()

    async def on_mount(self) -> None:
        self.router = self.router_factory(self.loaded)
        self.q("#composer", Composer).focus()
        if self.brief:
            self.q("#composer", Composer).load_text(self.brief)
        self.set_interval(0.2, self._tick)
        self.run_worker(self._load_files(), exclusive=False)
        self.run_worker(self._load_status(), exclusive=False)
        self._load_recent()
        if self.reopen:
            self.run_worker(self._open(self.reopen), exclusive=False)
        self._render()

    def _load_recent(self) -> None:
        import sqlite3
        db = self.home / "db" / "agent-desk.sqlite"
        if not db.exists():
            return
        try:
            with sqlite3.connect(db) as c:
                self.recent = c.execute("select id, status, title, created_at from sessions where repo=? order by created_at desc limit 5",
                                        (str(self.repo),)).fetchall()
        except sqlite3.Error:
            self.recent = []

    async def _load_files(self) -> None:
        inside = await gitws.git(self.repo, "rev-parse", "--is-inside-work-tree", check=False)
        has_commit = await gitws.git(self.repo, "rev-parse", "--verify", "HEAD", check=False)
        if inside != "true":
            self.repo_problem = f"{self.repo} is not a git repository. Agent Desk works on a git worktree: run `git init` and make a first commit, or cd into a repo."
        elif not has_commit:
            self.repo_problem = "this repository has no commits yet: make a first commit so Agent Desk can branch from it."
        self.dirty = True
        try:
            out = await gitws.git(self.repo, "ls-files", check=False)
            files = out.splitlines()
            dirs = sorted({str(Path(f).parent) + "/" for f in files if "/" in f})
            self.q("#composer", Composer).repo_files = files + dirs
        except Exception:
            pass

    async def _load_status(self) -> None:
        self.status = await gather(self.router, self.loaded.config)
        self.dirty = True
        if hasattr(self.screen, "status_updated"):
            self.screen.status_updated()

    async def on_unmount(self) -> None:
        if self.session:
            if self.outcome is None:                     # leaving while it runs: say so, so it can be reopened and continued
                try:
                    await self.session.bus.store.set_status(self.session.sid, "INTERRUPTED")
                except Exception:
                    pass
            await self.session.close()
        elif self.router:
            await self.router.close()

    # ------------------------------------------------------------------ rendering
    def _tick(self) -> None:
        self.frame += 1
        if self.dirty or self.proj.running_agents() or self.busy:
            try:
                self._render()                                # spinners and elapsed clocks move while anything runs
            except NoMatches:
                pass                                          # app is shutting down: widgets are already gone

    def _elapsed(self) -> str:
        s = int(time.monotonic() - self.t0)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _plan_items(self) -> list[str]:
        pl = self.proj.plan
        if not pl:
            return []
        return ([f"AC-{i}" for i in range(1, len(pl["acceptance_criteria"]) + 1)] + [t["id"] for t in pl["tasks"]]
                + [f"R-{i}" for i in range(1, len(pl["risks"]) + 1)] + [f"OUT-{i}" for i in range(1, len(pl["out_of_scope"]) + 1)]
                + [q["id"] for q in self.proj.questions.values() if not q["answered"]])

    def selected_item(self) -> str | None:
        items = self._plan_items()
        return items[self.q("#planlist", SelectList).sel % len(items)] if items else None

    def sched_idle(self) -> bool:
        return not (self.session and self.session.active_runs)

    def mode(self) -> str:
        return "HOME" if self.session is None else self.proj.mode

    def _render(self) -> None:
        self.dirty = False
        cfg = self.loaded.config
        p = self.proj
        mode = self.mode()
        now = time.monotonic()
        roles = views.header_roles(p, self._role_pm("main"), self._role_pm("architect"), now) if self.session else None
        self.q("#header", Static).update(views.header(p, self.repo.name, self._elapsed(), roles) if self.session else
                                         Text.assemble(("AGENT DESK", "bold"), (f" · {self.repo}", "bright_black")))
        self.q("#statusbar", Static).update(views.status_bar(cfg, self.status, self.notes + [o["text"] for o in p.overrides[-2:]]
                                                             + ([views.baseline_note(p)] if views.baseline_note(p) and p.status in ("PLANNING", "RUNNING") else []), p.limits))
        sw = self.q("#body", ContentSwitcher)
        sw.current = {"HOME": "home", "PLAN": "plan", "RUN": "run", "RESULT": "result"}[mode]
        if mode == "HOME":
            self.q("#home_txt", Static).update(Group(
                Text("\nWhat do you want to do in this repo?", style="bold cyan"),
                Text("Write it like a message to a colleague. Mention files with @, paste links, write as much as you like.", style="bright_black"),
                Text("Enter sends · Ctrl+J newline · @ files · / commands · F2 configuration · F3 or Ctrl+S settings\n", style="bright_black"),
                Text.assemble(("Will run with: ", "bright_black"), views.runs_with(cfg)),
                *([Text("\n" + views.SPIN[self.frame % 4] + " " + self.busy, style="italic yellow")] if self.busy else []),
                *[Text("\n" + m["text"], style="bold red") for m in self.proj.chat[-3:] if m["who"] == "sys"],
                *([Text("\n" + self.repo_problem, style="bold red")] if self.repo_problem else []),
                *([Text("\nEarlier sessions in this repo  (/open <id> to continue one)", style="bold cyan")] if self.recent else []),
                *[Text.assemble(f"  {sid}  ", (f"{status:<14}", "yellow" if status != "COMPLETED" else "green"), (f"{(title or '')[:70]}", ""),
                                (f"  {created[:16].replace('T', ' ')}", "bright_black")) for sid, status, title, created in self.recent]))
        elif mode == "PLAN":
            self.q("#chat", ChatLog).sync(p.chat, views.bubble_view(p, now, self.frame, self.busy))
            pl = self.q("#planlist", SelectList)
            pl.count = len(self._plan_items())
            pl.update(views.plan_view(p, self.selected_item(), pl.has_focus, [c.command for c in cfg.verification.values()]))
            st = self.session.studio if self.session else None
            extra = []
            if st and not p.approved:
                extra += [views.checklist_view(st.checklist()), Text.assemble(("Will run with  ", "bright_black"), views.runs_with(cfg))]
            self.q("#plan_extra", Static).update(Group(*extra))
        elif mode == "RUN":
            main_pm = self._role_pm("main")
            effort = resolve(cfg, "main", overrides=self.session.overrides if self.session else None).effort
            cw = self.q("#center").size.width or 130
            self.q("#arch", Static).update(views.architect_box(p, now, self.frame, self._role_pm("architect")))
            self.q("#run_top", Static).update(views.run_tree_top(p, views.pv(*main_pm), effort, cfg, cw, now, self.frame))
            al = self.q("#agents", SelectList)
            al.count = len([c for c in views.run_cards(p) if not isinstance(c, dict)])
            al.update(views.agent_cards(p, al.sel, al.has_focus, now, self.frame, per_row=max(1, min(3, (cw + 1) // (views.CARD_W + 1)))))
            self.q("#run_bottom", Static).update(views.run_tree_bottom(p, cw))
            b = views.bubble_view(p, now, self.frame, self.busy)
            self.q("#runlog", Static).update(Group(*([b] if b is not None else []), views.log_box(p)))
            if "run-focus" not in self.card_ids and not isinstance(self.screen, ModalScreen):
                self.card_ids.add("run-focus")                 # entering RUN: the dashboard has the keys, Tab goes to chat
                if not self.q("#composer", Composer).text.strip():
                    al.focus()
        else:
            b = views.bubble_view(p, now, self.frame, self.busy)
            self.q("#result_txt", Static).update(Group(views.result_view(p, cfg, self.facts),
                                                       *([Text("\nCHAT WITH MAIN", style="bold cyan"), views.chat_view(p, 10)] if p.chat else []),
                                                       *([b] if b is not None else [])))
            if (len(p.chat), b is not None) != self.result_seen:     # a new message or Main starts answering: show the bottom
                self.result_seen = (len(p.chat), b is not None)
                self.q("#result", VerticalScroll).scroll_end(animate=False)
        self._cards()
        self._hint()
        self._chips()

    # ------------------------------------------------------------------ cards (questions, proposal, quota)
    def open_questions(self) -> list[dict]:
        if self.clarify and self.clarify_future and not self.clarify_future.done():
            return [{"id": q.id, "text": q.text, "options": q.options, "recommended": q.recommended, "evidence": q.evidence, "src": "clarify"}
                    for q in self.clarify if q.id not in self.clarify_answers]
        st = self.session.studio if self.session else None
        if not st:
            return []
        return [{"id": q.id, "text": q.text, "options": q.options, "recommended": getattr(q, "recommended", 0) or 0, "evidence": q.evidence, "src": "studio"}
                for q in st.questions.values() if not q.answered]

    def _cards(self) -> None:
        composer = self.q("#composer", Composer)
        qc = self.q("#qcard", QuestionCard)
        qs = self.open_questions() if self.mode() in ("PLAN", "RUN") else []
        qc.set_questions(qs)
        pc = self.q("#pcard", KeyCard)
        pend = self.proj.pending if self.mode() in ("PLAN", "RUN") else None
        pc.update(views.proposal_view(pend, pc.has_focus) if pend else "")
        qt = self.q("#quotacard", KeyCard)
        quota = self.proj.status == "WAITING_QUOTA"
        if quota:
            qt.update(Panel(Text.assemble((f"Quota reached ({(self.proj.quota_hit or {}).get('provider')}) · resets {(self.proj.quota_hit or {}).get('reset')}. Progress is kept.\n", "bold red"),
                                          ("[f] switch roles to their fallback and continue   [w] wait (then /resume)", "bold")),
                            border_style="red" if qt.has_focus else "bright_black", padding=(0, 1)))
        for w, on, key in ((qc, bool(qs), f"q:{qs[0]['id']}" if qs else ""), (pc, bool(pend), f"p:{self.proj.last_seq}" if pend else ""), (qt, quota, "quota")):
            w.display = on
            if not on and w.has_focus:
                composer.focus()
            elif on and key not in self.card_ids:
                self.card_ids.add(key)
                if not composer.text.strip() and not isinstance(self.screen, ModalScreen):
                    w.focus()                                   # a card that needs you takes the keys, no Esc first

    def _role_pm(self, role: str) -> tuple[str, str]:
        r = resolve(self.loaded.config, role, overrides=self.session.overrides if self.session else None)
        return r.provider, r.model

    def _hint(self) -> None:
        m = self.mode()
        f = self.focused
        fid = getattr(f, "id", None)
        h = {"qcard": "1–9 pick · ↑↓ + Enter confirm · type to answer in your own words · Esc chat",
             "pcard": "a accept · e edit (reject + comment) · x reject · Esc chat",
             "quotacard": "f switch to fallback and continue · w wait · Esc chat",
             "planlist": "↑↓ item · c/Enter comment on item · p pin Main's last answer · d diff · Tab/Esc chat",
             "agents": "↑↓ agent · Enter inspect its context · d diff · c/m session.md · F2 config · Tab chat · just type to message the session"}.get(fid)
        if h is None:
            h = {"HOME": "Enter send · Ctrl+J newline · @ file · / commands · F2 config · F3/Ctrl+S settings · /quit",
                 "PLAN": "Enter send · Tab plan items/cards · /approve · /review · /model · F2 config · /quit",
                 "RUN": "Enter send · Tab agents/cards · Ctrl+D diff · /context · /stop · Ctrl+N home · F2 config · type /model, /stop, or a message",
                 "RESULT": "Ctrl+D diff · /context session.md · F2 config · /resume · Ctrl+N new task · ask Main below"}[m]
        self.q("#hint", Static).update(h)
        c = self.q("#composer", Composer)
        c.border_title = {"RUN": "message the session", "RESULT": "ask Main"}.get(m, "message")

    def _chips(self) -> None:
        mentions, urls = parse_mentions(self.q("#composer", Composer).text)
        t = Text()
        for m in mentions:
            t.append(f" file {m} ", style="black on cyan"); t.append(" ")
        for u in urls:
            t.append(f" url {u[:48]} ", style="black on magenta"); t.append(" ")
        self.q("#chips", Static).update(t)

    def on_composer_suggestions(self, msg: Composer.Suggestions) -> None:
        t = Text()
        for i, (rep, label) in enumerate(msg.items):
            line = Text(f" {rep:<46} {label}\n", style="reverse" if i == msg.index else "")
            t.append_text(line)
        sg = self.q("#sugg", Static)
        sg.update(t if msg.items else "")
        sg.display = bool(msg.items)
        self._chips()

    def on_composer_left(self, _) -> None:
        self.action_toggle_focus()            # Esc leaves the box: to the first card that needs you, else the plan/agent list

    def on_text_area_changed(self, _) -> None:
        self._chips()

    # ------------------------------------------------------------------ actions
    ACCOUNT_USAGE = ("/account                         list accounts (state · who · used by)\n"
                     "/account add <name> [runtime] [email]   add one (runtime: claude-cli | codex; asked when left out) and log it in\n"
                     "/account login <name> [email] [--normal]   log in in a private browser window, then pin who it is\n"
                     "/account pin <name>                pin who an already logged-in account is\n"
                     "/account use <provider|role> <account> [session|repo|global]   who a provider (all its roles) or one role runs as\n"
                     "/account remove <name>             remove from config + log its CLI out\n"
                     "F3 SETTINGS › Accounts does the same with the keyboard; pick an account for a role there too")

    async def _account(self, args: list[str]) -> None:
        sub = args[0] if args else "ls"
        handler = {"ls": self._account_ls, "add": self._account_add, "login": self._account_login,
                   "use": self._account_use, "remove": self._account_remove, "pin": self._account_pin}.get(sub)
        if handler is None:
            return self._sys(self.ACCOUNT_USAGE)
        await handler(args[1:])

    async def _account_ls(self, args: list[str]) -> None:
        from ..accounts import status, table
        self._sys("checking accounts…")

        async def go():
            try:
                rows = await status(self.loaded.config, self.router)
            except Exception as e:
                return self._sys(f"/account failed: {e}")
            bad = [r for r in rows if (r.state in ("not logged in", "MISMATCH") and r.used_by) or r.state == "DUPLICATE"]
            unpinned = [r for r in rows if r.state == "ok" and not r.pinned]
            hint = (f"next: /account login {bad[0].name} (opens a private window)" if bad else
                    f"next: /account pin {unpinned[0].name}" if unpinned else "")
            self._sys(table(rows) + ("\n" + hint if hint else "") + "\n/account help for add · login · use · remove")
        self.run_worker(go(), exclusive=False)

    def _account_commit(self, p) -> None:
        from ..config.writer import commit
        commit(self.loaded, p, self.session)
        self.config_saved(p)

    async def _account_add(self, args: list[str]) -> None:
        from ..accounts import plan_add, profile_runtimes, resolve_runtime
        if not args:
            return self._sys("usage: /account add <name> [runtime] [email] [global|repo]   e.g. /account add team claude-cli")
        name, rest = args[0], args[1:]
        email = next((a for a in rest if "@" in a), None)
        scope = next((a for a in rest if a in ("global", "repo")), "global")
        like = next((a for a in rest if "@" not in a and a not in ("global", "repo")), None)
        if like is None and len(choices := profile_runtimes(self.registry)) > 1:
            keys = {str(i + 1): rt for i, rt in enumerate(choices)}
            keys_esc = {**keys, "escape": ""}
            body = Text.assemble((f"Which CLI does account {name} log in to?\n\n", "bold"),
                                 *(f"[{k}] {rt}\n" for k, rt in keys.items()), ("\n[esc] cancel", "bright_black"))
            like = await self.push_screen_wait(ChoiceScreen(body, keys_esc, "/account add"))
            if not like:
                return self._sys("account add cancelled")
        resolve_runtime(self.loaded.config, self.registry, like)
        await self.add_account(name, like, scope, email)

    async def add_account(self, name: str, runtime: str | None, scope: str = "global", email: str | None = None) -> None:
        """Add + log in + pin (also used by SETTINGS › Accounts)."""
        from ..accounts import plan_add
        p = plan_add(self.loaded, self.registry, name, runtime, scope)
        self._account_commit(p)
        self._sys(f"added account {name} ({p.path}); logging it in…")
        await self._account_login([name] + ([email] if email else []))
        self._sys(f"pick it for a role: F3 → enter on a role → the {name} group (or /account use <role> {name})")
        self.run_worker(self._load_status(), exclusive=False)

    async def _account_login(self, args: list[str], undo: bool = True) -> None:
        from ..accounts import after_login, browser_shim, login_command, login_note, run_login
        normal = "--normal" in args
        args = [a for a in args if a != "--normal"]
        if not args:
            return self._sys("usage: /account login <account> [email] [--normal]")
        name = args[0]
        browser, opens = (None, "your default browser") if normal else browser_shim(self.loaded.config.login.browser)
        argv, env, itself = login_command(self.loaded.config, self.router, name, args[1] if len(args) > 1 else None, browser)
        with self.suspend():                                # the official CLI owns the terminal + browser flow; we never see the token
            print(f"\n{login_note(name, opens)}\n", flush=True)
            run_login(argv, env, itself, browser)
        await self._account_pin([name], undo)

    async def _account_pin(self, args: list[str], undo: bool = False) -> None:
        from ..accounts import after_login
        if not args:
            return self._sys("usage: /account pin <account>")
        msg, p = await after_login(self.loaded, self.router, args[0], undo_duplicate=undo)
        if p is not None:
            self._account_commit(p)
        self._sys(msg)

    async def _account_use(self, args: list[str]) -> None:
        from ..accounts import plan_use
        if len(args) < 2:
            return self._sys("usage: /account use <provider|role> <account> [session|repo|global]   e.g. /account use architect team")
        p = plan_use(self.loaded, self.registry, args[0], args[1], args[2] if len(args) > 2 else "global")
        self._account_commit(p)
        role = args[0] in self.loaded.config.roles
        self._sys(f"{'role ' if role else ''}{args[0]} → account {args[1]} ({p.path or 'this session'})"
                  + ("" if role else f" · every role on {args[0]} without its own account follows"))

    async def _account_remove(self, args: list[str]) -> None:
        import subprocess
        from ..accounts import logout_command, plan_remove
        if not args:
            return self._sys("usage: /account remove <account>")
        name = args[0]
        out = logout_command(self.loaded.config, self.router, name)
        p = plan_remove(self.loaded, name)
        home = self.loaded.config.accounts[name].path(name)
        if out:
            subprocess.run(out[0], env=out[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await self.router.forget_account(name)
        self._account_commit(p)
        self._sys(f"removed account {name}; profile dir kept: {home}")

    async def _checks(self, refresh: bool = False) -> None:
        from ..baseline import BaselineRunner, doctor
        cfg = self.loaded.config
        if not cfg.verification:
            return self._sys("no verification checks configured (.agent-desk.yaml → verification)")
        try:
            head = await gitws.git(self.repo, "rev-parse", "HEAD")
            runner = BaselineRunner(self.repo, head, self.home / "doctor" / head[:12], self.home / "baseline", cfg.workspace)
            try:
                rows = await doctor(runner, cfg.verification, refresh=refresh)
            finally:
                await runner.close()
        except Exception as e:
            return self._sys(f"/checks failed: {e}")
        self.push_screen(TextScreen(views.doctor_view(rows, head[:7]), "CHECKS"))

    def action_settings(self) -> None:
        from .settings import SettingsScreen
        self.push_screen(SettingsScreen(self))

    def config_saved(self, p) -> None:
        """Called by SETTINGS after a save: refresh provider status, tell the session (event log), repaint."""
        self.run_worker(self._load_status(), exclusive=False)
        if self.session is not None:
            payload = {"scope": p.scope, "keys": p.keys, "hash": self.loaded.snapshot_hash(),
                       "text": f"config {p.scope}: " + ", ".join(p.keys)[:200]}
            self.run_worker(self.session.emit("config.changed", payload, source="user"), exclusive=False)
        self.dirty = True

    def action_components(self) -> None:
        rows = check(self.loaded.config, self.registry, lambda n: self.status.infos.get(n), env=None,
                     account_probe=lambda a: self.status.account_infos.get(a))
        self.push_screen(TextScreen(views.components_view(self.loaded, self.status, self.registry, rows), "COMPONENTS · F3 to change"))

    async def action_home(self) -> None:
        await self._command("/home")

    def action_nav(self) -> None:
        self.focus_composer()

    def _focus_ring(self) -> list:
        ring = [w for w in (self.q("#qcard"), self.q("#pcard"), self.q("#quotacard")) if w.display]
        m = self.mode()
        if m == "PLAN":
            ring.append(self.q("#planlist"))
        elif m == "RUN":
            ring.append(self.q("#agents"))
        return ring + [self.q("#composer", Composer)]

    def action_toggle_focus(self) -> None:
        c = self.q("#composer", Composer)
        if self.focused is c and c.items:
            c.accept()                                  # Tab completes an @file / command first
            return
        ring = self._focus_ring()
        i = ring.index(self.focused) if self.focused in ring else -1
        ring[(i + 1) % len(ring)].focus()
        self.dirty = True

    def on_descendant_focus(self, _) -> None:
        self.dirty = True

    def on_descendant_blur(self, _) -> None:
        self.dirty = True

    async def on_select_list_action(self, msg: SelectList.Action) -> None:
        k = msg.key
        if msg.lst.id == "planlist":
            if k in ("c", "enter"):
                self.action_comment()
            elif k == "p":
                await self.action_pin()
            elif k == "d":
                await self.action_diff()
        else:
            if k in ("enter", "i"):
                await self.action_inspect()
            elif k == "d":
                await self.action_diff()
            elif k in ("m", "c"):
                await self.action_context()

    def on_select_list_typed(self, msg: SelectList.Typed) -> None:
        c = self.q("#composer", Composer)
        c.load_text(c.text + msg.char); c.focus(); c.move_cursor((0, len(c.text)))

    async def on_key_card_pressed(self, msg: KeyCard.Pressed) -> None:
        if msg.card.id == "quotacard":
            if msg.key == "f":
                await self.action_quota_fallback()
            else:
                self.action_quota_wait()
            return
        if msg.key == "a":
            await self.action_accept()
        elif msg.key == "x":
            await self.action_reject()
        elif msg.key == "e":
            item = (self.proj.pending or {}).get("item")
            await self.action_reject()
            c = self.q("#composer", Composer)
            c.load_text(f"[{item}] " if item else ""); c.focus(); c.move_cursor((0, len(c.text)))
        self.dirty = True

    async def on_question_card_answered(self, msg: QuestionCard.Answered) -> None:
        await self._answer_question(msg.qid, msg.option, msg.text)

    def on_question_card_free_text(self, msg: QuestionCard.FreeText) -> None:
        self.answering = msg.qid
        c = self.q("#composer", Composer)
        c.load_text(msg.first); c.focus(); c.move_cursor((0, len(c.text)))
        self._sys(f"typing your own answer to {msg.qid} — Enter sends it")

    async def _answer_question(self, qid: str, option: int | None, text: str | None) -> None:
        q = next((x for x in self.open_questions() if x["id"] == qid), None)
        if q is None:
            return
        self.answering = None
        if q["src"] == "clarify":
            self.clarify_answers[qid] = (option, q["options"][option]) if option is not None else (len(q["options"]), text or "")
            self.proj.questions.setdefault(qid, {"answered": False})["answered"] = True
            self.proj.chat.append({"who": "you", "text": f"{qid} → " + (q["options"][option] if option is not None else text or "")})
            if all(x.id in self.clarify_answers for x in self.clarify):
                self.clarify_future.set_result(dict(self.clarify_answers))
        else:
            st = self.session.studio
            try:
                await (st.answer(qid, option=option) if option is not None else st.answer(qid, text=text))
            except StudioError as e:
                self._sys("; ".join(e.reasons))
        self.dirty = True

    def action_focus_composer(self) -> None:
        self.focus_composer()

    def action_comment(self) -> None:
        it = self.selected_item()
        if self.mode() in ("PLAN", "RUN") and it:
            c = self.q("#composer", Composer)
            c.load_text(f"[{it}] "); c.focus(); c.move_cursor((0, len(c.text)))

    async def action_accept(self) -> None:
        st = self.session.studio if self.session else None
        if self.proposal_future and not self.proposal_future.done():
            self.proposal_future.set_result(True)
        elif st and st.pending:
            self.busy = "applying…"
            try:
                await st.accept()
                self.session.plan = st.current.plan
            except StudioError as e:
                self._sys("; ".join(e.reasons))
            self.busy = ""
            s = self.session
            if not st.locked and not s.architect_reviewed:
                self.busy = "Architect is reviewing…"; self.dirty = True
                async def go():
                    await s._review_plan(); self.busy = ""; self.dirty = True
                self.run_worker(go(), exclusive=False, group="session")

    async def action_reject(self) -> None:
        st = self.session.studio if self.session else None
        if self.proposal_future and not self.proposal_future.done():
            self.proposal_future.set_result(False)
        elif st and st.pending:
            await st.reject()

    async def action_diff(self) -> None:
        if not self.session:
            return
        ws = self.session.ws                                  # everything the session changed: its commits + what is not committed yet
        d = await gitws.diff_since_base(ws)
        commits = await gitws.git(ws.path, "log", "--oneline", f"{ws.base_commit}..HEAD", check=False)
        pending = await gitws.changed_files(ws)
        head = (f"commits on {ws.branch}:\n" + "\n".join("  " + c for c in commits.splitlines()) + "\n" if commits else "no commits yet\n")
        head += f"not committed yet: {', '.join(pending)}\n" if pending else ""
        self.push_screen(TextScreen(Text(head + "\n" + d if d else "(no changes yet)"), f"DIFF · {ws.branch} since {ws.base_commit[:7]}"))

    async def action_context(self) -> None:
        if self.session:
            self.push_screen(TextScreen(Text(await render_memory(self.session.mem)), "session.md (L2 session memory)"))

    async def action_pin(self) -> None:
        st = self.session.studio if self.session else None
        if st and self.last_main_answer:
            nid = await st.pin(self.last_main_answer)
            self._sys(f"pinned as {nid}")

    async def action_inspect(self) -> None:
        if not self.session or not self.session.plan:
            return
        agents = [c for c in views.run_cards(self.proj) if not isinstance(c, dict)]
        if not agents:
            return
        a = agents[self.q("#agents", SelectList).sel % len(agents)]
        spec = next((t for t in self.session.plan.tasks if t.id == a.task), None)
        pkt = await builder.build(self.session.mem, a.role, spec, max_tokens=self.loaded.config.context.packet_max_tokens)
        self.push_screen(TextScreen(Text(f"{a.role} {a.provider}·{a.model} task {a.task}\nThis is everything the agent receives (no chat, no other agent's transcript):\n\n" + render_packet(pkt)),
                                    f"INSPECT · {a.role} {a.task}"))

    async def action_quota_fallback(self) -> None:
        if self.session and self.proj.status == "WAITING_QUOTA" and self.session.quota_hit:
            r = resolve(self.loaded.config, "worker", overrides=self.session.overrides)
            moved = await self.session.use_fallbacks(r.provider, r.account)
            self._sys("; ".join(moved) or "no fallback configured")
            await self._resume()

    def action_quota_wait(self) -> None:
        if self.proj.status == "WAITING_QUOTA":
            self._sys(f"waiting for the quota to reset ({(self.proj.quota_hit or {}).get('reset')}); type /resume to continue")

    # ------------------------------------------------------------------ input
    def _sys(self, text: str) -> None:
        self.proj.chat.append({"who": "sys", "text": text})
        self.dirty = True

    async def on_composer_submitted(self, msg: Composer.Submitted) -> None:
        c = self.q("#composer", Composer)
        text = msg.text.strip()
        if self.busy and not text.startswith("/"):              # commands have their own guards and must always get through
            return self._sys(f"still working on: {self.busy} — wait, or /stop to interrupt")
        c.load_text("")
        self.q("#sugg", Static).update("")
        self.q("#sugg", Static).display = False
        self.dirty = True
        if text.startswith("/"):
            return await self._command(text)
        if self.session is None:
            return await self._start(text)
        qs = self.open_questions()
        target = next((q for q in qs if q["id"] == self.answering), None) or (qs[0] if qs and (qs[0]["src"] == "clarify" or text.isdigit()) else None)
        if target:
            if text.isdigit() and 1 <= int(text) <= len(target["options"]):
                return await self._answer_question(target["id"], int(text) - 1, None)
            return await self._answer_question(target["id"], None, text)
        if self.mode() == "RESULT":
            return await self._ask_main(text)
        await self._ask_main(text)

    async def _start(self, text: str) -> None:
        if self.repo_problem:
            return self._sys(self.repo_problem)
        mentions, _ = parse_mentions(text)
        missing = [m for m in mentions if not (Path(m).expanduser() if m.startswith(("~", "/")) else self.repo / m).exists()]
        if missing:
            return self._sys("cannot find " + ", ".join("@" + m for m in missing) + " — fix the path (type @ and press Tab to pick a file) and send again")
        self.busy = "creating session, worktree and reading your inputs…"
        self.dirty = True
        try:
            ui = TuiUI(self)
            self.session = await open_session(self.repo, text, self.loaded, self.router, ui, self.home, self.registry)
        except Exception as e:
            self.busy = ""
            self._sys(f"could not start: {e}")
            self.q("#composer", Composer).load_text(text)           # never lose what the user wrote
            return
        self.busy = "starting Main…"
        self.t0 = time.monotonic()
        self.run_worker(self._pump(), exclusive=False, group="session")
        self.run_worker(self._drive(), exclusive=False, group="session")

    async def _open(self, sid: str) -> None:
        """Reopen an earlier session: replay its history into the screen, then continue where it makes sense."""
        self.busy = f"reopening {sid}…"
        self.dirty = True
        try:
            s = await load_session(sid, self.loaded, self.router, TuiUI(self), self.home, self.registry)
        except ReopenError as e:
            self.busy = ""
            return self._sys(str(e))
        if s.repo.resolve() != self.repo:
            await s.bus.store.close()
            self.busy = ""
            return self._sys(f"{sid} belongs to {s.repo}: run `agent-desk resume {sid}` from there")
        self.session = s
        self.t0 = time.monotonic()
        self.run_worker(self._pump(), exclusive=False, group="session")
        last = (await s.bus.store.events(sid))[-1].seq
        for _ in range(100):                                  # let the replay reach the reopen event before deciding the mode
            if self.proj.last_seq >= last:
                break
            await asyncio.sleep(0.03)
        if s.studio is not None and s.studio.locked:           # plan approved: show where it stands; /resume continues the work
            await self._finish(s.outcome)
            if s.outcome != "COMPLETED":
                self._sys(f"{sid} is {s.outcome}: /resume continues the unfinished tasks, or ask Main below")
        else:                                                 # still planning: straight back into the plan studio
            self.busy = "back to the plan…"
            self.run_worker(self._drive(), exclusive=False, group="session")

    async def _go_home(self) -> None:
        """Leave the current session (kept on disk, reopen with /open) and show the home screen for a new task."""
        s, self.session = self.session, None
        self.workers.cancel_group(self, "session")
        try:
            await s.bus.store.close()                         # the router is shared across sessions: keep it open
        except Exception:
            pass
        self.proj, self.outcome, self.busy, self.facts, self.notes = Projection(), None, "", {}, []
        self.clarify, self.clarify_answers, self.answering, self.card_ids, self.last_main_answer = [], {}, None, set(), ""
        for f in (self.clarify_future, self.studio_future, self.proposal_future):
            if f and not f.done():
                f.cancel()
        self.clarify_future = self.studio_future = self.proposal_future = None
        self._load_recent()
        self.dirty = True
        self.focus_composer()

    async def _pump(self) -> None:
        async for e in self.session.bus.subscribe(self.session.sid):
            if self.proj.apply(e):
                if e.type == "chat.main":
                    self.last_main_answer = e.payload.get("text", "")
                if e.type in ("plan.version", "plan.approved", "question.opened", "clarify.asked", "agent.started"):
                    self.busy = ""                        # from here the live bubble / agent cards show what is happening
                self.dirty = True

    async def _drive(self) -> None:
        try:
            outcome = await run_graph(self.session)
        except Exception as e:
            outcome = "FAILED"
            self.proj.errors.append(repr(e))
            self._sys(f"internal error: {e!r}")
        await self._finish(outcome)

    async def _finish(self, outcome: str) -> None:
        self.busy = ""
        self.outcome = outcome
        await asyncio.sleep(0.3)                                  # let the last events reach the projection
        self.proj.finish(outcome)
        self.result_seen = (len(self.proj.chat), False)       # the result opens at the top; only later messages scroll
        s = self.session
        try:
            base = s.ws.base_commit
            self.facts = {"files": (await gitws.git(s.ws.path, "diff", "--name-only", base, "HEAD", check=False)).splitlines(),
                          "commits": (await gitws.git(s.ws.path, "log", "--oneline", f"{base}..HEAD", check=False)).splitlines(),
                          "followups": [f"{n['id']} {n['text']}" for n in await s.mem.notes("followup")]}
        except Exception:
            pass
        self.dirty = True

    async def _resume(self) -> None:
        self.proj.status, self.proj.mode = "RUNNING", "RUN"
        self.busy = "resuming…"
        self.dirty = True
        async def go():
            try:
                outcome = await self.session.resume_after_quota()
            except Exception as e:
                outcome = "FAILED"; self._sys(f"internal error: {e!r}")
            await self._finish(outcome)
        self.run_worker(go(), exclusive=False, group="session")

    async def _ask_main(self, text: str) -> None:
        item = None
        if text.startswith("[") and "]" in text:
            item, text = text[1:text.index("]")], text[text.index("]") + 1:].strip()
        st = self.session.studio
        if st is None:
            return self._sys("Main is not ready yet")
        self.busy = "sending to Main…"
        self.dirty = True
        async def go():
            try:
                if st.locked:
                    await self.session.user_message(text, item)
                else:
                    await st.ask(text, item)
            except StudioError as e:
                self._sys("; ".join(e.reasons))
            except Exception as e:
                self._sys(f"Main could not answer: {e}")
            self.busy = ""
            self.dirty = True
        self.run_worker(go(), exclusive=False, group="session")

    # ------------------------------------------------------------------ commands
    async def _command(self, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        s = self.session
        try:
            if cmd in ("/quit", "/exit"):
                if s is not None and self.outcome is None and not self.sched_idle():
                    return self._sys("a session is still running: /stop it first, or press Ctrl+Q to leave anyway (work on disk is kept)")
                self.exit()
            elif cmd == "/help":
                self._sys("  ".join(f"{k}" for k in COMMANDS))
            elif cmd == "/settings":
                self.action_settings()
            elif cmd == "/checks":
                self._sys("checking the verification commands on a clean checkout (cached per commit)…")
                self.run_worker(self._checks(refresh="--refresh" in rest), exclusive=False)
            elif cmd == "/account":
                await self._account(rest.split())
            elif cmd == "/config":
                self.action_components()
            elif cmd in ("/context", "/diff", "/pin"):
                await {"/context": self.action_context, "/diff": self.action_diff, "/pin": self.action_pin}[cmd]()
            elif cmd in ("/home", "/new"):
                if s is None:
                    return self._sys("already on the home screen")
                if self.outcome is None and not self.sched_idle():
                    return self._sys("the session is still running: /stop it first (work on disk is kept), then /home")
                await self._go_home()
            elif cmd == "/open":
                if s is not None:
                    await self._go_home()
                if not rest.strip():
                    return self._sys("usage: /open <session id> (listed on the home screen)")
                await self._open(rest.strip())
            elif s is None:
                self._sys(f"{cmd} needs a running session; describe your task first")
            elif cmd == "/approve":
                if s.studio is None or s.studio.locked:
                    return self._sys("nothing to approve")
                await s.studio.approve(skip_review="--skip-review" in rest)
                if self.studio_future and not self.studio_future.done():
                    self.studio_future.set_result(True)
            elif cmd == "/questions":
                qs = [q for q in s.studio.questions.values() if not q.answered] if s.studio else []
                self._sys("open: " + ("; ".join(f"{q.id} {q.text}" for q in qs) if qs else "none"))
            elif cmd == "/review":
                self.busy = "Architect is reviewing…"; self.dirty = True
                async def go():
                    await s._review_plan(); self.busy = ""; self.dirty = True
                self.run_worker(go(), exclusive=False, group="session")
            elif cmd == "/diff-plan":
                a, b = (rest.split() + ["", ""])[:2]
                d = s.studio.diff(int(a), int(b))
                body = Text()
                for c in d.changes:
                    body.append(f"{c.op:<7} {c.id:<8} {'(material) ' if c.material else ''}{(c.before or '')[:60]} → {(c.after or '')[:60]}\n")
                self.push_screen(TextScreen(body or Text("no differences"), f"plan v{a} → v{b}"))
            elif cmd == "/model":
                parts = rest.split()
                if len(parts) < 2:
                    return self._sys("usage: /model <role> <provider>[@account] [model-or-tier]   e.g. /model architect claude@team opus")
                role, (prov, _, acc) = parts[0], parts[1].partition("@")
                mt = parts[2] if len(parts) > 2 else None
                tier = mt if mt in self.loaded.config.tiers.get(prov, {}) else None
                self._sys(await s.switch_role(role, prov, None if tier else mt, tier, acc or None))
            elif cmd == "/engine":
                parts = rest.split()
                if len(parts) < 2:
                    return self._sys("usage: /engine <point> <engine> [provider] [model]")
                self._sys(await s.switch_engine(parts[0], parts[1], *(parts[2:4])))
            elif cmd == "/stop":
                r = await self.push_screen_wait(ChoiceScreen(Text.assemble(("Stop the session?\n\n", "bold red"), "Will: stop new work, interrupt running agents, mark INTERRUPTED.\n",
                                                                           "Will NOT: roll back, delete the worktree, branch or commits.\n\n", ("[y] stop   [n] keep running", "bold")),
                                                              {"y": "y", "n": "n"}, "/stop"))
                if r == "y":
                    await s.stop()
            elif cmd == "/baseline":
                s.baseline_task = None
                s.baseline_notes.clear()
                self._sys("re-running the checks on the base commit in the background…")
                s._baseline_runner()
                s.baseline_task = asyncio.create_task(s._precompute_baseline(refresh=True))
            elif cmd == "/setup":
                if self.outcome is None and not self.sched_idle():
                    return self._sys("the session is still running")
                self._sys("preparing the worktree (" + (f"running `{s.cfg.workspace.setup}`" if s.cfg.workspace.setup else "linking dependencies") + ") then resuming…")
                self.outcome = None
                self.proj.status, self.proj.mode = "RUNNING", "RUN"
                self.busy = "setting up…"
                async def go():
                    try:
                        outcome = await s.setup_and_resume()
                    except Exception as e:
                        outcome = "FAILED"; self._sys(f"setup failed: {e!r}")
                    await self._finish(outcome)
                self.run_worker(go(), exclusive=False)
            elif cmd == "/resume":
                locked = s.studio is not None and s.studio.locked
                if self.proj.status == "WAITING_QUOTA" or (locked and self.outcome in ("FAILED", "INTERRUPTED", "CANCELLED", "WAITING_QUOTA")):
                    self.outcome = None
                    await self._resume()
                else:
                    self._sys("nothing to resume")
            else:
                self._sys(f"unknown command {cmd}; /help")
        except StudioError as e:
            self._sys("cannot approve yet: " + "; ".join(e.reasons))
        except Exception as e:
            self._sys(f"{cmd}: {e}")
