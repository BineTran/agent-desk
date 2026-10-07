"""Rich renderers. Pure functions of (projection, config, status): easy to test, and the TUI just displays them."""
from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config.check import resolve_model
from ..roles import resolve
from .projection import Projection

C = {"main": "cyan", "codex": "cyan", "claude": "magenta", "ok": "green", "bad": "red", "warn": "yellow", "dim": "bright_black", "agent": "blue", "arch": "medium_purple1"}
STATUS_STYLE = {"RUNNING": "green", "PLANNING": "cyan", "COMPLETED": "green", "FAILED": "red", "CANCELLED": "red", "WAITING_QUOTA": "red", "WAITING YOU": "yellow", "VERIFYING": "cyan"}


def pv(provider: str, model: str = "") -> Text:
    style = {"codex": "bold black on cyan", "claude": "bold black on magenta"}.get(provider, "bold white on grey30")
    return Text(f" {provider}" + (f"·{model}" if model else "") + " ", style=style)


def engine_label(pt) -> str:
    return (pt.model or pt.engine) if pt.engine == "llm" else pt.engine


def runs_with(cfg) -> Text:
    t = Text()
    for i, role in enumerate(r for r in cfg.roles if cfg.roles[r].enabled):
        r = resolve(cfg, role, None if not cfg.roles[role].tiers else cfg.roles[role].default_tier)
        t.append(("  " if i else "") + role.capitalize() + " ", style="bright_black")
        t.append_text(pv(r.provider, r.model))
        if cfg.roles[role].fallback:
            t.append(" (fb " + cfg.roles[role].fallback.provider + ")", style="bright_black")
    live = sorted({engine_label(v) for v in cfg.decision.points.values() if v.engine != "none"})
    t.append("  Decision ", style="bright_black")
    t.append("rules" + ("+" + "+".join(live) if live else ""), style="bold")
    return t


def _quota_bar(pct: float, width: int = 6) -> Text:
    n = int(round(width * min(pct, 100) / 100))
    color = "red" if pct >= 100 else "yellow" if pct >= 80 else "green"
    t = Text("▮" * n, style=color)
    t.append("▯" * (width - n), style="bright_black")
    return t


LIMIT_STYLE = {"allowed": "green", "allowed_warning": "yellow", "rejected": "red"}
WINDOW = {"five_hour": "5h", "seven_day": "week", "seven_day_opus": "week·opus", "seven_day_sonnet": "week·sonnet"}


def limit_text(info: dict) -> Text:
    """Claude only reports a status and a reset time per window (no percent)."""
    import datetime
    st = info.get("status", "?")
    t = Text(f" {WINDOW.get(info.get('rateLimitType', ''), info.get('rateLimitType', ''))} ", style="bright_black")
    pct = info.get("utilization")
    label = f"{float(pct) * 100 if float(pct) <= 1 else float(pct):.0f}%" if isinstance(pct, (int, float)) else {"allowed": "ok", "allowed_warning": "near limit", "rejected": "limit reached"}.get(st, st)
    t.append(label, style=LIMIT_STYLE.get(st, "white"))
    if info.get("resetsAt"):
        t.append(" · reset " + datetime.datetime.fromtimestamp(int(info["resetsAt"])).strftime("%H:%M"), style="bright_black" if st == "allowed" else LIMIT_STYLE.get(st, ""))
    return t


def status_bar(cfg, st, notes: list[str] | None = None, limits: dict | None = None, mode: str | None = None) -> Text:
    t = Text()
    for name, p in st.providers.items():
        ok = p.logged_in and not p.mismatch
        t.append("● ", style="green" if ok else "red" if (p.logged_in is False or p.mismatch) else "yellow")
        label = name + (f"→{p.account_name}" if p.account_name and p.account_name != name else "")
        t.append(f"{label} " + ("wrong account" if p.mismatch else "login" if ok else "not logged in" if p.logged_in is False else "?"), style="white" if ok else "red")
        if p.quota:
            pct = max(p.quota.values())
            t.append(" ")
            t.append_text(_quota_bar(pct))
            t.append(f" {pct:.0f}%", style="red" if pct >= 100 else "yellow" if pct >= 80 else "bright_black")
        elif (limits or {}).get(name):
            t.append_text(limit_text(limits[name]))
        elif ok and not p.quota:
            t.append(" quota: after first run", style="bright_black")
        t.append("   ")
    pts = cfg.decision.points
    live = {engine_label(v) for v in pts.values() if v.engine != "none"}
    t.append("decision ", style="bright_black")
    t.append("rules" + ("+" + "+".join(sorted(live)) if live else ""), style="bold")
    t.append("   sandbox ", style="bright_black")
    t.append("✓", style="green")
    if mode:
        t.append("   approvals ", style="bright_black")
        t.append("AUTO ⚡" if mode == "auto" else "ask", style="bold yellow" if mode == "auto" else "white")
        t.append(" · Shift+Tab", style="bright_black")
    t.append("   F2 config · Ctrl+S settings", style="bright_black")
    if notes:                                     # own line: provider status must never push them off-screen
        t.append("\n" + "   ".join(notes), style="cyan")
    return t


def header(proj: Projection, repo_name: str, elapsed: str = "", roles: Text | None = None) -> Text:
    t = Text()
    t.append("AGENT DESK", style="bold")
    t.append(f" · {repo_name}" + (f" · {proj.session_id}" if proj.session_id else ""), style="bright_black")
    t.append("   ")
    for m in ("PLAN", "RUN", "RESULT"):
        t.append(f" {m} ", style="bold black on cyan" if m == proj.mode else "bright_black")
        t.append(" ")
    waiting = proj.pending or any(not q["answered"] for q in proj.questions.values())
    status = "WAITING YOU" if (waiting and proj.status in ("PLANNING", "RUNNING")) else proj.status
    t.append(f" {status} ", style=f"bold {STATUS_STYLE.get(status, 'white')}")
    if proj.plan_version:
        t.append(f" plan v{proj.plan_version}" + (" ✓" if proj.approved else " draft"), style="bright_black")
    if elapsed:
        t.append("  " + elapsed, style="bright_black")
    if roles is not None:
        t.append("   ")
        t.append_text(roles)
    if proj.mode == "RUN":
        t.append("   /stop", style="bright_black")
    return t


