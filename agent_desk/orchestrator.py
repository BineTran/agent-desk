"""Session orchestration: plan phase, execute loop, completion gate. Code owns the workflow."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import gitws, safety, verifier
from .config.loader import Loaded
from .context import builder
from .context.inputs import build_inputs
from .context.memory import Memory
from .context.render import render
from .architect import Architect, build_prompt
from .gatekeeper import EnvGate
from .contracts import (AgentReport, ControlDecision, Decision, Event, FileRef, Plan, Route, TaskSpec, now)
from .studio import Question
from .decision import rules
from .decision.engine import DecisionPipeline
from .events.bus import EventBus
from .reasoner import ChatTriage, Reasoner, ReasonerError, render_packet
from .studio import Studio, StudioError, plan_hash, PlanDiff, PlanVersion
from .roles import resolve
from .runtime.base import Approval, AgentRuntime, RunSpec, labelled
from .scheduler import DONE, FAILED, PENDING, STALE, Scheduler
from .ui import UI



def missing_outputs(outputs: list[str], changed: list[str], root: Path) -> list[str]:
    """The promised files (paths or globs) that this task's diff does not contain, or that do not exist."""
    return [o for o in outputs if not any(fnmatch.fnmatch(c, o) and (root / c).exists() for c in changed)]


