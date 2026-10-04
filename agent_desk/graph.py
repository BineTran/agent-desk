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


async def open_session(repo: Path, task: str, loaded: Loaded, runtime: AgentRuntime, ui: UI, home_root: Path, registry: Registry | None = None) -> Session:
    sid = "s-" + uuid.uuid4().hex[:4]
    home = home_root / "sessions" / sid
    home.mkdir(parents=True, exist_ok=True)
    store = await EventStore(home_root / "db" / "agent-desk.sqlite").open()
    ws = await gitws.create(repo, home, sid)
    await store.create_session(sid, str(repo), task, loaded.snapshot(), loaded.snapshot_hash(), now())
    await store.db.execute("UPDATE sessions SET base_commit=?, branch=?, worktree=? WHERE id=?", (ws.base_commit, ws.branch, str(ws.path), sid))
    await store.db.commit()
    mem = Memory(store, sid)
    for i in build_inputs(task, repo, home, ws.base_commit[:7]):
        await mem.add_input(i)
    c = loaded.config.scheduler
    s = Session(sid, repo, home, loaded, EventBus(store), runtime, ui, mem, ws, Scheduler(c.max_total_agents, c.max_writers, c.max_retries_per_task))
    import os
    reg = registry or default_registry(loaded.config.plugins)
    ctx = EngineCtx(loaded.config, runtime if hasattr(runtime, "runtime") else None, dict(os.environ))
    s.registry = reg
    s.pipeline = DecisionPipeline(loaded.config, reg, ctx, s.record_decision, None, ws.path)
    await s.emit("session.created", {"repo": str(repo), "branch": ws.branch, "worktree": str(ws.path), "config_hash": loaded.snapshot_hash()})
    await s.prepare_workspace()                    # node_modules/.venv are gitignored: the fresh worktree has none
    return s


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
    if not row["worktree"] or not wt.exists():
        await store.close()
        raise ReopenError(f"the worktree of {sid} is gone ({wt}); its branch {row['branch']} may still exist in the repo")
    ws = gitws.Workspace(Path(row["repo"]), wt, row["branch"], row["base_commit"])
    c = loaded.config.scheduler
    s = Session(sid, Path(row["repo"]), home_root / "sessions" / sid, loaded, EventBus(store), runtime, ui, Memory(store, sid), ws,
                Scheduler(c.max_total_agents, c.max_writers, c.max_retries_per_task))
    import os
    reg = registry or default_registry(loaded.config.plugins)
    s.registry = reg
    s.pipeline = DecisionPipeline(loaded.config, reg, EngineCtx(loaded.config, runtime if hasattr(runtime, "runtime") else None, dict(os.environ)),
                                  s.record_decision, None, ws.path)
    events = await store.events(sid)
    s.outcome = "INTERRUPTED" if row["status"] in ("RUNNING", "PLANNING", "CREATED") else row["status"]
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
        return {"outcome": await s.finalize()}

    async def abandoned(_: State) -> State:
        await s.bus.store.set_status(s.sid, "CANCELLED")
        return {"outcome": "CANCELLED"}

    g = StateGraph(State)
    g.add_node("plan", plan); g.add_node("execute", execute); g.add_node("finalize", finalize); g.add_node("abandoned", abandoned)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda st: "execute" if st.get("approved") else "abandoned", {"execute": "execute", "abandoned": "abandoned"})
    g.add_edge("execute", "finalize"); g.add_edge("finalize", END); g.add_edge("abandoned", END)
    async with AsyncSqliteSaver.from_conn_string(str(s.home / "checkpoints.sqlite")) as cp:
        out = await g.compile(checkpointer=cp).ainvoke({"sid": s.sid}, config={"configurable": {"thread_id": s.sid}})
    return out["outcome"]