SPIN = "◐◓◑◒"


def clock(sec: float) -> str:
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def header_roles(proj: Projection, main: tuple[str, str], arch: tuple[str, str], now: float) -> Text:
    t = Text("main ", style="bright_black")
    t.append_text(pv(*main))
    t.append("   architect ", style="bright_black")
    t.append_text(pv(*arch))
    a = next((x for x in proj.running_agents() if x.role == "architect"), None)
    t.append(f" reviewing {clock(now - a.started)}" if a else " on call", style="medium_purple1" if a else "bright_black")
    return t


# ---------------------------------------------------------------- PLAN
def plan_view(proj: Projection, selected: str | None = None, focused: bool = False, verify: list[str] | None = None) -> Group:
    pl = proj.plan
    if not pl:
        return Group(Panel(Text("No plan yet. Main asks about anything unclear first, then drafts the plan here.", style="bright_black"), title="PLAN", border_style="cyan"))
    pend = {c["id"] for c in (proj.pending or {}).get("changes", [])}
    ids = [t["id"] for t in pl["tasks"]] + [q["id"] for q in proj.questions.values()] + ["OUT-9"]
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(no_wrap=True, style="bright_black", width=min(max(map(len, ids)) + 1, 18))
    grid.add_column(ratio=1)

    def section(title):
        grid.add_row("", Text(title, style="bold bright_black"))

    def item(id_, text, extra=""):
        sel = id_ == selected and focused
        style = ("reverse " if sel else "") + ("bold yellow" if id_ in pend else "")
        t = Text(text, style=style.strip())
        if extra:
            t.append("  " + extra, style="bright_black")
        if id_ in pend:
            t.append("\n  ↳ proposed change, not accepted yet", style="yellow")
        elif proj.annotations.get(id_):
            t.append("\n  ↳ " + proj.annotations[id_], style="medium_purple1")
        grid.add_row(Text(("▸" if sel else " ") + id_, style="bold" if id_ == selected else "bright_black"), t)

    section("Goal"); item("", pl["goal"])
    section("Acceptance criteria")
    for i, a in enumerate(pl["acceptance_criteria"], 1): item(f"AC-{i}", a)
    section("Tasks (rules route them when they run)")
    for t in pl["tasks"]:
        dep = f"← {', '.join(t['depends_on'])}" if t["depends_on"] else ""
        st = proj.tasks.get(t["id"], "pending")
        outs = f"\n  → outputs: {', '.join(t['outputs'])}" if t.get("outputs") else ""
        item(t["id"], f"{t['kind']} · {t['title']}" + outs, f"{dep} [{st}]" if proj.approved else dep)
    if pl["risks"]:
        section("Risks")
        for i, r in enumerate(pl["risks"], 1): item(f"R-{i}", r)
    if pl["out_of_scope"]:
        section("Out of scope")
        for i, r in enumerate(pl["out_of_scope"], 1): item(f"OUT-{i}", r)
    if proj.findings:
        section("Architect review")
        for fid, f in proj.findings.items():
            v, q = f.get("verdict"), f.get("question")
            if v == "fixed":
                st, sty = "fixed ✓", "green"
            elif v == "rejected":
                st, sty = "rejected", "bright_black"
            elif v == "question" and q in proj.questions:
                done = proj.questions[q]["answered"]
                st, sty = (f"{q} answered ✓", "green") if done else (f"{q} open ← blocks /approve", "bold red")
            else:
                st, sty = "not triaged yet", "yellow"
            line = Text(f"{f.get('severity', ''):<7} {(f.get('file') or '').rsplit('/', 1)[-1][:26]}  ", style="bright_black")
            line.append(st, style=sty)
            grid.add_row(Text(f" {fid}", style="bright_black"), line)
    open_q = [q for q in proj.questions.values() if not q["answered"]]
    if open_q:
        section("Open questions")
        for q in open_q: item(q["id"], q["text"], "← blocks /approve" + (f" · {q['evidence']}" if q.get("evidence") else ""))
    if verify:
        section("Verify"); item("", " · ".join(verify))
    badge = "approved ✓" if proj.approved else "proposal pending" if proj.pending else (f"draft · {len(open_q)} open question(s)" if open_q else "draft")
    hist = f"   v1 → v{proj.plan_version}" if proj.plan_version > 1 else ""
    title = Text.assemble((f"PLAN v{proj.plan_version}", "bold cyan"), f"  {badge}", (hist, "bright_black"))
    sub = Text("Tab here · ↑↓ item · c comment · p pin last answer · d diff-plan" if focused else "Tab to select items", style="bright_black")
    return Group(Panel(Group(grid, sub), title=title, title_align="left", border_style="yellow" if proj.pending else ("bold cyan" if focused else "cyan")))


def checklist_view(checks) -> Text:
    """Compact: one line when ready, else only what is still missing."""
    bad = [c for c in checks if not c.ok and c.blocking]
    warn = [c for c in checks if not c.ok and not c.blocking]
    t = Text("Ready to approve? ", style="bright_black")
    t.append("✓" if not bad else f"✗ {len(bad)} left", style="bold green" if not bad else "bold red")
    for c in bad:
        t.append("\n  ✗ ", style="red")
        t.append(c.label + (f" — {c.detail}" if c.detail else ""))
    for c in warn:
        t.append("\n  ! ", style="yellow")
        t.append(c.label + (f" — {c.detail}" if c.detail else ""), style="bright_black")
    return t


