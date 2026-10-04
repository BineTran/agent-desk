"""ContextBuilder: L2 memory -> bounded ContextPacket for ONE agent run. Never sees chat."""
from __future__ import annotations

from ..contracts import ContextPacket, InputItem, TaskSpec
from .memory import Memory


def est_tokens(obj) -> int:
    return len(obj if isinstance(obj, str) else obj.model_dump_json()) // 4 + 1


def _relevant(i: InputItem, task: TaskSpec | None, role: str) -> bool:
    if role in ("main", "architect"):
        return True
    if i.kind == "image":
        return False                                    # images only go to Main (and only if the task text cites them)
    if task is None:
        return True
    hay = (task.goal + " " + task.title + " " + " ".join(task.acceptance_criteria)).lower()
    return i.id.lower() in hay or i.kind in ("doc", "url", "file", "dir")


async def build(mem: Memory, role: str, task: TaskSpec | None, *, max_tokens: int = 6000,
                previous_attempt: str | None = None, failure_tail: list[str] | None = None,
                tail_lines: int = 60) -> ContextPacket:
    files = [f for f in await mem.files()
             if task is None or not task.relevant_files or f.path in task.relevant_files or role in ("main", "architect")]
    pkt = ContextPacket(
        brief=await mem.brief(), task=task,
        acceptance=(task.acceptance_criteria if task else []),
        decisions=await mem.decisions_in_force(),
        inputs=[i for i in await mem.inputs() if _relevant(i, task, role)],
        relevant_files=files, previous_attempt_summary=previous_attempt,
        failure_tail=(failure_tail or [])[-tail_lines:])
    # trim in order of least value until within budget; failures + task + decisions are never dropped
    while est_tokens(pkt) > max_tokens and (pkt.inputs or pkt.relevant_files):
        if pkt.inputs:
            pkt = pkt.model_copy(update={"inputs": pkt.inputs[:-1]})
        else:
            pkt = pkt.model_copy(update={"relevant_files": pkt.relevant_files[:-1]})
    return pkt
