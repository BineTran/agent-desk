"""Typed contracts shared across the harness. Flat, all-required where sent as outputSchema."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["main", "explorer", "worker", "researcher", "architect", "deployer", "debug", "chat"]


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- plan ----
class TaskSpec(M):
    id: str
    title: str
    goal: str
    kind: Literal["investigation", "implementation", "research", "review", "verification", "deployment"]
    depends_on: list[str]
    relevant_files: list[str]
    files_known: bool
    requires_write: bool
    acceptance_criteria: list[str]
    outputs: list[str]                     # files (or globs) the task must create or change; checked against its diff

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, v):
        if isinstance(v, dict) and "outputs" not in v:      # plans persisted before `outputs` existed
            v = {**v, "outputs": []}
        return v


class Plan(M):
    goal: str
    constraints: list[str]
    acceptance_criteria: list[str]
    tasks: list[TaskSpec]
    risks: list[str]
    out_of_scope: list[str]


class Question(M):
    id: str
    text: str
    options: list[str]
    recommended: int
    evidence: str


class Questions(M):
    questions: list[Question]


# ---- agent I/O ----
class Evidence(M):
    kind: Literal["file", "command", "input"]
    ref: str            # path:lines | command | I-n
    note: str


class Escalation(M):
    reason: str
    evidence: list[Evidence]


class AgentReport(M):
    status: Literal["completed", "blocked", "escalate"]
    summary: str
    evidence: list[Evidence]
    open_questions: list[str]
    changed_files: list[str]
    escalate: Escalation | None


class Finding(M):
    id: str
    severity: Literal["critical", "major", "minor"]
    file: str
    message: str
    root_cause: str


class ArchitectReview(M):
    verdict: Literal["approve", "revise"]
    findings: list[Finding]
    advice: list[str]


class FileRef(M):
    path: str
    lines: str
    why: str
    commit: str
    stale: bool = False


class Decision(M):
    id: str                 # D-001
    text: str
    verbatim: str | None = None
    source: Literal["user", "main", "architect"]
    supersedes: str | None = None


class InputItem(M):
    id: str                 # I-1
    kind: Literal["file", "dir", "doc", "image", "url", "text"]
    ref: str
    snapshot: str | None = None
    sha256: str | None = None
    summary: str | None = None


class ContextPacket(M):
    brief: str
    task: TaskSpec | None
    acceptance: list[str]
    decisions: list[Decision]
    inputs: list[InputItem]
    relevant_files: list[FileRef]
    previous_attempt_summary: str | None = None
    failure_tail: list[str] = []


class AgentRequest(M):
    run_id: str
    role: Role
    task_id: str | None
    attempt: int
    plan_version: int
    packet: ContextPacket
    output_schema: str


# ---- decisions (control) ----
class ControlDecision(M):
    type: Literal["route", "tier", "retry_or_stop", "dangerous", "intent", "env_access"]
    engine: Literal["rule", "llm", "jev", "main", "user", "debug"]
    selected: str
    confidence: float | None
    distribution: dict[str, float]
    sharp: bool | None
    policy_version: str = "v1"
    input_snapshot: dict[str, Any] = {}


# ---- events ----
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Event(M):
    session_id: str
    seq: int = 0
    ts: str = Field(default_factory=now)
    source: str
    type: str
    task_id: str | None = None
    agent_run_id: str | None = None
    payload: dict[str, Any] = {}


class Summary(M):
    text: str
    ac_evidence: list[str]


class Pick(M):
    choice: str
    reason: str


class MainReply(M):
    kind: Literal["answer", "proposal", "stop"]
    text: str
    plan: Plan | None


class Route(M):
    """How Main handles one chat message. answer: `text` IS the answer (no plan, no other agent); quick: one small worker task
    (`task`), verified like any other; plan: the full plan flow. escalate is only for the cheap `chat` triage role (Main never uses it)."""
    kind: Literal["answer", "quick", "plan", "escalate"]
    text: str              # answer: the full answer; quick/plan: one line on what happens next
    brief: str | None      # quick/plan: the request restated so it stands alone without the chat
    task: TaskSpec | None  # quick only
    reason: str


class TriageItem(M):
    finding_id: str
    verdict: Literal["fixed", "rejected", "question"]
    reason: str
    question: str | None
    options: list[str]


class Triage(M):
    items: list[TriageItem]
    plan: Plan | None


class MainTurn(M):
    """The ONE output schema of Main's thread. The schema is part of the cached prompt prefix (Spike M7): changing it
    between turns throws the whole cache away, so every Main call fills exactly one field and leaves the rest null."""
    questions: Questions | None
    plan: Plan | None
    triage: Triage | None
    reply: MainReply | None
    summary: Summary | None
    pick: Pick | None
    route: Route | None


MAIN_FIELD = {"Route": "route", "Questions": "questions", "Plan": "plan", "Triage": "triage", "MainReply": "reply", "Summary": "summary", "Pick": "pick"}