def next_step(proj: Projection, mode: str, checks=None, open_q: list | None = None, busy: str = "", frame: int = 0, running: str = "") -> tuple[Text, str]:
    """(the bar above the composer, the key hint): what the user can do NOW, from the current state. Pure, so it is testable."""
    t = Text()
    spin = SPIN[frame % len(SPIN)]
    open_q = open_q or []
    ok, bad, warn = "bold green", "bold red", "yellow"
    if mode == "CHAT":
        t.append("Ask anything or describe a change", style=ok)
        t.append("  ·  /ask /quick /plan force how Main handles it", style="bright_black")
        return t, "Enter send · @ file · / commands · F2 config · F3/Ctrl+S settings · Ctrl+N home"
    if proj.status == "WAITING_QUOTA":
        t.append(f"✗ quota reached ({(proj.quota_hit or {}).get('provider')}) · resets {(proj.quota_hit or {}).get('reset')}", style=bad)
        t.append("  ·  f switch roles to their fallback and continue  ·  w wait, then /resume", style="bright_black")
        return t, "f fallback · w wait · /resume · Ctrl+N home"
    if mode == "RESULT":
        if proj.status == "COMPLETED":
            t.append("✓ COMPLETED", style=ok)
            t.append("  ·  type the next request (same branch)  ·  Ctrl+D diff  ·  /context", style="bright_black")
        else:
            t.append(f"✗ {proj.status}", style=bad)
            t.append("  ·  /resume continues the unfinished tasks  ·  /setup if the machine was missing something  ·  or ask Main below", style="bright_black")
        return t, "Ctrl+D diff · /context session.md · F2 config · /resume · Ctrl+N new task · ask Main below"
    if mode == "RUN":
        done = sum(1 for v in proj.tasks.values() if v == "done")
        total = len((proj.plan or {}).get("tasks") or []) or len(proj.tasks)
        t.append(f"{spin} " + (running or "running"), style=warn)
        t.append(f"  ·  {done}/{total} done  ·  /stop  ·  Ctrl+D diff  ·  type to message Main"
                 + ("  ·  Shift+Tab auto-approve commands" if proj.approval_mode != "auto" else ""), style="bright_black")
        return t, "Enter send · Tab agents/cards · Ctrl+D diff · /context · /stop · Ctrl+N home · F2 config"
    # PLAN
    if busy:
        t.append(f"{spin} {busy}", style=warn)
        t.append("  ·  you can type comments meanwhile", style="bright_black")
        return t, "Enter send · Tab plan items/cards · /model · F2 config · /quit"
    if proj.pending:
        t.append("Proposal waiting for you", style=warn)
        t.append("  ·  a accept  ·  x reject  ·  e edit (reject + comment)", style="bright_black")
        return t, "a accept · e edit · x reject · Esc chat"
    if open_q:
        ids = ", ".join(q["id"] for q in open_q[:6])
        t.append(f"✗ {len(open_q)} question{'s' if len(open_q) > 1 else ''} wait for you: {ids}", style=bad)
        t.append("  ·  1–9 pick on the card  ·  or type your own answer", style="bright_black")
        return t, "1–9 pick · ↑↓ + Enter confirm · type to answer in your own words · Esc chat"
    failing = [c for c in (checks or []) if not c.ok and c.blocking]
    if any("architect" in c.label for c in failing):
        t.append("✗ the architect has not reviewed this version", style=bad)
        t.append("  ·  /review  ·  or /approve --skip-review", style="bright_black")
        return t, "Enter send · /review · /approve --skip-review · Tab plan items · /quit"
    if failing:
        t.append(f"✗ not ready: {failing[0].label}" + (f" (+{len(failing) - 1} more)" if len(failing) > 1 else ""), style=bad)
        return t, "Enter send · Tab plan items/cards · /questions · /review · /quit"
    t.append("✓ Ready", style=ok)
    t.append("  ·  /approve runs the plan  ·  type to change it  ·  /review asks the architect again", style="bright_black")
    for c in (checks or []):
        if not c.ok:
            t.append(f"  ·  ! {c.label}", style="yellow")
    return t, "/approve to run · type to comment · Tab plan items · /review · /findings · /quit"


WHO = {"you": ("you", "yellow"), "main": ("main", "cyan"), "sys": ("·", "bright_black"), "arch": ("architect", "medium_purple1")}
SEV = {"blocker": "bold red", "major": "red", "minor": "yellow", "nit": "bright_black"}


def message_view(m: dict) -> RenderableType:
    who, col = WHO.get(m["who"], (m["who"], "white"))
    t = Text(f"{who:<10}", style=f"bold {col}")
    if m.get("item"):
        t.append(f"[comment on {m['item']}] ", style="bright_black")
    if m.get("kind") == "proposal":
        t.append("[proposal] ", style="bold yellow")
    elif m.get("kind") == "answer":
        t.append("[answer · plan unchanged] ", style="bright_black")
    if m["who"] == "arch":
        t.append(f"{m.get('trigger', '')} → {m['text']}", style="medium_purple1")
        fs = m.get("findings") or []
        for f in fs:
            t.append(f"\n{'':<10}{str(f.get('severity', '')):<8}", style=SEV.get(f.get("severity", ""), ""))
            t.append(f"{str(f.get('id')):<4}", style="bold")
            t.append(f"{(f.get('file') or '').rsplit('/', 1)[-1]:<28.28} ", style="bright_black")
            msg = " ".join(str(f.get("message", "")).split())
            t.append(msg[:80] + ("…" if len(msg) > 80 else ""))
        if fs:
            t.append(f"\n{'':<10}/findings shows each one in full", style="bright_black")
        return t
    t.append(m["text"], style="bright_black" if m["who"] == "sys" else "")
    return t


