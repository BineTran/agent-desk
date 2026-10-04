import pytest

from agent_desk import gitws, scheduler as S
from agent_desk.config.schema import Check
from agent_desk.contracts import TaskSpec
from agent_desk.verifier import fingerprint, required_failed, run_all


def T(id, deps=(), write=False):
    return TaskSpec(id=id, title=id, goal=id, kind="implementation" if write else "investigation", depends_on=list(deps),
                    relevant_files=[], files_known=False, requires_write=write, acceptance_criteria=[])


def test_nothing_runs_before_plan_locked_or_after_cancel():
    s = S.Scheduler(); s.load([T("T1")])
    assert s.ready() == []
    s.plan_locked = True
    assert [t.spec.id for t in s.ready()] == ["T1"]
    s.cancel(); assert s.ready() == []


def test_dag_and_single_writer_queueing():
    s = S.Scheduler(); s.plan_locked = True
    s.load([T("T1"), T("T2", ["T1"], True), T("T3", ["T1"], True)])
    assert [t.spec.id for t in s.ready()] == ["T1"]
    s.start("T1"); s.finish("T1", True)
    assert [t.spec.id for t in s.ready()] == ["T2"]            # one writer only
    s.start("T2")
    assert s.ready() == [] and s.tasks["T3"].status == S.QUEUED
    s.finish("T2", True)
    assert [t.spec.id for t in s.ready()] == ["T3"]


def test_slot_limit():
    s = S.Scheduler(max_agents=2); s.plan_locked = True
    s.load([T("A"), T("B"), T("C")])
    assert len(s.ready()) == 2


def test_retry_then_escalate_on_same_fingerprint():
    s = S.Scheduler(); s.plan_locked = True; s.load([T("T2", write=True)])
    s.start("T2"); assert s.finish("T2", False, "fp1") == "retry"
    s.start("T2"); assert s.finish("T2", False, "fp1") == "escalate"


def test_retry_budget_exhausted_with_different_failures():
    s = S.Scheduler(max_retries=2); s.plan_locked = True; s.load([T("T2", write=True)])
    out = []
    for i in range(3):
        s.start("T2"); out.append(s.finish("T2", False, f"fp{i}"))
    assert out == ["retry", "retry", "escalate"]


def test_reload_keeps_progress_and_stale():
    s = S.Scheduler(); s.plan_locked = True; s.load([T("T1"), T("T2", ["T1"])])
    s.start("T1"); s.finish("T1", True)
    s.load([T("T1"), T("T2", ["T1"]), T("T3", ["T2"])])
    assert s.tasks["T1"].status == S.DONE and "T3" in s.tasks
    s.mark_stale(["T1"]); assert s.tasks["T1"].status == S.STALE


def test_fingerprint_ignores_timing_and_paths():
    a = ["FAILED test_x - AssertionError: 409 != 200 in 1.2s /tmp/abc/x.py"]
    b = ["FAILED test_x - AssertionError: 409 != 200 in 9.9s /tmp/zzz/x.py"]
    assert fingerprint("unit", a) == fingerprint("unit", b) != fingerprint("lint", a)


async def test_verifier_required_vs_optional(tmp_path):
    res = await run_all({"ok": Check(command="true"), "bad": Check(command="echo 'AssertionError boom'; exit 1"),
                         "opt": Check(command="exit 3", required=False)}, str(tmp_path))
    assert [r.passed for r in res] == [True, False, False]
    assert [r.name for r in required_failed(res)] == ["bad"] and res[1].fingerprint


async def test_worktree_isolated_and_commits(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    await gitws.git(repo, "init", "-q"); (repo / "a.txt").write_text("1")
    await gitws.git(repo, "add", "-A"); await gitws.git(repo, "commit", "-qm", "i")
    ws = await gitws.create(repo, tmp_path / "sess", "s1")
    assert await gitws.commit_all(ws, "noop") is None           # nothing changed -> no empty commit
    (ws.path / "a.txt").write_text("2")
    assert (repo / "a.txt").read_text() == "1"                  # user's checkout untouched
    assert await gitws.changed_files(ws) == ["a.txt"] and "+2" in await gitws.diff(ws)
    assert await gitws.commit_all(ws, "t") and (await gitws.git(repo, "branch", "--list", "agent-desk/s1"))
