"""LangGraph wiring: durable top-level topology. Per-task scheduling lives inside the execute node."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from . import gitws
from .config.loader import Loaded
from .context.inputs import build_inputs
from .context.memory import Memory
from .contracts import now
from .decision.engine import DecisionPipeline
from .decision.engines import EngineCtx
from .plugins import Registry, default_registry
from .events.bus import EventBus
from .events.store import EventStore
from .orchestrator import Session
from .runtime.base import AgentRuntime
from .scheduler import Scheduler
from .ui import UI


class State(TypedDict, total=False):
    sid: str
    approved: bool
    outcome: str


async def open_chat(repo: Path, title: str, loaded: Loaded, runtime: AgentRuntime, ui: UI, home_root: Path, registry: Registry | None = None) -> Session:
    """A conversation in this repo. Cheap: no worktree, branch or setup until a turn needs to change code (Session.ensure_workspace)."""
    repo = repo.resolve()
    if await gitws.git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise gitws.GitError(f"{repo} is not a git repository")
    sid = "s-" + uuid.uuid4().hex[:4]
    home = home_root / "sessions" / sid
    home.mkdir(parents=True, exist_ok=True)
    store = await EventStore(home_root / "db" / "agent-desk.sqlite").open()
    await store.create_session(sid, str(repo), title, loaded.snapshot(), loaded.snapshot_hash(), now())
    mem = Memory(store, sid)
    head = await gitws.git(repo, "rev-parse", "HEAD")
    for i in build_inputs(title, repo, home, head[:7]):
        await mem.add_input(i)
    c = loaded.config.scheduler
    s = Session(sid, repo, home, loaded, EventBus(store), runtime, ui, mem, None, Scheduler(c.max_total_agents, c.max_writers, c.max_retries_per_task))
    import os
    reg = registry or default_registry(loaded.config.plugins)
    ctx = EngineCtx(loaded.config, runtime if hasattr(runtime, "runtime") else None, dict(os.environ))
    s.registry = reg
    s.pipeline = DecisionPipeline(loaded.config, reg, ctx, s.record_decision, None, repo)
    await s.emit("session.created", {"repo": str(repo), "branch": None, "worktree": None, "config_hash": loaded.snapshot_hash()})
    await store.set_status(sid, "IDLE")
    return s


async def open_session(repo: Path, task: str, loaded: Loaded, runtime: AgentRuntime, ui: UI, home_root: Path, registry: Registry | None = None) -> Session:
    """A session that goes straight into the full plan flow for `task` (headless default, tests)."""
    s = await open_chat(repo, task, loaded, runtime, ui, home_root, registry)
    await s.start_job("plan", brief=task)
    return s


async def run_turn(s: Session, text: str, forced: str | None = None) -> str:
    """One chat message: "ANSWERED", or the outcome of the quick task / plan it started."""
    r = await s.route(text, forced)
    if r.kind == "answer":
        return "ANSWERED"
    await s.start_job(r.kind, r)
    return await run_graph(s)


class ReopenError(Exception):
    pass


async def load_session(sid: str, loaded: Loaded, runtime: AgentRuntime, ui: UI, home_root: Path, registry: Registry | None = None) -> Session:
    """Reopen a persisted session: same id, worktree, branch, memory and (when the provider still has it) Main thread."""
    store = await EventStore(home_root / "db" / "agent-desk.sqlite").open()
    cur = await store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,))
    row = await cur.fetchone()
    if row is None:
        await store.close()
        raise ReopenError(f"no session {sid}")
    wt = Path(row["worktree"] or "")
    if row["worktree"] and not wt.exists():
        await store.close()
        raise ReopenError(f"the worktree of {sid} is gone ({wt}); its branch {row['branch']} may still exist in the repo")
    ws = gitws.Workspace(Path(row["repo"]), wt, row["branch"], row["base_commit"]) if row["worktree"] else None
    c = loaded.config.scheduler
    s = Session(sid, Path(row["repo"]), home_root / "sessions" / sid, loaded, EventBus(store), runtime, ui, Memory(store, sid), ws,
                Scheduler(c.max_total_agents, c.max_writers, c.max_retries_per_task))
    import os
    reg = registry or default_registry(loaded.config.plugins)
    s.registry = reg
    s.pipeline = DecisionPipeline(loaded.config, reg, EngineCtx(loaded.config, runtime if hasattr(runtime, "runtime") else None, dict(os.environ)),
                                  s.record_decision, None, ws.path if ws else Path(row["repo"]))
    events = await store.events(sid)
    s.outcome = "INTERRUPTED" if row["status"] in ("RUNNING", "PLANNING", "CREATED") else row["status"]
    if row["status"] == "IDLE" or (row["status"] == "CREATED" and ws is None):
        s.outcome = "IDLE"                                         # only chat so far: nothing was interrupted
    await s.restore(events)
    if s.outcome != row["status"]:
        await store.set_status(sid, s.outcome)                 # the process that ran it died without saying so
    await s.emit("session.reopened", {"status": s.outcome, "generation": s.generation, "main_thread": s.reasoner.thread_id,
                                      "plan_version": s.plan_version or None})
    return s


async def run_graph(s: Session) -> str:
    async def plan(_: State) -> State:
        return {"approved": await s.plan_phase()}

    async def execute(_: State) -> State:
        await s.execute()
        return {}

    async def finalize(_: State) -> State:
        out = await s.finalize()
        await s.job_finished(out)
        return {"outcome": out}

    async def abandoned(_: State) -> State:
        await s.bus.store.set_status(s.sid, "CANCELLED")
        s.outcome = "CANCELLED"
        await s.job_finished("CANCELLED")
        return {"outcome": "CANCELLED"}

    g = StateGraph(State)
    g.add_node("plan", plan); g.add_node("execute", execute); g.add_node("finalize", finalize); g.add_node("abandoned", abandoned)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda st: "execute" if st.get("approved") else "abandoned", {"execute": "execute", "abandoned": "abandoned"})
    g.add_edge("execute", "finalize"); g.add_edge("finalize", END); g.add_edge("abandoned", END)
    async with AsyncSqliteSaver.from_conn_string(str(s.home / "checkpoints.sqlite")) as cp:
        out = await g.compile(checkpointer=cp).ainvoke({"sid": s.sid}, config={"configurable": {"thread_id": f"{s.sid}/j{s.job}" if s.job > 1 else s.sid}})
    return out["outcome"]