def proposal_view(pc: dict, focused: bool) -> RenderableType:
    body = Text()
    for c in pc["changes"]:
        body.append(f"{'+' if c['op'] == 'add' else '-' if c['op'] == 'remove' else '~'} {c['id']}  ", style="green" if c["op"] == "add" else "red" if c["op"] == "remove" else "yellow")
        body.append(f"{(c.get('after') or c.get('before') or '')[:110]}" + ("" if c.get("material") else "  (non-material)") + "\n")
    body.append("[a] accept   [e] edit (reject + comment)   [x] reject", style="bold")
    if not focused:
        body.append("   · Tab to this card", style="bright_black")
    return Panel(body, title="proposal · " + ("material" if pc["material"] else "non-material") + (f" · from {pc['source']}" if pc.get("source") else ""),
                 title_align="left", border_style="yellow" if focused else "bright_black", padding=(0, 1))


def _tail(s: str, lines: int, width: int = 150) -> list[str]:
    out = [ln for ln in s.replace("\r", "").splitlines() if ln.strip()]
    return [ln[:width] for ln in out[-lines:]]


def live_view(a, now: float, frame: int, reply_lines: int = 8) -> RenderableType:
    """What a running agent is doing right now: spinner, label, elapsed, last activity, reasoning and reply as they stream."""
    t = Text(SPIN[frame % len(SPIN)] + " ", style="yellow")
    t.append(a.role, style="bold " + {"main": "cyan", "architect": "medium_purple1", "deployer": "dark_orange", "debug": "yellow"}.get(a.role, "blue"))
    t.append(f" · {a.label or a.task or 'working'} · {clock(now - a.started)} ", style="")
    t.append_text(pv(a.provider, a.model))
    idle = now - a.last_activity
    if a.activity:
        t.append(f"\n  {a.activity[:140]}", style="bright_black")
    for ln in _tail(a.live_output, 2):
        t.append(f"\n  │ {ln}", style="bright_black")
    for ln in _tail(a.live_reasoning, 3):
        t.append(f"\n  ⋯ {ln}", style="italic bright_black")
    reply = a.reply_preview()
    for ln in _tail(reply, reply_lines):
        t.append(f"\n  {ln}")
    if idle > 90:
        t.append(f"\n  no output for {clock(idle)} — still running (the model may be thinking) · /stop to interrupt", style="yellow")
    elif not (a.activity or a.live_reasoning or reply or a.live_output):
        t.append("\n  waiting for the first output…", style="bright_black")
    return t


def bubble_view(proj: Projection, now: float, frame: int, busy: str = "") -> RenderableType | None:
    agents = [a for a in proj.running_agents() if a.role in ("main", "architect")]
    parts: list = [live_view(a, now, frame) for a in agents]
    if busy and not agents:
        parts.append(Text(SPIN[frame % len(SPIN)] + " " + busy, style="italic yellow"))
    return Group(*parts) if parts else None


def chat_view(proj: Projection, limit: int = 14) -> Group:
    rows = [message_view(m) for m in proj.chat[-limit:]]
    return Group(*rows) if rows else Group(Text("Say something to Main, or answer the questions. /approve when ready.", style="bright_black"))


# ---------------------------------------------------------------- RUN
ENG_STYLE = {"rule": "white on grey23", "llm": "black on medium_purple1", "jev": "black on green", "main": "black on cyan", "user": "black on yellow"}
ENG_NAME = {"rule": "rule", "llm": "haiku", "jev": "jev", "main": "main", "user": "you"}


def _bar(p: float, width: int, style: str) -> Text:
    n = int(round(width * max(0.0, min(p, 1.0))))
    return Text("█" * n, style=style) + Text("░" * (width - n), style="grey30")


def decision_box(proj: Projection, width: int | None = None) -> Panel:
    rows = []
    for d in proj.decisions_log[-4:]:
        t = Text(f"{d['type']} {d.get('task') or ''}".strip() + " → ", style="")
        t.append(str(d["selected"]), style="bold")
        t = Text(t.plain[:34].ljust(36)) if len(t.plain) > 34 else t + Text(" " * (36 - len(t.plain)))
        eng = d["engine"]
        if d.get("confidence") is not None:
            col = "green" if d.get("sharp") else "cyan"
            t.append_text(_bar(d["confidence"], 16, col))
            t.append(f" {ENG_NAME.get(eng, eng)} ", style=ENG_STYLE.get(eng, ""))
            t.append(f" {d['confidence']:.2f} ", style="bold")
            t.append("sharp" if d.get("sharp") else "split", style=col)
        else:
            t.append("— deterministic —".ljust(17), style="bright_black")
            t.append(f" {ENG_NAME.get(eng, eng)} ", style=ENG_STYLE.get(eng, ""))
        rows.append(t)
    if not rows:
        rows.append(Text("no decisions yet", style="bright_black"))
    rows.append(Text.assemble(("rules run first · ", "bright_black"), ("sharp → code", "green"), (" · ", "bright_black"), ("split → main", "cyan"), (" · risky → you", "bright_black")))
    title = Text.assemble(("DECISION · fork layer", "bold green"), (f"   decisions {proj.decisions_count}", "bright_black"))
    return Panel(Group(*rows), title=title, title_align="left", border_style="green", width=width)


ICON = {"running": ("running", "yellow"), "done": ("✓ done", "green"), "failed": ("✗ failed", "red"), "interrupted": ("■ interrupted", "red"),
        "quota": ("⚠ quota", "red"), "wait": ("⧗ waiting you", "yellow"), "queued": ("⏸ queued", "bright_black")}
CARD_W = 36


def run_cards(proj: Projection) -> list:
    """Agents shown on RUN (newest last), then tasks still waiting their turn."""
    shown = [a for a in proj.agents.values() if a.role not in ("main", "architect", "decision")][-6:]
    return shown + proj.queued_tasks()


