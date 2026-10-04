"""Deployer role, per-role MCP grant and the Debug gatekeeper for env/secret reads."""
import json

from agent_desk.contracts import TaskSpec
from agent_desk.runtime.base import Approval, RunResult
from agent_desk.ui import HeadlessUI

from .conftest import Script, plan, task
from .test_flow import go, types


def deploy_task(id="T2", deps=("T1",)):
    return TaskSpec(id=id, title="deploy to dev", goal="merge into deployment/dev and push", kind="deployment", depends_on=list(deps),
                    relevant_files=[], files_known=False, requires_write=True, acceptance_criteria=["pushed"])


class AskUI(HeadlessUI):
    def __init__(self, answer=False):
        self.asked, self.answer = [], answer

    async def approve_command(self, a, advice):
        self.asked.append((a.command, advice))
        return self.answer


def gated(script, decision="allow", fail=False):
    def f(spec):
        if spec.schema_name == "Gate":
            if fail:
                return RunResult("failed", "", "", error="boom")
            return RunResult("completed", json.dumps({"decision": decision, "reason": f"because {decision}"}), "th-g")
        return script(spec)
    return f


async def test_deployment_task_runs_as_deployer_with_git_dir_writable_and_no_diff_required(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"), deploy_task())))
    assert out == "COMPLETED"
    dep = [x for x in rt.specs if x.role.role == "deployer"]
    assert len(dep) == 1 and dep[0].role.network and dep[0].role.sandbox == "workspaceWrite"
    assert dep[0].writable_roots and dep[0].writable_roots[0].endswith(".git")
    assert "never force-push" in dep[0].prompt and "Do not commit or push" not in dep[0].prompt
    assert "nodiff" not in json.dumps([e.payload for e in await s.bus.store.events(s.sid)])


async def test_disabled_deployer_falls_back_to_worker(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"), deploy_task())), extra="roles:\n  deployer: { enabled: false }\n")
    assert not [x for x in rt.specs if x.role.role == "deployer"] and [x for x in rt.specs if x.role.role == "worker"]


async def test_git_push_asks_the_user_and_force_push_is_denied(repo, tmp_path):
    ui = AskUI(answer=True)
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"))), ui=ui)
    h = s.approval_handler("deployer", "T1")
    assert await h(Approval("r", "command", "git push origin deployment/dev", str(s.ws.path), None)) is True
    assert await h(Approval("r", "command", "git push --force origin deployment/dev", str(s.ws.path), None)) is False
    assert [c for c, _ in ui.asked] == ["git push origin deployment/dev"]


async def test_mcp_calls_are_allowed_and_logged(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"))))
    assert await s.approval_handler("explorer", "T1")(Approval("r", "mcp", "mcp__clickup__get_task", None, None)) is True
    ev = [e.payload for e in await s.bus.store.events(s.sid) if e.type == "approval.classified"]
    assert ev[-1] == {"command": "mcp__clickup__get_task", "verdict": "allow", "kind": "mcp"}


async def test_debug_gatekeeper_allows_denies_or_hands_to_the_user(repo, tmp_path):
    for decision, expect, asked in (("allow", True, False), ("deny", False, False), ("ask_user", True, True)):
        ui = AskUI(answer=True)
        s, rt, out = await go(repo, tmp_path / decision, gated(Script(plan(task("T1"))), decision), ui=ui)
        ok = await s.approval_handler("explorer", "T1")(Approval("r", "command", "cat .env", str(s.ws.path), "need DATABASE_URL"))
        assert ok is expect and bool(ui.asked) is asked
        if asked:
            assert "debug gatekeeper: because ask_user" in ui.asked[0][1]           # the gatekeeper's reason is shown to the user
        gate = [x for x in rt.specs if x.schema_name == "Gate"][-1]
        assert gate.role.role == "debug" and gate.tools == [] and "cat .env" in gate.prompt and "do T1" in gate.prompt
        g = [e.payload for e in await s.bus.store.events(s.sid) if e.type == "approval.gated"]
        assert g[-1]["decision"] == decision and g[-1]["by"] == "debug"


async def test_gatekeeper_failure_goes_to_the_user_and_plain_reads_skip_it(repo, tmp_path):
    ui = AskUI(answer=False)
    s, rt, out = await go(repo, tmp_path, gated(Script(plan(task("T1"))), fail=True), ui=ui)
    h = s.approval_handler("explorer", "T1")
    assert await h(Approval("r", "read", str(s.ws.path / ".env"), None, None)) is False
    assert "no usable answer" in ui.asked[0][1]
    assert await h(Approval("r", "read", str(s.ws.path / "calc.py"), None, None)) is True and len(ui.asked) == 1


async def test_env_gate_modes_deny_and_user(repo, tmp_path):
    ui = AskUI(answer=True)
    s, rt, out = await go(repo, tmp_path / "d", Script(plan(task("T1"))), ui=ui, extra="approval: { env_gate: deny }\n")
    assert await s.approval_handler("explorer", "T1")(Approval("r", "command", "printenv", str(s.ws.path), None)) is False and not ui.asked
    s, rt, out = await go(repo, tmp_path / "u", Script(plan(task("T1"))), ui=ui, extra="approval: { env_gate: user }\n")
    assert await s.approval_handler("explorer", "T1")(Approval("r", "command", "printenv", str(s.ws.path), None)) is True
    assert ui.asked and not [x for x in rt.specs if x.schema_name == "Gate"]
