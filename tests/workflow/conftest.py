import json
import subprocess
from pathlib import Path

import pytest

from agent_desk.config.loader import load
from agent_desk.contracts import MAIN_FIELD, AgentReport, ArchitectReview, MainReply, Plan, Questions, Route, Summary, TaskSpec, Triage
from agent_desk.runtime.base import RunResult
from agent_desk.runtime.mock import MockRuntime

NOGLOBAL = Path("/nonexistent")


def task(id, deps=(), write=False, title=None, outputs=()):
    return TaskSpec(id=id, title=title or id, goal=f"do {id}", kind="implementation" if write else "investigation",
                    depends_on=list(deps), relevant_files=[], files_known=False, requires_write=write, acceptance_criteria=[f"AC for {id}"],
                    outputs=list(outputs))


def report(status="completed", summary="ok", evidence=(), changed=()):
    return AgentReport(status=status, summary=summary, evidence=list(evidence), open_questions=[], changed_files=list(changed), escalate=None)


def plan(*tasks):
    return Plan(goal="g", constraints=[], acceptance_criteria=["AC-1"], tasks=list(tasks), risks=[], out_of_scope=[])


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "calc.py").write_text("def add(a,b): return a+b\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    return r


def cfg(repo, checks: dict[str, str] | None = None, extra: str = ""):
    checks = checks if checks is not None else {"unit": "true"}
    (repo / ".agent-desk.yaml").write_text("verification:\n" + "".join(f"  {k}: {{ command: \"{v}\", required: true }}\n" for k, v in checks.items())
                                           if checks else "verification: {}\n")
    if extra:
        (repo / ".agent-desk.yaml").write_text((repo / ".agent-desk.yaml").read_text() + extra)
    return load(repo, global_path=NOGLOBAL)


def main_turn(schema_name: str, inner_json: str) -> str:
    """Main answers through ONE envelope schema (MainTurn); scripts write the inner model, this wraps it."""
    out = {k: None for k in MAIN_FIELD.values()}
    out[MAIN_FIELD[schema_name]] = json.loads(inner_json)
    return json.dumps(out)


class Script:
    """Programmable agent behaviour. Records everything; worker edits real files in the worktree."""
    def __init__(self, plan_, questions=None, worker=None, worker_status="completed", worker_summary=None, reply=None, architect=None, triage=None, revise=None, route=None, chat=None):
        self.architect, self.triage, self.revise, self.reviews = architect, triage, revise, []
        self.reply, self.prompts, self.route, self.chat = reply, [], route, chat
        self.worker_status, self.worker_summary = worker_status, worker_summary
        self.plan, self.questions, self.worker = plan_, questions or Questions(questions=[]), worker or (lambda spec, n: ("a.txt", "x"))
        self.calls: dict[str, int] = {}
        self.worker_results: list[str] = []

    def __call__(self, spec):
        res = self._answer(spec)
        if (spec.output_schema or {}).get("title") == "MainTurn" and res.status == "completed":
            res.final_text = main_turn(spec.schema_name, res.final_text)
        return res

    def _answer(self, spec):
        n = spec.schema_name
        if n == "Route":
            if spec.role.role == "chat":
                r = self.chat(spec) if self.chat else Route(kind="escalate", text="", brief=None, task=None, reason="too big")
            else:
                self.prompts.append(spec.prompt)
                r = self.route(spec) if self.route else Route(kind="plan", text="Making a plan.", brief=None, task=None, reason="default")
            return RunResult("completed", r.model_dump_json(), "th-chat" if spec.role.role == "chat" else "th-main")
        if n == "Questions":
            return RunResult("completed", self.questions.model_dump_json(), "th-main")
        if n == "ArchitectReview":
            trig = spec.run_id.split("-")[1]
            self.reviews.append(trig)
            r = self.architect(trig, spec) if self.architect else ArchitectReview(verdict="approve", findings=[], advice=[])
            return RunResult("completed", r.model_dump_json(), "th-arch")
        if n == "Triage":
            r = self.triage(spec) if self.triage else Triage(items=[], plan=None)
            return RunResult("completed", r.model_dump_json(), "th-main")
        if n == "Plan" and "Revise the plan to act on this advice" in spec.prompt:
            p = self.revise(spec) if self.revise else (self.plan() if callable(self.plan) else self.plan)
            return RunResult("completed", p.model_dump_json(), "th-main")
        if n == "Plan":
            p = self.plan() if callable(self.plan) else self.plan
            return RunResult("completed", p.model_dump_json(), "th-main")
        if n == "MainReply":
            self.prompts.append(spec.prompt)
            r = self.reply(spec) if self.reply else MainReply(kind="answer", text="ok", plan=None)
            return RunResult("completed", r.model_dump_json(), "th-main")
        if n == "Summary":
            return RunResult("completed", Summary(text="done", ac_evidence=["AC-1: unit passed"]).model_dump_json(), "th-main")
        # AgentReport
        tid = spec.run_id.split("-")[1]
        self.calls[tid] = self.calls.get(tid, 0) + 1
        if spec.role.role == "worker":
            fn, content = self.worker(spec, self.calls[tid])
            if fn:
                (Path(spec.cwd) / fn).write_text(content)
            rep = report(status=self.worker_status, summary=self.worker_summary or f"{tid} attempt {self.calls[tid]}")
        else:
            rep = report(evidence=[__import__("agent_desk.contracts", fromlist=["Evidence"]).Evidence(kind="file", ref="calc.py:1-1", note="add lives here")])
        return RunResult("completed", rep.model_dump_json(), f"th-{spec.run_id}")


def qtask(*outputs, title="tweak"):
    """The TaskSpec Main puts in a quick route."""
    return task("T1", write=True, title=title, outputs=outputs or ("a.txt",))


def route(kind, text="ok", task=None, brief=None, reason="test"):
    return Route(kind=kind, text=text, brief=brief, task=task, reason=reason)
