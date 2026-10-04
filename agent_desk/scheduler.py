"""Deterministic scheduling: DAG readiness, slots, single writer, retry budget, cancel."""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import TaskSpec

PENDING, QUEUED, RUNNING, DONE, FAILED, STALE, DROPPED = "pending", "queued", "running", "done", "failed", "stale", "dropped"


@dataclass
class TaskState:
    spec: TaskSpec
    status: str = PENDING
    attempts: int = 0
    fingerprints: list[str] = field(default_factory=list)
    last_summary: str = ""
    commit: str | None = None
    tail: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(self, max_agents: int = 6, max_writers: int = 1, max_retries: int = 2):
        self.max_agents, self.max_writers, self.max_retries = max_agents, max_writers, max_retries
        self.tasks: dict[str, TaskState] = {}
        self.plan_locked = False
        self.cancelled = False

    def load(self, specs: list[TaskSpec]) -> None:
        old = self.tasks
        self.tasks = {t.id: old.get(t.id) or TaskState(t) for t in specs}
        for t in specs:
            self.tasks[t.id].spec = t
        for tid, st in old.items():                    # tasks removed from the plan stay visible, marked dropped
            if tid not in self.tasks:
                st.status = DROPPED
                self.tasks[tid] = st

    def mark_stale(self, ids: list[str]) -> None:
        for i in ids:
            if i in self.tasks and self.tasks[i].status in (DONE, FAILED):
                self.tasks[i].status = STALE

    def running(self) -> list[TaskState]:
        return [t for t in self.tasks.values() if t.status == RUNNING]

    def ready(self) -> list[TaskState]:
        """Tasks whose deps are done. Writers are limited; nothing runs before the plan is locked or after cancel."""
        if not self.plan_locked or self.cancelled:
            return []
        run = self.running()
        slots = self.max_agents - len(run)
        writers = self.max_writers - sum(t.spec.requires_write for t in run)
        out = []
        for t in self.tasks.values():
            if t.status not in (PENDING, QUEUED, STALE) or slots <= 0:
                continue
            if any(self.tasks[d].status != DONE for d in t.spec.depends_on if d in self.tasks):
                continue
            if t.spec.requires_write:
                if writers <= 0:
                    t.status = QUEUED
                    continue
                writers -= 1
            slots -= 1
            out.append(t)
        return out

    def start(self, tid: str) -> None:
        t = self.tasks[tid]
        t.status, t.attempts = RUNNING, t.attempts + 1

    def finish(self, tid: str, ok: bool, fingerprint: str | None = None, summary: str = "", commit: str | None = None) -> str:
        """Returns next action: done | retry | escalate."""
        t = self.tasks[tid]
        t.last_summary = summary
        if ok:
            t.status, t.commit = DONE, commit
            return "done"
        if fingerprint:
            t.fingerprints.append(fingerprint)
        t.status = FAILED
        repeated = len(t.fingerprints) >= 2 and t.fingerprints[-1] == t.fingerprints[-2]
        if repeated or t.attempts > self.max_retries:
            return "escalate"
        t.status = PENDING
        return "retry"

    def reset(self, tid: str) -> None:
        """A new approach was agreed (replan): fresh retry budget, same task."""
        t = self.tasks[tid]
        t.status, t.attempts, t.fingerprints, t.tail = PENDING, 0, [], []

    def stop(self, tid: str, summary: str = "") -> None:
        """A human is needed (e.g. environment error): no retry, no escalation to an agent."""
        t = self.tasks[tid]
        t.status, t.last_summary = FAILED, summary

    def all_done(self) -> bool:
        live = [t for t in self.tasks.values() if t.status != DROPPED]
        return all(t.status == DONE for t in live)          # an answer-only plan (no tasks) is trivially done

    def cancel(self) -> None:
        self.cancelled = True