def _card(a, on: bool, now: float, frame: int) -> Panel:
    from rich.align import Align
    from .projection import clean_md
    if isinstance(a, dict):                                       # queued task
        body = Text.assemble(("task\n", "bold bright_black"), (f"{a['id']}\n", "bright_black"), (a["title"][:CARD_W - 4] + "\n", "bright_black"), ("⏸ queued", "bright_black"))
        if a.get("depends_on"):
            body.append(f"\nafter {', '.join(a['depends_on'])}", style="bright_black")
        body.justify = "center"
        return Panel(body, width=CARD_W, border_style="bold yellow" if on else "grey35", padding=(0, 1))
    body = Text(a.role + "\n", style="bold", justify="center")
    body.append_text(pv(a.provider, a.model)); body.append("\n")
    body.append(f"{a.task or ''} {a.label}"[:CARD_W - 4] + "\n")
    if a.running:
        body.append(f"{SPIN[frame % len(SPIN)]} running {clock(now - a.started)}", style="yellow")
        idle = now - a.last_activity
        if idle > 90:
            body.append(f" · quiet {clock(idle)}", style="yellow")
        note = a.activity or "starting…"
        tail = (_tail(a.live_output, 1) or _tail(clean_md(a.live_reasoning), 1) or [""])[0]
        body.append("\n" + note[:CARD_W - 4], style="bright_black")
        if tail:
            body.append("\n" + tail[:CARD_W - 4], style="italic bright_black")
    else:
        lab, col = ICON.get(a.status, (a.status, "white"))
        body.append(lab, style=col)
        if a.note:
            body.append("\n" + a.note[:CARD_W - 4], style="bright_black")
    border = "bold yellow" if on else {"failed": "red", "interrupted": "red", "quota": "red", "wait": "yellow"}.get(a.status, "blue")
    return Panel(body, width=CARD_W, border_style=border, padding=(0, 1))


def agent_cards(proj: Projection, sel: int = -1, focused: bool = False, now: float = 0, frame: int = 0, per_row: int = 3) -> RenderableType:
    """Fixed-width cards centred in rows, like the prototype's agent tree."""
    from rich.align import Align
    from rich.columns import Columns
    cards = [_card(a, focused and i == sel, now, frame) for i, a in enumerate(run_cards(proj))]
    if not cards:
        return Align.center(Text("no agents yet · rules route each task when it is ready", style="bright_black"))
    rows = []
    for i in range(0, len(cards), per_row):
        g = Table.grid(padding=(0, 1))
        for c in cards[i:i + per_row]:
            g.add_column(width=CARD_W)
        g.add_row(*cards[i:i + per_row])
        rows.append(Align.center(g))
    return Group(*rows)


def architect_box(proj: Projection, now: float = 0, frame: int = 0, pm: tuple[str, str] | None = None) -> Panel:
    a = proj.architect
    live = next((x for x in proj.running_agents() if x.role == "architect"), None)
    trig = {"before_plan": "before a plan", "error_repeats": "error repeats", "before_done": "before done"}
    done = {r["trigger"] for r in a["reviews"]}
    last = a["reviews"][-1] if a["reviews"] else None
    state = "reviewing" if live else "advising" if (last and last["n"]) else "on call"
    head = Text("ARCHITECT\n", style="bold medium_purple1", justify="center")
    if pm:
        head.append_text(pv(*pm)); head.append("\n")
    head.append(f"{SPIN[frame % len(SPIN)]} {state} {clock(now - live.started)}" if live else state, style="medium_purple1")
    t = Text("")
    for k, label in trig.items():
        active = live is not None and k in live.label
        t.append(("◆ " if active else "✓ " if k in done else "◇ ") + label + "\n",
                 style="bold medium_purple1" if active else "medium_purple1" if k in done else "bright_black")
    if live and live.activity:
        t.append(live.activity[:24] + "\n", style="bright_black")
    t.append("\nlast advice:\n", style="bright_black")
    t.append(f"» {a['advice']}\n", style="medium_purple1")
    t.append(f"\ncalls {a['calls']}\ntokens read {a['tokens']:,}\n", style="bright_black")
    t.append("\nnever writes code.\nMain applies the review.", style="bright_black")
    return Panel(Group(head, Text(""), t), border_style="medium_purple1", padding=(0, 1))


VSTYLE = {"pass": ("✓", "green"), "skipped": ("–", "bright_black"), "pre-existing": ("•", "yellow"),
          "regression": ("✗", "red"), "environment": ("✗", "red")}


def verify_mark(v: dict) -> tuple[str, str]:
    st = v.get("status") or ("pass" if v.get("passed") else "regression")
    return VSTYLE.get(st, ("✗", "red"))


def verify_rows(proj: Projection) -> list:
    """One line per check with its reason: regression (new failures) / pre-existing / skipped / environment."""
    rows = []
    for n, v in proj.verify.items():
        mark, style = verify_mark(v)
        st = v.get("status") or ("pass" if v.get("passed") else "regression")
        why = {"pass": "", "skipped": v.get("skip", ""),
               "pre-existing": f"already failing on the base commit ({v.get('failures', 0)} failures, none new) — not blocking",
               "environment": "the machine is missing something: " + (v.get("summary") or ""),
               }.get(st, "new failures: " + (", ".join(v.get("new", [])[:4]) or v.get("summary") or ""))
        line = Text.assemble((f"  {mark} {n}", style), (f"  {st}" if st != "pass" else "", style), (f" — {why}" if why else "", "bright_black"))
        if v.get("wrote"):
            line.append(f"  (tried to rewrite {len(v['wrote'])} file(s): reverted — set writes: true to keep)", style="yellow")
        rows.append(line)
    return rows


