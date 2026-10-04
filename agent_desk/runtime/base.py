from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from ..roles import ResolvedRole

Status = Literal["completed", "failed", "interrupted", "quota"]
# Sent when a role's limits trip: one last turn on the same thread, no more tools, answer with what is known.
WRAP_UP = ("STOP investigating: your budget for this run is used up ({why}). Do not run any more tools. "
           "Reply now with the final answer in the required format, based only on what you have already found; "
           "say explicitly what you could not check.")


@dataclass
class RunSpec:
    run_id: str
    role: ResolvedRole
    cwd: str
    prompt: str
    output_schema: dict | None = None
    images: list[str] = field(default_factory=list)
    thread_id: str | None = None          # resume/reuse (Main reuses one thread)
    schema_name: str = ""
    tools: list[str] | None = None        # override the role's default tool list ([] = no tools)
    writable_roots: list[str] = field(default_factory=list)   # extra writable dirs (deployer: the repo's git dir, so merges/pushes update refs)


@dataclass
class RunResult:
    status: Status
    final_text: str = ""
    thread_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    reset: str | None = None              # when a subscription quota resets (as reported by the CLI)


@dataclass
class Approval:
    run_id: str
    kind: str                             # command | file_change | read (a file path) | mcp (a tool name)
    command: str | None
    cwd: str | None
    reason: str | None


EventSink = Callable[[str, dict], Awaitable[None]]
ApprovalHandler = Callable[[Approval], Awaitable[bool]]


class AgentRuntime(Protocol):
    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult: ...
    async def cancel(self, run_id: str) -> None: ...
    async def close(self) -> None: ...


def labelled(emit: EventSink, run_id: str, label: str) -> EventSink:
    """Wrap a sink so this call's events carry its own run id and a human label on agent.started."""
    async def f(t: str, p: dict) -> None:
        p = {**p, "_run": run_id}
        if t == "agent.started":
            p["label"] = label
        await emit(t, p)
    return f