@dataclass
class Session:
    sid: str
    repo: Path
    home: Path
    loaded: Loaded
    bus: EventBus
    runtime: AgentRuntime
    ui: UI
    mem: Memory
    ws: gitws.Workspace | None                             # None until a turn needs to change code (chat reads the user's checkout)
    sched: Scheduler
    reasoner: Reasoner | None = None
    plan: Plan | None = None
    plan_version: int = 0
    approved_hash: str | None = None
    outcome: str = "CREATED"
    quota_hit: dict | None = None
    pipeline: DecisionPipeline | None = None
    studio: Studio | None = None
    registry: object = None
    architect_reviewed: bool = True
    architect: Architect | None = None
    replans: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)         # role -> Target chosen during the session (config source 'session')
    active_runs: set = field(default_factory=set)
    unmet: list[str] = field(default_factory=list)
    results: list[verifier.CheckResult] = field(default_factory=list)
    generation: int = 0                                    # how many times this session was reopened (keeps run ids unique)
    setup_task: asyncio.Task | None = None                 # workspace.setup running in the background; verification waits for it
    baseline: object = None                                # BaselineRunner: the checks on the base commit (verifier.classify)
    baseline_task: asyncio.Task | None = None
    baseline_notes: dict = field(default_factory=dict)     # check -> why it already fails on the base commit
    _gate: EnvGate | None = None
    job: int = 0                                           # how many jobs (quick tasks / plans) this conversation has started
    job_kind: str | None = None                            # "quick" | "plan"
    job_base: str | None = None                            # HEAD when the current job started: its checks and review see only its changes
    chat: ChatTriage | None = None
    approval_mode: str = "ask"                             # ask: unknown commands ask you; auto: only EXTERNAL / outside ones do

    @property
    def cwd(self) -> str:
        """Where agents read: the worktree once it exists (that is where the changes are), else the user's checkout (read-only)."""
        return str(self.ws.path) if self.ws is not None else str(self.repo)

    @property
    def triggers(self) -> list[str]:
        """Architect triggers of the current job: quick tasks only get the ones in chat.quick_review."""
        return list(self.cfg.chat.quick_review if self.job_kind == "quick" else self.cfg.review.triggers)

    @property
    def env_gate(self) -> EnvGate:
        if self._gate is None:
            self._gate = EnvGate(self.runtime, self.cfg, self.cwd, self.overrides)
        self._gate.cfg = self.cfg                          # follows settings changed during the session
        return self._gate

    async def close(self) -> None:
        if self.baseline_task is not None and not self.baseline_task.done():
            self.baseline_task.cancel()
        if self.baseline is not None:
            try:
                await self.baseline.close()
            except Exception:
                pass
        await self.runtime.close()
        await self.bus.store.close()           # aiosqlite's worker thread keeps the process alive otherwise

    @property
    def cfg(self):
        return self.loaded.config

    async def emit(self, type_: str, payload: dict | None = None, *, source: str = "harness", task_id=None, run_id=None) -> Event:
        return await self.bus.emit(Event(session_id=self.sid, source=source, type=type_, task_id=task_id, agent_run_id=run_id, payload=payload or {}))

    def sink(self, role: str, task_id: str | None, run_id: str):
        async def f(t: str, p: dict) -> None:
            rid = p.pop("_run", None) if "_run" in p else None          # per-call run id (Main/Architect reuse one sink)
            if t.startswith("live."):                          # token deltas: published live, never persisted
                self.bus.publish_live(Event(session_id=self.sid, source=role, type=t, task_id=task_id, agent_run_id=rid or run_id, payload=p))
                return
            await self.emit(t, {"role": role, **p}, source=role, task_id=task_id, run_id=rid or run_id)
        return f

    def approval_handler(self, role: str, task_id: str | None):
        async def f(a: Approval) -> bool:
            if a.kind == "mcp":                                     # the role's `mcp` config is the grant; logged, never asked
                await self.emit("approval.classified", {"command": a.command, "verdict": "allow", "kind": "mcp"}, source="policy", task_id=task_id)
                return True
            if a.kind == "read":
                verdict = safety.classify_path(a.command or "")
                if verdict == "allow":
                    return True                                     # ordinary reads are bounded by the sandbox: not logged
            elif a.kind == "file_change":
                verdict = "allow"                                   # the sandbox already bounds writes to the worktree
            else:
                auto = self.approval_mode == "auto"
                verdict = safety.classify(a.command or "", a.cwd, Path(self.cwd), auto=auto, local_ok=self.ws is not None)
                if auto and verdict == "allow" and safety.classify(a.command or "", a.cwd, Path(self.cwd)) == "approve":
                    await self.emit("approval.classified", {"command": a.command, "verdict": verdict, "cwd": a.cwd, "auto": True},
                                    source="policy", task_id=task_id)
                    return True
            await self.emit("approval.classified", {"command": a.command, "verdict": verdict, "cwd": a.cwd}, source="policy", task_id=task_id)
            if verdict == "allow":
                return True
            if verdict == "deny":
                return False
            advice = ""
            if verdict == "secret":
                ok, advice = await self._env_gate(a, role, task_id)
                if ok is not None:
                    return ok
            ok = await self.ui.approve_command(a, advice)
            await self.emit("approval.resolved", {"command": a.command, "approved": ok}, source="user", task_id=task_id)
            return ok
        return f

    async def _env_gate(self, a: Approval, role: str, task_id: str | None) -> tuple[bool | None, str]:
        """An env/secret read. (decision, advice): None -> ask the user, with the gatekeeper's reason as advice."""
        mode = self.cfg.approval.env_gate
        if mode == "deny":
            await self.emit("approval.gated", {"command": a.command, "decision": "deny", "reason": "approval.env_gate: deny", "by": "policy"},
                            source="policy", task_id=task_id)
            return False, ""
        if mode == "user":
            return None, "reads env vars or a secret file"
        ts = self.sched.tasks.get(task_id) if task_id else None
        task = {"id": task_id, "title": ts.spec.title, "goal": ts.spec.goal} if ts else None
        g = await self.env_gate.decide(a, role, task)
        await self.record_decision(ControlDecision(type="env_access", engine="debug", selected=g.decision, confidence=None, distribution={}, sharp=None,
                                                   input_snapshot={"role": role, "target": a.command, "reason": g.reason, "model": g.model}), task_id)
        await self.emit("approval.gated", {"command": a.command, "decision": g.decision, "reason": g.reason, "by": "debug", "role": role},
                        source="debug", task_id=task_id)
        if g.decision == "ask_user":
            return None, f"debug gatekeeper: {g.reason}"
        return g.decision == "allow", ""

    async def set_approval_mode(self, mode: str, by: str = "user") -> str:
        if mode not in ("ask", "auto"):
            raise ValueError(f"unknown approval mode {mode!r}: ask | auto")
        self.approval_mode = mode
        await self.emit("approval.mode", {"mode": mode, "by": by}, source="user")
        return ("approvals: AUTO — commands in the worktree run without asking; push, deploy, migrations, sudo and anything outside still ask"
                if mode == "auto" else "approvals: ask — every command off the allow-list asks you")

    async def record_decision(self, d: ControlDecision, task_id: str | None) -> None:
        await self.bus.store.record_decision(self.sid, d, task_id, now())
        await self.emit("decision.made", d.model_dump(exclude={"input_snapshot"}), source=d.engine, task_id=task_id)

    # ---------------- plan phase ----------------
    def _agents(self, main_thread: str | None = None) -> None:
        self.reasoner = Reasoner(self.runtime, self.cfg, self.cwd, self.sink("main", None, "main"), self.approval_handler("main", None), self.overrides,
                                 memory=lambda: render(self.mem), thread_id=main_thread)
        self.architect = Architect(self.runtime, self.cfg, self.cwd, self.sink("architect", None, "architect"), self.approval_handler("architect", None), self.overrides)
        if self.pipeline is not None:
            self.pipeline.main_pick = self.reasoner.pick

    async def plan_phase(self) -> bool:
        if self.studio is not None and self.studio.locked:          # reopened after approval: straight to execution
            return True
        if self.reasoner is None:
            self._agents()
        if self.studio is not None and self.studio.versions:        # reopened while planning: back to the studio, no re-planning
            await self.bus.store.set_status(self.sid, "PLANNING")
            if await self.ui.run_studio(self.studio):
                return True
            await self.emit("plan.abandoned")
            return False
        if not await self.mem.brief():
            await self.mem.set_brief(await self._brief())
        self.start_baseline()                                       # what already fails on the base commit: runs while Main plans
        cmds = [c.command for c in self.cfg.verification.values()]
        if self.baseline_notes:                                     # known before planning: don't plan "fix the test suite"
            cmds.append("(already failing on the base commit, not this task's job — only NEW failures block: "
                        + "; ".join(f"{k}: {v}" for k, v in self.baseline_notes.items()) + ")")
        await self.bus.store.set_status(self.sid, "PLANNING")
        await self.emit("session.planning")
        pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
        qs = await self.reasoner.clarify(pkt, cmds)
        if qs.questions:
            await self.emit("clarify.asked", {"questions": [q.model_dump() for q in qs.questions]})
            answers = await self.ui.answer_questions(qs)
            for q in qs.questions:
                idx, verbatim = answers[q.id]
                d = Decision(id=await self.mem.next_decision_id(), text=f"{q.text} -> {q.options[idx] if idx < len(q.options) else verbatim}",
                             verbatim=verbatim, source="user")
                await self.mem.add_decision(d)
                await self.emit("decision.recorded", d.model_dump(), source="user")
        pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
        plan = await self.reasoner.plan(pkt, cmds, None)
        self.plan_version = 0
        self.studio = Studio(self)
        await self.studio.start(plan)
        self.plan = plan
        await self._review_plan()
        if await self.ui.run_studio(self.studio):
            return True
        await self.emit("plan.abandoned")
        return False

    # ---------------- conversation: each message -> answer | quick job | plan job ----------------
    async def _state(self, forced: str | None) -> dict:
        st: dict = {"worktree": None, "reading": self.cwd, "verification": [c.command for c in self.cfg.verification.values()],
                    "max_quick_files": self.cfg.chat.max_quick_files}
        if self.ws is not None:
            commits = await gitws.git(self.ws.path, "rev-list", "--count", f"{self.ws.session_base}..HEAD", check=False)
            st["worktree"] = {"branch": self.ws.branch, "commits": int(commits or 0)}
        if self.job:
            st["last_job"] = {"job": self.job, "kind": self.job_kind, "goal": self.plan.goal if self.plan else None, "outcome": self.outcome}
        if forced:
            st["user_forced_kind"] = forced
        return st

    def _chat(self) -> ChatTriage:
        if self.chat is None:
            self.chat = ChatTriage(self.runtime, self.cfg, self.cwd, self.sink("chat", None, "chat"), self.approval_handler("chat", None), self.overrides)
        self.chat.cfg, self.chat.cwd = self.cfg, self.cwd
        return self.chat

    async def route(self, text: str, forced: str | None = None) -> Route:
        """Decide how one chat message is handled. Deterministic first (forced kind, chat.route), then the optional cheap
        triage, then ONE Main call (an answer comes back in that same call), then the guard, which only ever escalates."""
        await self.emit("chat.user", {"text": text}, source="user")        # L0 only: chat never enters packets
        known = await self.mem.inputs()
        commit = self.ws.base_commit[:7] if self.ws else (await gitws.git(self.repo, "rev-parse", "--short=7", "HEAD", check=False))
        for i in build_inputs(text, Path(self.cwd), self.home, commit, start=len(known) + 1):
            if not any(k.ref == i.ref for k in known):
                await self.mem.add_input(i)
        if self.reasoner is None:
            self._agents()
        state = await self._state(forced)
        r, by = None, "user" if forced else "main"
        if forced == "plan" or (not forced and self.cfg.chat.route == "plan"):
            r, by = Route(kind="plan", text="Making a plan for this.", brief=text, task=None, reason="/plan" if forced else "chat.route: plan"), by if forced else "rule"
        elif not forced and self.cfg.chat.triage == "cascade" and "chat" in self.cfg.roles and self.cfg.roles["chat"].enabled:
            c = await self._chat().triage(text, state)
            if c.kind == "answer":
                r, by = c, "chat"
            else:
                await self.emit("chat.escalated", {"reason": c.reason}, source="chat")
        if r is None:
            pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
            r = await self.reasoner.route(pkt, text, state)
            if forced and r.kind != forced and forced in ("answer", "quick"):
                if forced == "answer":                                  # /ask: never starts work, whatever Main thought
                    r = r.model_copy(update={"kind": "answer", "task": None})
                elif r.task is not None:
                    r = r.model_copy(update={"kind": "quick"})
        kind, why = rules.route_guard(r.kind, r.task, text, self.cfg.chat)
        if kind != r.kind:
            r, by = r.model_copy(update={"kind": kind, "task": None, "reason": why}), "rule"
        if r.kind != "answer" and not r.brief:
            r = r.model_copy(update={"brief": text})
        await self.record_decision(ControlDecision(type="intent", engine={"chat": "llm"}.get(by, by), selected=r.kind, confidence=None,
                                                   distribution={}, sharp=None, input_snapshot={"text": text[:2000], "reason": r.reason}), None)
        await self.emit("chat.routed", {"kind": r.kind, "by": by, "reason": r.reason, "task": r.task.title if r.task else None}, source=by)
        await self.emit("chat.main", {"kind": "answer" if r.kind == "answer" else "route", "text": r.text}, source="chat" if by == "chat" else "main")
        return r

    async def _recent_chat(self, n: int = 6) -> str:
        evs = [e for e in await self.bus.store.events(self.sid) if e.type in ("chat.user", "chat.main")][-n:]
        return "\n".join(f"- {'user' if e.type == 'chat.user' else 'main'}: {e.payload.get('text', '')[:600]}" for e in evs)

    async def ensure_workspace(self) -> gitws.Workspace:
        """The worktree is made the first time a turn needs to change code. Main then starts a fresh thread there (seeded from
        the curated memory + the last chat turns): a runtime thread cannot move to another directory."""
        if self.ws is not None:
            return self.ws
        self.ws = await gitws.create(self.repo, self.home, self.sid)
        await self.bus.store.db.execute("UPDATE sessions SET base_commit=?, branch=?, worktree=? WHERE id=?",
                                        (self.ws.base_commit, self.ws.branch, str(self.ws.path), self.sid))
        await self.bus.store.db.commit()
        if self.pipeline is not None:
            self.pipeline.repo = self.ws.path
        self._gate = None
        await self.emit("workspace.created", {"branch": self.ws.branch, "worktree": str(self.ws.path), "base": self.ws.base_commit})
        await self.prepare_workspace()
        if self.reasoner is not None:
            had = self.reasoner.thread_id
            self.reasoner.cwd = self.architect.cwd = self.cwd
            if had:
                chat = await self._recent_chat()
                self.reasoner.rotate(await render(self.mem) + (f"\n## Recent chat (context only)\n{chat}\n" if chat else ""))
                await self.emit("main.rotated", {"reason": "workspace created: Main now reads the worktree", "thread_id": None})
        return self.ws

    def _reset_job(self) -> None:
        c = self.cfg.scheduler
        self.sched = Scheduler(c.max_total_agents, c.max_writers, c.max_retries_per_task)
        self.studio, self.plan, self.plan_version, self.approved_hash = None, None, 0, None
        self.unmet, self.results, self.replans, self.quota_hit = [], [], {}, None
        self.architect_reviewed, self.baseline_notes = True, {}
        if self.baseline_task is not None and not self.baseline_task.done():
            self.baseline_task.cancel()
        self.baseline, self.baseline_task = None, None             # the next job's base commit has its own baseline

    async def start_job(self, kind: str, route: Route | None = None, brief: str | None = None) -> None:
        """Start a quick task or a plan on the session branch. Memory, Main's thread and overrides carry over; per-job state does not."""
        await self.ensure_workspace()
        if self.job and await gitws.stash_leftovers(self.ws, f"agent-desk/{self.sid}/j{self.job}-leftovers"):
            await self.emit("workspace.stashed", {"job": self.job, "stash": f"agent-desk/{self.sid}/j{self.job}-leftovers"})
        if self.job:
            self._reset_job()
        self.job += 1
        self.job_kind = kind
        self.job_base = self.ws.base_commit = await gitws.head(self.ws)
        text = brief or (route.brief if route else None) or await self._brief()
        await self.mem.set_brief(text)
        await self.emit("job.started", {"job": self.job, "kind": kind, "brief": text, "base": self.job_base})
        if self.reasoner is None:
            self._agents()
        if kind == "quick":
            await self._lock_quick(route.task, text, route.reason)

    async def _lock_quick(self, task: TaskSpec, brief: str, reason: str) -> None:
        """A quick task is a one-task plan the route approved: no clarify, plan call, before_plan review or studio; execution,
        safety, verification and the completion gate are exactly those of any plan."""
        t = task.model_copy(update={"id": "T1", "depends_on": [], "requires_write": True, "kind": "implementation"})
        plan = Plan(goal=t.goal or brief, constraints=[], acceptance_criteria=t.acceptance_criteria or [brief], tasks=[t], risks=[], out_of_scope=[])
        self.studio = Studio(self)
        v = await self.studio.start(plan, source="quick", note=reason[:200])
        self.studio.locked = True
        await self.lock_plan(v, by="route", rotate=False)
        self.start_baseline()

    async def job_finished(self, outcome: str) -> None:
        if not self.job:
            return
        title = self.plan.goal if self.plan else ""
        commits = (await gitws.git(self.ws.path, "log", "--oneline", f"{self.job_base}..HEAD", check=False)).splitlines() if self.ws and self.job_base else []
        await self.mem.add_note("job", f"J{self.job}", f"{self.job_kind} \"{title[:120]}\" -> {outcome}" + (f" ({len(commits)} commit(s))" if commits else ""), "harness")
        await self.emit("job.finished", {"job": self.job, "kind": self.job_kind, "outcome": outcome})

    # ---------------- in-session control (config source: session) ----------------
    async def switch_role(self, role: str, provider: str, model: str | None = None, tier: str | None = None, account: str | None = None) -> str:
        from .config.schema import Target
        if role not in self.cfg.roles:
            raise ValueError(f"unknown role {role!r}; roles: {', '.join(self.cfg.roles)}")
        if provider not in self.cfg.providers:
            raise ValueError(f"unknown provider {provider!r}; providers: {', '.join(self.cfg.providers)}")
        t = (Target(provider=provider, model=model, account=account) if model else
             Target(provider=provider, tier=tier or self.cfg.roles[role].default_tier or self.cfg.roles[role].tier or "strong", account=account))
        self.cfg._target_tiers(f"role {role}", t)
        if self.cfg.roles[role].access == "write" and "sandbox_write" not in self.registry.get("runtime", self.cfg.providers[provider].runtime).capabilities:
            raise ValueError(f"{provider} cannot enforce a write sandbox for role {role}")
        self.overrides[role] = t
        for k in ("provider", "model", "tier", "account"):
            self.loaded.sources[f"roles.{role}.{k}"] = "session"
        r = resolve(self.cfg, role, overrides=self.overrides)
        text = f"{role} → {r.provider}{'@' + r.account if r.account else ''}·{r.model} (session)"
        await self.emit("role.switched", {"role": role, "provider": r.provider, "model": r.model, "text": text, "source": "session"}, source="user")
        return text

    async def use_fallbacks(self, provider: str, account: str | None = None) -> list[str]:
        """Move every role currently on `provider` (as `account`: the quota belongs to one login) to its configured fallback
        (used after a quota hit)."""
        moved = []
        hit = self.cfg.account_of(provider, account)[0]
        for role, r in self.cfg.roles.items():
            cur = resolve(self.cfg, role, overrides=self.overrides)
            if cur.provider == provider and self.cfg.account_of(cur.provider, cur.account)[0] == hit and r.fallback:
                self.overrides[role] = r.fallback
                moved.append(await self.switch_role_text(role))
        return moved

    async def switch_role_text(self, role: str) -> str:
        r = resolve(self.cfg, role, overrides=self.overrides)
        text = f"{role} → {r.provider}·{r.model} (session, fallback)"
        await self.emit("role.switched", {"role": role, "provider": r.provider, "model": r.model, "text": text, "source": "session"}, source="user")
        return text

    async def switch_engine(self, point: str, engine: str, provider: str | None = None, model: str | None = None) -> str:
        from .config.schema import POINTS
        if point not in POINTS:
            raise ValueError(f"unknown decision point {point!r}; points: {', '.join(POINTS)}")
        if not self.registry.has("decision", engine):
            raise ValueError(f"no decision engine {engine!r}; available: {', '.join(self.registry.names('decision'))}")
        pt = self.cfg.decision.points.get(point)
        if pt is None:
            from .config.schema import Point
            pt = self.cfg.decision.points[point] = Point()
        if engine != "none":
            provider = provider or pt.provider
            if not provider or provider not in self.cfg.providers:
                raise ValueError(f"engine {engine} needs an existing provider (given: {provider})")
            want = "decision" if "provider:decision" in self.registry.get("decision", engine).capabilities else "llm"
            if self.cfg.providers[provider].kind != want:
                raise ValueError(f"engine {engine} needs a provider of kind {want}")
            pt.provider, pt.model = provider, model or pt.model
        pt.engine = engine
        for k in ("engine", "provider", "model"):
            self.loaded.sources[f"decision.points.{point}.{k}"] = "session"
        text = f"decision {point} → {engine}" + (f" · {provider}" if engine != "none" else "") + " (session)"
        await self.emit("engine.switched", {"point": point, "engine": engine, "text": text, "source": "session"}, source="user")
        return text

    async def stop(self) -> None:
        """Stop giving work, interrupt what runs, keep everything on disk. Never rolls back."""
        self.sched.cancel()
        for rid in list(self.active_runs):
            try:
                await self.runtime.cancel(rid)
            except Exception:
                pass
        await self.emit("user.stop", {"text": "/stop"}, source="user")

    # ---------------- workspace: gitignored dependencies the worktree needs ----------------
    async def prepare_workspace(self, run_setup: bool = True, wait: bool = False) -> dict:
        """Link node_modules/.venv/... from the checkout, or run workspace.setup (a real install) when configured."""
        w = self.cfg.workspace
        linked: list[str] = []
        if w.setup and run_setup:
            if self.setup_task is None or self.setup_task.done():
                self.setup_task = asyncio.create_task(self._run_setup(w.setup))
            if wait:
                await self.setup_task
        elif not w.setup:
            linked = await gitws.link_deps(self.ws, w.link)
        info = {"linked": self.ws.linked, "new": linked, "setup": w.setup if run_setup else None}
        await self.emit("workspace.prepared", info)
        return info

    async def _run_setup(self, command: str) -> bool:
        await self.emit("workspace.setup", {"command": command, "status": "running"})
        ok, tail = await gitws.run_setup(self.ws, command)
        await self.emit("workspace.setup", {"command": command, "status": "ok" if ok else "failed", "tail": tail[-600:]})
        return ok

    def _baseline_runner(self):
        if self.baseline is None and self.cfg.verification:
            from .baseline import BaselineRunner
            self.baseline = BaselineRunner(self.repo, self.ws.base_commit, self.home / "baseline", self.home.parent.parent / "baseline",
                                           self.cfg.workspace)
        return self.baseline

    def start_baseline(self) -> None:
        """Run the unscoped checks on the base commit in the background (parallel to planning); cached per commit."""
        if self._baseline_runner() is None or self.baseline_task is not None:
            return
        self.baseline_task = asyncio.create_task(self._precompute_baseline())

    async def _precompute_baseline(self, refresh: bool = False) -> None:
        for name, c in self.cfg.verification.items():
            if "{changed}" in c.command:
                continue                                           # scoped: computed on demand for the files actually changed
            try:
                b = await self.baseline.get(name, c, None, refresh=refresh)
            except Exception as e:
                await self.emit("verification.baseline", {"name": name, "error": f"{type(e).__name__}: {e}"[:200]}, source="verifier")
                continue
            if not b.get("passed"):
                self.baseline_notes[name] = (b.get("summary") or "fails")[:100]
            await self.emit("verification.baseline", {"name": name, "passed": b.get("passed"), "failures": len(b.get("keys") or []),
                                                      "summary": b.get("summary", ""), "command": b.get("command", c.command)}, source="verifier")

    async def _verify(self):
        if self.setup_task is not None and not self.setup_task.done():
            await self.setup_task
        changed = await gitws.changed_since_base(self.ws)
        results = await verifier.run_all(self.cfg.verification, str(self.ws.path), changed)
        for r in results:
            if r.passed:
                continue
            base = None
            if self._baseline_runner() is not None:
                try:
                    base = await self.baseline.get(r.name, self.cfg.verification[r.name], changed)
                except Exception as e:                             # no baseline: the failure blocks, as before
                    await self.emit("verification.baseline", {"name": r.name, "error": f"{type(e).__name__}: {e}"[:200]}, source="verifier")
            verifier.classify(r, base if base and base.get("passed") is not None else None)
        return results

    def _result_payload(self, r) -> dict:
        return {"name": r.name, "passed": r.passed, "required": r.required, "exit_code": r.exit_code, "fingerprint": r.fingerprint,
                "status": r.status or ("pass" if r.passed else "regression"), "new_failures": r.new_failures[:20], "summary": r.summary,
                "failures": len(r.keys), "skip_reason": r.skip_reason, "wrote": r.wrote[:20], "command": r.command}

    async def setup_and_resume(self) -> str:
        """/setup: prepare the worktree (links or workspace.setup), then continue the unfinished tasks."""
        await self.prepare_workspace(run_setup=True, wait=True)
        return await self.resume_after_quota()

    def _commit_message(self, tid: str, title: str) -> str:
        """workspace.commit_message; the title starts lowercase so conventional-commit linters accept it."""
        return self.cfg.workspace.commit_message.format(title=title[:1].lower() + title[1:], task=tid, session=self.sid)

    async def resume_after_quota(self) -> str:
        """Continue unfinished work: after a quota pause, a stop, a failure, or when the session was reopened."""
        if not self.cfg.workspace.setup:
            await gitws.link_deps(self.ws, self.cfg.workspace.link)      # sessions from before a link was added get it now
        self.quota_hit = None
        self.sched.cancelled = False
        self.unmet.clear()
        for t in self.sched.tasks.values():
            if t.status == FAILED:                                 # a human chose to continue: fresh budget for what failed
                self.sched.reset(t.spec.id)
        await self.emit("session.resumed", {})
        await self.execute()
        return await self.finalize()

    # ---------------- reopening a session ----------------
    async def restore(self, events: list[Event]) -> None:
        """Rebuild in-memory state from the persisted events (the canonical history). Memory (L2) is already in SQLite."""
        self.generation = 1 + sum(e.type == "session.reopened" for e in events)
        self.approval_mode = next((e.payload.get("mode", "ask") for e in reversed(events) if e.type == "approval.mode"), "ask")
        main_thread = None
        for e in events:
            if e.type == "main.thread":
                main_thread = e.payload.get("thread_id")
            elif e.type == "main.rotated":
                main_thread = None
        last = next((e.payload for e in reversed(events) if e.type == "main.thread"), {})
        main = resolve(self.cfg, "main")
        main_prov, main_acc = main.provider, self.cfg.account_of(main.provider, main.account)[0]
        if main_thread and (main_prov != last.get("provider") or main_acc != last.get("account")):   # no recorded account: owner unknown, start fresh
            main_thread = None                                     # Main moved to another provider/account: its old thread cannot be continued
        self._agents(main_thread)
        if last.get("cwd") and main_thread and last["cwd"] != self.cwd:
            main_thread = self.reasoner.thread_id = None            # Main read another directory then (before the worktree existed)
        if main_thread:
            self.reasoner.thread_owner = self.reasoner.owner(main_prov, main.account)
        self.reasoner._n = sum(e.type == "agent.started" and (e.agent_run_id or "").startswith("main-") for e in events)
        self.architect._n = sum(e.type == "agent.started" and (e.agent_run_id or "").startswith("architect-") for e in events)
        starts = [i for i, e in enumerate(events) if e.type == "job.started"]
        if starts:                                                 # only the last job is live; earlier ones are history (memory has their notes)
            e = events[starts[-1]]
            self.job, self.job_kind, self.job_base = e.payload["job"], e.payload["kind"], e.payload.get("base")
            events = events[starts[-1]:]
            if self.ws is not None and self.job_base:
                self.ws.base_commit = self.job_base
        elif any(e.type == "plan.version" for e in events):
            self.job, self.job_kind = 1, "plan"                    # a session from before jobs: one plan
        if not any(e.type == "plan.version" for e in events):
            return                                                 # died before the first plan: plan_phase starts over
        self.studio = Studio(self)
        answered = {e.payload.get("question") for e in events if e.type == "decision.recorded"}
        for e in events:
            p = e.payload
            if e.type == "plan.version":
                plan = Plan.model_validate(p["plan"])
                self.studio.versions.append(PlanVersion(p["version"], plan, p["hash"], p.get("source", "main"), p.get("note", "")))
            elif e.type == "question.opened":
                self.studio.questions[p["id"]] = Question(p["id"], p["text"], p["options"], p.get("evidence", ""), answered=p["id"] in answered)
            elif e.type in ("plan.approved", "plan.applied"):
                v = next(v for v in self.studio.versions if v.n == p["version"])
                self.plan, self.plan_version, self.approved_hash = v.plan, v.n, v.hash
                self.sched.load(v.plan.tasks)
                if e.type == "plan.applied" and p.get("material"):
                    self.sched.mark_stale(p.get("changed_tasks") or [])
                self.studio.locked = self.sched.plan_locked = True
            elif e.type == "task.done" and e.task_id in self.sched.tasks:
                ts = self.sched.tasks[e.task_id]
                ts.status, ts.commit, ts.last_summary = DONE, p.get("commit"), p.get("summary", "")
            elif e.type == "plan.abandoned":
                self.studio.versions.clear()
        if self.plan is None and self.studio.versions:
            self.plan = self.studio.current.plan
        reviewed = False
        for e in events:                                          # last word wins: a material user edit invalidates an earlier review
            if e.type == "architect.reviewed" and e.payload.get("trigger") == "before_plan" or \
                    e.type == "architect.skipped" and e.payload.get("by") == "user":
                reviewed = True
            elif e.type == "architect.invalidated":
                reviewed = False
        self.architect_reviewed = reviewed or "before_plan" not in self.triggers
        for t in self.sched.tasks.values():
            if t.status not in (DONE, STALE) and t.status != "dropped":
                t.status = PENDING

    async def lock_plan(self, v: PlanVersion, by: str = "user", rotate: bool = True) -> None:
        """The only way work can start: freezes the approved plan by hash; Scheduler refuses everything before this."""
        self.plan, self.plan_version, self.approved_hash = v.plan, v.n, v.hash
        self.sched.load(v.plan.tasks)
        self.sched.plan_locked = True
        await self.emit("plan.approved", {"version": v.n, "hash": v.hash, "by": by}, source="user" if by == "user" else "harness")
        if rotate:
            self.reasoner.rotate(await render(self.mem))         # curated memory replaces the planning chatter

    async def apply_plan(self, v: PlanVersion, diff: PlanDiff) -> None:
        """A plan change after approval. Material changes were accepted by the user; non-material ones are auto-acknowledged."""
        changed = [c.id for c in diff.changes if c.id in self.sched.tasks]
        self.plan, self.plan_version, self.approved_hash = v.plan, v.n, v.hash
        self.sched.load(v.plan.tasks)
        self.sched.mark_stale([c.id for c in diff.changes if c.material and c.id in self.sched.tasks and c.op in ("modify", "remove")])
        await self.emit("plan.applied", {"version": v.n, "hash": v.hash, "material": diff.material, "changed_tasks": changed,
                                         "acknowledged": "user" if diff.material else "auto"}, source="user" if diff.material else "harness")
        self.reasoner.rotate(await render(self.mem))

    async def user_message(self, text: str, item: str | None = None):
        """A message while running. Main answers, proposes, or stops; non-material proposals apply automatically."""
        reply = await self.studio.ask(text, item)
        if reply.kind == "stop":
            self.sched.cancel()
        elif reply.kind == "proposal" and not self.studio.pending.diff.material:
            await self.studio.accept()
        return reply

    # ---------------- architect ----------------
    async def _architect(self, trigger: str, **kw):
        if trigger not in self.triggers:
            return None
        pkt = await builder.build(self.mem, "architect", None, max_tokens=self.cfg.context.packet_max_tokens)
        plan = self.studio.current.plan if (trigger == "before_plan" or not self.studio.locked) else self.plan
        out = await self.architect.review(trigger, build_prompt(trigger, pkt, plan, **kw))
        await self.emit("architect.reviewed", {"trigger": trigger, "verdict": out.review.verdict if out.review else None,
                                               "findings": [f.model_dump() for f in out.review.findings] if out.review else [],
                                               "via_fallback": out.via_fallback, "error": out.error, "quota": out.quota}, source="architect")
        if out.review is None:
            await self.emit("architect.unavailable", {"trigger": trigger, "error": out.error, "quota": out.quota}, source="architect")
        return out

    async def _review_plan(self) -> None:
        if "before_plan" not in self.triggers:
            return
        self.architect_reviewed = False
        for _ in range(self.cfg.review.max_rounds):
            out = await self._architect("before_plan")
            if out is None or out.review is None:
                return                                           # unavailable: stays unreviewed; the user may skip explicitly
            if out.review.verdict == "approve" and not out.review.findings:
                break
            pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
            res = await self.studio.integrate_review("before_plan", out.review, await self.reasoner.triage(pkt, self.studio.current.plan, out.review))
            self.plan = self.studio.current.plan
            if res["version"] is None:
                break                                            # nothing changed, a re-review would say the same
        self.architect_reviewed = True

    async def _apply_external(self, plan: Plan, text: str) -> bool:
        prop = await self.studio.propose_external(plan, text, "architect")
        if prop is None:
            return True
        if prop.diff.material and not await self.ui.resolve_proposal(self.studio):
            await self.studio.reject()
            return False
        await self.studio.accept()                                # locked -> apply_plan: new hash, stale/dropped bookkeeping
        return True

    async def _escalate(self, tid: str, summary: str) -> bool:
        """Repeated failure: independent review -> Main revises the plan -> fresh budget. At most max_replans_per_task times."""
        if "error_repeats" not in self.triggers or self.replans.get(tid, 0) >= self.cfg.review.max_replans_per_task:
            return False
        self.replans[tid] = self.replans.get(tid, 0) + 1
        ts = self.sched.tasks[tid]
        diff = await gitws.diff(self.ws)
        out = await self._architect("error_repeats", diff=diff, task_title=ts.spec.title, failures=ts.tail[-self.cfg.context.failure_tail_lines:] or [summary])
        if out is None or out.review is None:
            return False
        for f in out.review.findings:
            await self.mem.add_note("finding", f.id, f"[{f.severity}] {f.file}: {f.message}", "error_repeats")
        advice = "\n".join([f"- {a}" for a in out.review.advice] + [f"- {f.message}" for f in out.review.findings])
        pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
        done = [t.spec.id for t in self.sched.tasks.values() if t.status == DONE]
        new = await self.reasoner.revise(pkt, self.plan, done, advice or summary)
        if not await self._apply_external(new, f"replan after repeated failure of {tid}"):
            return False
        if tid in self.sched.tasks and self.sched.tasks[tid].status != "dropped":
            self.sched.reset(tid)
        await self.emit("task.replanned", {"advice": advice[:400]}, task_id=tid)
        return True

    async def _final_review(self) -> None:
        """Last trigger: critical findings block completion; the rest become follow-ups."""
        if "before_done" not in self.triggers or self.unmet or not self.sched.all_done():
            return
        for rnd in range(self.cfg.review.max_rounds):
            diff = await gitws.diff_since_base(self.ws)
            if not diff.strip():                                 # read-only work: there is nothing to review
                await self.emit("architect.skipped", {"trigger": "before_done", "reason": "no changes"}, source="architect")
                return
            results = await self._verify()
            ver = "\n".join(f"- {r.name}: " + ("pass" if r.passed else r.status or "FAIL")
                            + (f" (already failing on base, {len(r.keys)} failures, none new)" if r.status == "pre-existing" else "")
                            for r in results)
            out = await self._architect("before_done", diff=diff, verification=ver)
            if out is None or out.review is None:
                return                                           # reviewer unavailable: recorded, gate still enforces everything else
            crit = [f for f in out.review.findings if f.severity == "critical"]
            for f in out.review.findings:
                if f.severity != "critical":
                    await self.mem.add_note("followup", f.id, f"[{f.severity}] {f.file}: {f.message}", "before_done")
            if not crit:
                return
            pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
            tri = await self.reasoner.triage(pkt, self.plan, out.review)
            res = await self.studio.integrate_review("before_done", out.review, tri)
            if res["questions"]:
                self.unmet.append("critical finding needs a user decision: " + ", ".join(res["questions"]))
                return
            fixed = tri.plan is not None and any(i.verdict == "fixed" for i in tri.items)
            if not fixed or not await self._apply_external(tri.plan, "fix for critical review finding"):
                self.unmet.append("unresolved critical architect finding: " + "; ".join(f.message[:120] for f in crit))
                return
            self.sched.cancelled = False
            await self.execute()                                 # run the new/stale tasks, then review again
            if not self.sched.all_done() or self.unmet:
                return
        self.unmet.append("critical architect finding still open after review rounds")

    async def _brief(self) -> str:
        r = await self.bus.store.db.execute("SELECT title FROM sessions WHERE id=?", (self.sid,))
        return (await r.fetchone())["title"]

    # ---------------- execution ----------------
    async def execute(self) -> None:
        await self.bus.store.set_status(self.sid, "RUNNING")
        running: dict[asyncio.Task, str] = {}
        while not self.sched.cancelled:
            for ts in self.sched.ready():
                self.sched.start(ts.spec.id)
                running[asyncio.create_task(self._run_task(ts.spec.id))] = ts.spec.id
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                running.pop(d)
                if exc := d.exception():
                    await self.emit("error", {"message": repr(exc)})
                    self.unmet.append(f"internal error: {exc!r}")
                    self.sched.cancel()
        for t in list(running):
            t.cancel()

    async def _run_task(self, tid: str) -> None:
        ts = self.sched.tasks[tid]
        spec = ts.spec
        rv = await self.pipeline.route(spec, tid)
        role_name = rv.choice
        role_tiers = self.cfg.roles[role_name].tiers
        tv = await self.pipeline.tier(spec, role_tiers, tid)
        tier = tv.choice if tv else None
        pkt = await builder.build(self.mem, role_name, spec, max_tokens=self.cfg.context.packet_max_tokens,
                                  previous_attempt=ts.last_summary or None, failure_tail=ts.tail,
                                  tail_lines=self.cfg.context.failure_tail_lines)
        run_id = f"{role_name}-{tid}-a{ts.attempts}" + (f"-j{self.job}" if self.job > 1 else "") + (f"-r{self.generation}" if self.generation else "")
        deploying = spec.kind == "deployment"
        prompt = render_packet(pkt) + "\n\n---\n" + (
            "Carry out these deploy steps in the current directory with the repository's own deploy tooling. Every push or deploy command is "
            "approved by the user first; never force-push. Stop on a rejected push or an unexpected conflict. List each command that changed a "
            "branch or a remote, and each pushed ref with its commit, as evidence of kind 'command'. Reply with the report JSON."
            if deploying else
            "Implement this task in the current directory. Run only the checks relevant to it. Do not commit or push. Reply with the report JSON."
            if spec.requires_write else "Investigate and report. List the files that matter as evidence of kind 'file' with ref 'path:startLine-endLine'. Do not modify anything.")
        roots = [await gitws.common_dir(self.ws)] if deploying else []      # merges/pushes update refs in the repo's git dir
        self.active_runs.add(run_id)
        res = await self.runtime.run(
            RunSpec(run_id, resolve(self.cfg, role_name, tier, overrides=self.overrides), str(self.ws.path), prompt, AgentReport.model_json_schema(),
                    schema_name="AgentReport", writable_roots=roots),
            labelled(self.sink(role_name, tid, run_id), run_id, spec.title), self.approval_handler(role_name, tid))
        self.active_runs.discard(run_id)
        if res.status == "quota":                                  # a state, not a failure: don't burn a retry
            ts.attempts -= 1
            ts.status = PENDING
            self.quota_hit = {"provider": self.cfg.roles[role_name].provider if not res.reset else res.reset, "reset": res.reset, "task": tid}
            await self.emit("quota.hit", {"role": role_name, "provider": resolve(self.cfg, role_name).provider, "reset": res.reset}, task_id=tid)
            self.sched.cancel()
            return
        if self.sched.cancelled:
            return
        report: AgentReport | None = None
        if res.status == "completed":
            try:
                report = AgentReport.model_validate_json(res.final_text)
            except Exception:
                report = None
        ok = report is not None and report.status == "completed"
        summary = (report.summary if report else (res.error or "invalid report"))
        if report and report.status == "escalate":
            await self.emit("task.escalated", {"reason": report.escalate.reason if report.escalate else summary}, task_id=tid)
        fp = None
        commit = None
        if spec.requires_write and not deploying and report is not None:
            # Evidence beats self-report: verify whenever a writer produced a report, even a "blocked" one.
            changed = await gitws.changed_files(self.ws)
            await self.mem.mark_stale(changed)
            self.results = await self._verify() if changed else []
            for r in self.results:
                await self.emit("verification.result", self._result_payload(r), source="verifier", task_id=tid)
            bad = verifier.required_failed(self.results)
            if bad:
                ok, fp = False, bad[0].fingerprint
                ts.tail = bad[0].tail                              # failure evidence for the retry packet and the decision rules
                summary = f"{summary} | verification failed: {bad[0].name}"
            elif ok and not changed:
                ok, summary, fp = False, "implementation task produced no diff", "nodiff"
            elif ok and (missing := missing_outputs(spec.outputs, changed, self.ws.path)):
                ts.tail = [f"missing outputs: {', '.join(missing)} (the task promised these files; they are not in the diff)"]
                ok, fp = False, verifier.fingerprint("outputs", missing)
                summary = f"{summary} | {ts.tail[0]}"
            elif ok:
                try:
                    commit = await gitws.commit_all(self.ws, self._commit_message(tid, spec.title))
                except gitws.GitError as e:                    # the repo's hooks (lint-staged, commitlint) said no: the worker fixes it
                    ts.tail = str(e).splitlines()[-25:]
                    ok, fp = False, verifier.fingerprint("hook", ts.tail)
                    summary = f"{summary} | commit hook failed: {ts.tail[-1][:200] if ts.tail else e}"
        elif ok and report:
            for ev in report.evidence:
                if ev.kind == "file" and ":" in ev.ref:
                    path, lines = ev.ref.rsplit(":", 1)
                    await self.mem.add_file(FileRef(path=path, lines=lines, why=ev.note, commit=self.ws.base_commit[:7]), run_id)
        if not ok and not fp:
            fp = verifier.fingerprint("agent", [summary])
        if not ok:
            v = await self.pipeline.retry_or_stop({"task": spec.title, "attempt": ts.attempts, "max_attempts": self.cfg.scheduler.max_retries_per_task,
                                                   "fingerprints": ts.fingerprints + ([fp] if fp else []), "failure_tail": ts.tail[-25:], "summary": summary}, tid)
            if v.choice == "stop":
                self.sched.stop(tid, summary)
                await self.emit("task.stop", {"reason": summary[:300], "decided_by": f"{v.engine}: {v.reason}", "engine": v.engine}, task_id=tid)
                self.unmet.append(f"{tid} needs a human, not a retry: {summary[:200]} (stopped by {v.engine}: {v.reason})")
                self.sched.cancel()
                return
        action = self.sched.finish(tid, ok, fp, summary, commit)
        await self.emit(f"task.{action}", {"summary": summary[:300], "commit": commit, "fingerprint": fp}, task_id=tid)
        if action == "escalate":
            if await self._escalate(tid, summary):
                return
            self.unmet.append(f"{tid} failed after {ts.attempts} attempt(s): {summary[:200]}")
            self.sched.cancel()

    # ---------------- completion ----------------
    async def completion_gate(self) -> list[str]:
        unmet = list(self.unmet)
        if self.sched.cancelled and not unmet:
            unmet.append("session cancelled")
        if not self.sched.all_done():
            unmet.append("required tasks not all done: " + ", ".join(f"{t.spec.id}={t.status}" for t in self.sched.tasks.values() if t.status != DONE))
        for t in self.sched.tasks.values():
            if t.spec.requires_write and t.spec.kind != "deployment" and t.status == DONE and not t.commit:
                unmet.append(f"{t.spec.id} is an implementation task with no diff")
        for t in self.sched.tasks.values():
            if t.status == DONE and t.spec.kind != "deployment":
                unmet += [f"{t.spec.id} promised {f} but it is missing" for f in t.spec.outputs if not any(self.ws.path.glob(f))]
        if self.plan is None or plan_hash(self.plan) != self.approved_hash:
            unmet.append("running plan differs from the approved plan")
        if not unmet and any(t.spec.requires_write and t.spec.kind != "deployment" for t in self.sched.tasks.values()):   # nothing was written: nothing to verify
            self.results = await self._verify()
            for r in self.results:
                await self.emit("verification.result", self._result_payload(r), source="verifier")
            for r in verifier.required_failed(self.results):
                unmet.append(f"required check failed: {r.name} — new failures: " + ", ".join(r.new_failures[:3] or [r.summary]))
        return unmet

    async def finalize(self) -> str:
        if self.quota_hit:
            self.outcome = "WAITING_QUOTA"
            await self.bus.store.set_status(self.sid, self.outcome)
            await self.emit("session.waiting_quota", self.quota_hit)
            return self.outcome
        await self._final_review()
        unmet = await self.completion_gate()
        await self.emit("completion.gate", {"unmet": unmet}, source="verifier")
        if unmet:
            self.outcome = "FAILED" if not self.sched.cancelled or self.unmet else "CANCELLED"
            await self.bus.store.set_status(self.sid, self.outcome)
            return self.outcome
        facts = "\n".join(f"- {r.name}: {r.status or ('pass' if r.passed else 'FAIL')} (`{r.command}`)" for r in self.results) or "- no checks configured"
        facts += "\n" + "\n".join(f"- {t.spec.id} {t.spec.title}: commit {t.commit}" for t in self.sched.tasks.values() if t.commit)
        try:
            if self.job_kind == "quick" and self.cfg.chat.quick_summary == "report":     # no Main call: the worker's report + the checks
                t = next(iter(self.sched.tasks.values()))
                await self.emit("final.summary", {"text": t.last_summary or t.spec.title, "ac_evidence": [f"{a}: " + ", ".join(
                    f"{r.name} {'pass' if r.passed else r.status or 'FAIL'}" for r in self.results) for a in t.spec.acceptance_criteria[:5]]},
                    source="harness")
            else:
                pkt = await builder.build(self.mem, "main", None, max_tokens=self.cfg.context.packet_max_tokens)
                s = await self.reasoner.summarize(pkt, facts)
                await self.emit("final.summary", s.model_dump(), source="main")
        except ReasonerError as e:
            await self.emit("final.summary", {"text": f"(summary unavailable: {e})", "ac_evidence": []})
        (self.home / "session.md").write_text(await render(self.mem))
        self.outcome = "COMPLETED"
        await self.bus.store.set_status(self.sid, self.outcome)
        return self.outcome