def baseline_note(proj: Projection) -> str:
    bad = {n: b for n, b in (getattr(proj, "baseline", {}) or {}).items() if b.get("passed") is False}
    if not bad:
        return ""
    return "already failing on base: " + ", ".join(bad) + " — only new failures will block"


def verify_line(proj: Projection, width: int | None = None) -> Panel:
    t = Text.assemble(("review + verify", "bold cyan"), (" · ", "bright_black"))
    if not proj.verify:
        t.append("no results yet ", style="bright_black")
    for n, v in proj.verify.items():
        mark, style = verify_mark(v)
        t.append(f"{mark} {n}  ", style=style)
    t.append(f"· approvals {proj.approvals}", style="bright_black")
    return Panel(t, border_style="cyan", width=width, padding=(0, 1))


verify_box = verify_line


LOG_WHO = {"main": "cyan", "worker": "blue", "explorer": "blue", "researcher": "blue", "architect": "medium_purple1", "haiku": "medium_purple1", "deployer": "dark_orange", "debug": "yellow",
           "jev": "green", "you": "yellow", "gate": "yellow", "verify": "cyan"}


def log_box(proj: Projection, n: int = 7) -> Panel:
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bright_black", no_wrap=True); g.add_column(no_wrap=True); g.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
    for ts, who, _typ, msg in proj.log[-n:]:
        g.add_row(ts, Text(who[:10], style=LOG_WHO.get(who, "bright_black")), msg)
    return Panel(g if proj.log else Text("—", style="bright_black"), title="session log", title_align="left", border_style="grey35")


def main_box(proj: Projection, main_pv: Text, effort: str | None, now: float, frame: int, width: int | None) -> Panel:
    m = next((x for x in proj.running_agents() if x.role == "main"), None)
    lv = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 4, "ultra": 4}.get((effort or "").lower(), 0)
    head = Text.assemble(("Main ", "bold cyan"), main_pv, (" · plans + replans", "bright_black"))
    line = Text.assemble(("effort ", "bright_black"), ("▮" * lv, "cyan"), ("▯" * (4 - lv), "grey30"), (f" {effort or '—'} · ", "bright_black"))
    if m:
        line.append(f"{SPIN[frame % len(SPIN)]} {m.label} {clock(now - m.started)}", style="yellow")
    else:
        last = next((c for c in reversed(proj.chat) if c["who"] == "main"), None)
        if last and proj.status == "RUNNING":
            line.append(last["text"][:90])
        else:
            line.append(f"plan v{proj.plan_version} ✓ · waiting for results" if proj.status == "RUNNING" else proj.status.lower(), style="")
    return Panel(Group(head, line), border_style="cyan", width=width, padding=(0, 1))


def _spine() -> RenderableType:
    from rich.align import Align
    return Align.center(Text("│", style="grey35"))


def run_tree_top(proj: Projection, main_pv: Text, effort: str | None, cfg, width: int, now: float, frame: int) -> RenderableType:
    from rich.align import Align
    w = max(60, min(100, width - 2))
    running = [a for a in proj.running_agents() if a.role not in ("main", "architect", "decision")]
    writers = sum(1 for a in running if a.role in ("worker", "deployer"))
    mx, mw = (cfg.scheduler.max_total_agents, cfg.scheduler.max_writers) if cfg else (6, 1)
    spawn = Text.assemble(("spawn agents · max ", "bright_black"), (str(mx), "bright_black"), (" · ", "bright_black"), (f"agents [{len(running)}/{mx}]", "blue"),
                          (" · ", "bright_black"), (f"writers [{writers}/{mw}]", "blue"), (f" · queued {len(proj.queued_tasks())}", "bright_black"))
    return Group(Align.center(main_box(proj, main_pv, effort, now, frame, w)), _spine(), Align.center(decision_box(proj, w)), Align.center(spawn))


def run_tree_bottom(proj: Projection, width: int) -> RenderableType:
    from rich.align import Align
    return Group(_spine(), Align.center(verify_line(proj, max(60, min(100, width - 2)))))


def run_top(proj: Projection, main_pv: Text, cfg, now: float, frame: int, effort: str | None = None, width: int = 130) -> Table:
    top = Table.grid(expand=True, padding=(0, 1))
    top.add_column(width=30); top.add_column(ratio=1)
    top.add_row(architect_box(proj, now, frame), run_tree_top(proj, main_pv, effort, cfg, width - 32, now, frame))
    return top


def run_view(proj: Projection, main_pv: Text, cfg=None, now: float | None = None, frame: int = 0) -> Group:
    """Static RUN screen (used by `agent-desk show` and tests); the TUI composes the same parts with a focusable agent list."""
    import time
    now = time.monotonic() if now is None else now
    return Group(run_top(proj, main_pv, cfg, now, frame), agent_cards(proj, now=now, frame=frame), run_tree_bottom(proj, 130), log_box(proj))


# ---------------------------------------------------------------- RESULT
def result_view(proj: Projection, cfg, facts: dict) -> Group:
    rows: list = []
    status_col = STATUS_STYLE.get(proj.status, "white")
    rows.append(Text.assemble((proj.status, f"bold {status_col}"), f"  branch {proj.branch}  worktree {proj.worktree}"))
    if proj.status == "WAITING_QUOTA" and proj.quota_hit:
        rows.append(Text(f"Quota reached ({proj.quota_hit.get('provider')}) · resets {proj.quota_hit.get('reset')}. Progress is kept.", style="bold red"))
    if proj.errors:
        rows.append(Text("Errors:", style="bold red"))
        rows += [Text("  ✗ " + str(e)[:300], style="red") for e in proj.errors[-5:] if e]
    if proj.gate_unmet:
        rows.append(Text("Not complete:", style="bold red"))
        rows += [Text("  ✗ " + u, style="red") for u in proj.gate_unmet]
    ws = getattr(proj, "workspace", {}) or {}
    env_err = any("environment error" in str(x) for x in list(proj.errors) + list(proj.gate_unmet))
    if env_err and proj.status == "FAILED":
        how = (f"runs `{cfg.workspace.setup}` in the worktree" if cfg.workspace.setup
               else "links " + ", ".join(cfg.workspace.link[:3]) + " from your checkout")
        rows.append(Text(f"/setup prepares the worktree ({how}) and resumes · the machine, not the code, was missing something", style="bold yellow"))
    elif proj.status in ("FAILED", "INTERRUPTED", "CANCELLED", "WAITING_QUOTA") and proj.approved:
        rows.append(Text("/resume continues the unfinished tasks (done ones are kept) · or ask Main below", style="yellow"))
    st = ws.get("setup") or {}
    if st.get("status") == "failed":
        rows.append(Text(f"workspace.setup failed: `{st.get('command')}`\n  " + str(st.get("tail", ""))[-300:], style="red"))
    if ws.get("linked"):
        rows.append(Text("worktree uses your checkout's " + ", ".join(ws["linked"][:4]) + " (symlinked, never committed)", style="bright_black"))
        if any(f.endswith(("package.json", "yarn.lock", "pnpm-lock.yaml", "package-lock.json", "pyproject.toml", "uv.lock")) for f in facts.get("files") or []):
            rows.append(Text("dependencies changed in this session, but node_modules/.venv are shared with your checkout: run your install there, "
                             "or set workspace.setup so sessions install their own copy", style="yellow"))
    if proj.summary:
        rows += [Text("SUMMARY", style="bold cyan"), Text(proj.summary["text"])]
        if proj.summary.get("ac_evidence"):
            rows += [Text("ACCEPTANCE → EVIDENCE", style="bold cyan")] + [Text("  ✓ " + a) for a in proj.summary["ac_evidence"]]
    if proj.verify:
        rows += [Text("VERIFICATION", style="bold cyan")] + verify_rows(proj)
    elif baseline_note(proj):
        rows.append(Text(baseline_note(proj), style="yellow"))
    if facts.get("files"):
        rows += [Text(f"FILES CHANGED ({len(facts['files'])})", style="bold cyan"), Text("  " + " · ".join(facts["files"][:12]), style="bright_black")]
    if facts.get("commits"):
        rows += [Text("COMMITS", style="bold cyan")] + [Text("  " + c) for c in facts["commits"]]
    if proj.design_decisions:
        rows.append(Text("DECISIONS", style="bold cyan"))
        for d in proj.design_decisions:
            rows.append(Text(f"  {d['id']} {d['text'][:110]}", style="white") + Text(f"  ({d['source']})", style="bright_black"))
    if proj.followups or facts.get("followups"):
        rows += [Text("FOLLOW-UPS", style="bold cyan")] + [Text("  " + f) for f in (facts.get("followups") or proj.followups)]
    rows.append(Text("RAN ON (subscription: tokens, not money)", style="bold cyan"))
    for k, v in sorted(proj.tokens.items()):
        prov, role = k.split("|")
        d = proj.token_detail.get(k)
        rows.append(Text.assemble("  ", pv(prov), f" {role:<10} {v:,} tokens",
                                  (f"   fresh {d['fresh']:,} · cached {d['cached']:,} · out {d['output']:,}", "bright_black") if d else ""))
    kinds: dict = {}
    for d in proj.decisions_log:
        kinds[d["engine"]] = kinds.get(d["engine"], 0) + 1
    if kinds:
        rows.append(Text("  decisions: " + "  ".join(f"{k} {v}" for k, v in kinds.items()) + f" · approvals {proj.approvals} · architect reviews {len(proj.architect['reviews'])}", style="bright_black"))
    if proj.overrides:
        rows.append(Text("  in-session changes: " + "; ".join(str(o.get("text", o)) for o in proj.overrides), style="yellow"))
    rows.append(Text(f"\nnext step (yours): git -C <repo> merge --no-ff {proj.branch}", style="bright_black"))
    return Group(*rows)


# ---------------------------------------------------------------- COMPONENTS
LAYERS = ["default", "global", "repo", "--set", "session"]
SOURCE_STYLE = {"repo": "yellow", "--set": "white on grey30", "session": "cyan", "global": "magenta"}


def source_of(sources: dict, key: str) -> str:
    """Highest layer that set the key or anything under it."""
    found = [v for k, v in sources.items() if k == key or k.startswith(key + ".")]
    return max(found, key=LAYERS.index) if found else "default"


def source_badge(s: str) -> Text:
    return Text(f" {s} ", style=SOURCE_STYLE.get(s, "bright_black"))


def components_view(loaded, st, reg, rows) -> Group:
    cfg, src = loaded.config, loaded.sources

    def source(key: str) -> Text:
        return source_badge(source_of(src, key))

    out: list = [Text.assemble("config hash ", (loaded.snapshot_hash(), "bold"), "   source: default < global < repo < --set < session", style="bright_black")]
    t = Table(title="Providers", title_justify="left", box=None, pad_edge=False)
    for c in ("provider", "runtime", "auth", "state", "quota"):
        t.add_column(c, style="bright_black" if c == "runtime" else None)
    for name, p in cfg.providers.items():
        ps = st.providers.get(name)
        if p.kind == "decision":
            t.add_row(Text(name, style="bright_black"), p.runtime, p.auth + (f" ({p.env_key})" if p.env_key else ""), Text("decision provider (API key via " + (p.env_key or "env") + ")", style="bright_black"), "")
            continue
        acct = f"[{ps.account_name}] " if ps and ps.account_name else ""
        if ps and ps.mismatch:
            state = Text(f"✗ {acct}wrong account: {ps.mismatch} — {ps.login_hint}", style="red")
        elif ps and ps.logged_in:
            state = Text(f"● {acct}login {ps.account}", style="green")
        else:
            state = Text(f"✗ {acct}not logged in" + (f" — run: {ps.login_hint}" if ps else ""), style="red")
        q = "  ".join(f"{k} {v:.0f}%" for k, v in (ps.quota if ps else {}).items())
        t.add_row(pv(name), f"{p.runtime} {ps.version if ps else ''}", p.auth, state, q)
    out.append(t)
    if getattr(st, "accounts", None):
        t = Table(title="Accounts (one login each, own profile dir · F3 SETTINGS › Accounts)", title_justify="left", box=None)
        for c in ("account", "runtime", "state", "logged in as", "used by"):
            t.add_column(c, style="bright_black" if c == "runtime" else None)
        for a in st.accounts:
            t.add_row(a.name, a.runtime, Text(a.state, style="green" if a.state == "ok" else "red"),
                      a.identity or a.detail, ", ".join(a.used_by) or "-")
        out.append(t)
    used = {p.runtime for p in cfg.providers.values()}
    for name in reg.names("decision"):
        if name in ("jev",) and name not in used:
            out.append(Text(f"decision engine '{name}' available, not enabled — F3 SETTINGS › Jev (it plugs into the decision points below)", style="bright_black"))
    out.append(Text(f"policy: subscription_only {'✓' if cfg.policy.subscription_only else 'off'}" + (f"  API-key exceptions: {', '.join(cfg.policy.allow_api_key)}" if cfg.policy.allow_api_key else "  no provider uses an API key"), style="bright_black"))
    t = Table(title="Roles", title_justify="left", box=None)
    for c in ("role", "provider · model", "effort", "access", "enforced by", "source"):
        t.add_column(c)
    for role in cfg.roles:
        r = resolve(cfg, role, cfg.roles[role].default_tier if cfg.roles[role].tiers else None)
        enforced = "sandbox · dontAsk · hook" if r.sandbox == "workspaceWrite" and r.runtime == "claude-cli" else "read-only sandbox" if r.sandbox == "readOnly" else "workspace-write sandbox"
        fb = f"  fallback {cfg.roles[role].fallback.provider}" if cfg.roles[role].fallback else ""
        who = Text(f"@{r.account} ", style="magenta") if r.account else Text("")
        t.add_row(role, Text.assemble(pv(r.provider, r.model), " ", who, (fb, "bright_black")), r.effort or "—", cfg.roles[role].access + (" + network" if r.network else "") + (f" + mcp:{r.mcp if isinstance(r.mcp, str) else ','.join(r.mcp)}" if r.mcp != "none" else "") + ("" if cfg.roles[role].enabled else " (disabled)"), enforced, source(f"roles.{role}"))
    out.append(t)
    t = Table(title="Decision points (rules always run first)", title_justify="left", box=None)
    for c in ("point", "pipeline", "→ fallback", "source"):
        t.add_column(c)
    for k in ("route", "tier", "dangerous", "retry_or_stop"):
        pt = cfg.decision.points.get(k)
        eng = "rules → " + ("(no engine)" if not pt or pt.engine == "none" else f"{pt.engine} · {pt.provider}{'·' + pt.model if pt.model else ''} ≥{pt.sharp_threshold}")
        if pt and pt.shadow:
            eng += f"  (shadow {pt.shadow.get('engine')})"
        t.add_row(k, eng, pt.fallback if pt else "main", source(f"decision.points.{k}"))
    out.append(t)
    out.append(Text(f"Review (Architect): {', '.join(cfg.review.triggers) or 'off'} · max {cfg.review.max_rounds} rounds · {cfg.review.max_replans_per_task} replan/task", style="white"))
    out.append(Text("Safety 🔒  deny-list fixed · approve: " + ", ".join(cfg.approval.require_for) + " · invariants: rules first, sandbox for writers, plan lock before Worker, completion gate, persist-before-publish", style="bright_black"))
    if cfg.verification:
        out.append(Text("Verification: " + "   ".join(f"{k}: {v.command}{'' if v.required else ' (optional)'}" for k, v in cfg.verification.items())))
    out.append(Text("Plugins: runtime " + ", ".join(reg.names("runtime")) + " · decision " + ", ".join(reg.names("decision")) + (f" · external: {', '.join(cfg.plugins)}" if cfg.plugins else ""), style="bright_black"))
    bad = [r for r in rows if not r.ok]
    out.append(Text("Problems: " + ("none blocking ✓" if not bad else ""), style="green" if not bad else "red"))
    for r in bad:
        out.append(Text(f"  ✗ {r.role}: " + "; ".join(r.problems), style="red"))
    return Group(*out)


def doctor_view(rows: list[dict], commit: str) -> Group:
    """Result of the check doctor: each verification command on a clean checkout."""
    out: list = [Text(f"verification commands on a clean checkout of {commit} (cached; /checks --refresh to re-run)\n", style="bright_black")]
    for r in rows:
        ok = r["state"] in ("ok", "works (existing failures)")
        style = "green" if ok else "yellow" if r["state"] == "untested" else "red"
        out.append(Text.assemble((("✓ " if ok else "✗ ") + r["name"], f"bold {style}"), (f"  {r['state']}", style),
                                 (f" — {r['detail']}" if r.get("detail") else "", "bright_black")))
        out.append(Text(f"    $ {r['command']}"[:220], style="bright_black"))
        for n in r.get("notes") or []:
            out.append(Text(f"    ! {n}", style="yellow"))
        if not ok and r["state"] != "untested":
            out.append(Text("    → in a session this only blocks NEW failures; to fix it for good: scope it with paths + {changed}, "
                            "e.g. \"yarn biome lint {changed}\" with paths: [\"**/*.ts\"], or mark required: false", style="bright_black"))
    return Group(*out)
