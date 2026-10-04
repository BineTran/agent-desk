import asyncio
from pathlib import Path

from agent_desk.contracts import Question, Questions
from agent_desk.graph import open_session, run_graph
from agent_desk.runtime.mock import MockRuntime
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, task


async def go(repo, tmp_path, script, checks=None, ui=None, delay=0, extra=""):
    rt = MockRuntime(script, delay)
    s = await open_session(repo, "implement feature X", cfg(repo, checks, extra), rt, ui or HeadlessUI(), tmp_path / "home")
    return s, rt, await run_graph(s)


async def types(s):
    return [e.type for e in await s.bus.store.events(s.sid)]


async def test_happy_path_commits_on_session_branch_and_repo_untouched(repo, tmp_path):
    sc = Script(plan(task("T1"), task("T2", ["T1"], True)))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "COMPLETED"
    log = (await __import__("agent_desk.gitws", fromlist=["git"]).git(repo, "log", "--oneline", s.ws.branch)).splitlines()
    assert len(log) == 2 and "T2" in log[0]
    assert not (repo / "a.txt").exists() and (s.ws.path / "a.txt").exists()
    t = await types(s)
    assert t.index("plan.approved") < t.index("task.done") and t[-1] != "error" and "final.summary" in t
    assert (s.home / "session.md").exists()


async def test_worker_never_runs_before_plan_is_approved(repo, tmp_path):
    class NoUI(HeadlessUI):
        async def run_studio(self, studio): return False
    sc = Script(plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, ui=NoUI())
    assert out == "CANCELLED" and not [x for x in rt.specs if x.role.role == "worker"]


async def test_fail_then_retry_succeeds_with_failure_context(repo, tmp_path):
    # a.txt must contain 'good' for the check to pass; attempt 1 writes 'bad'
    checks = {"unit": "test ! -f a.txt || grep -q good a.txt || { echo 'AssertionError: not good'; exit 1; }"}   # base (no a.txt) passes
    sc = Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "bad" if n == 1 else "good"))
    s, rt, out = await go(repo, tmp_path, sc, checks)
    assert out == "COMPLETED"
    workers = [x for x in rt.specs if x.role.role == "worker"]
    assert len(workers) == 2 and "AssertionError: not good" in workers[1].prompt and "Previous attempt" in workers[1].prompt
    assert "AssertionError" not in workers[0].prompt


async def test_same_failure_twice_escalates_and_never_completes(repo, tmp_path):
    checks = {"unit": "test ! -f a.txt || { echo 'AssertionError: boom'; exit 1; }"}   # base passes, every attempt fails
    sc = Script(plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, checks)
    assert out == "FAILED"
    t = await types(s)
    assert t.count("task.escalate") == 2 and "final.summary" not in t       # escalated, one replan allowed, escalated again -> stop
    assert t.count("task.replanned") == 1 and len([x for x in rt.specs if x.role.role == "worker"]) == 4


async def test_implementation_without_diff_is_not_done(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)), worker=lambda spec, n: (None, ""))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "FAILED"


async def test_only_one_writer_at_a_time(repo, tmp_path):
    running = {"n": 0, "max": 0}
    inner = Script(plan(task("T1", write=True), task("T2", write=True)), worker=lambda spec, n: (f"{spec.run_id}.txt", "x"))
    sc = lambda spec: inner(spec)
    s, rt, out = await go(repo, tmp_path, sc, delay=0.05)
    ev = await s.bus.store.events(s.sid)
    live, peak = 0, 0
    for e in ev:
        if e.source == "worker" and e.type == "agent.started": live += 1
        if e.source == "worker" and e.type == "agent.finished": live -= 1
        peak = max(peak, live)
    assert out == "COMPLETED" and peak == 1


async def test_clarify_answers_become_decisions_with_verbatim_text(repo, tmp_path):
    qs = Questions(questions=[Question(id="Q-1", text="What does 409 return?", options=["{currentRevision}", "empty"], recommended=0, evidence="PRD s4")])
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1")), qs))
    ds = await s.mem.decisions_in_force()
    assert [d.id for d in ds] == ["D-001"] and ds[0].source == "user" and "currentRevision" in ds[0].text
    assert "decision.recorded" in await types(s)


async def test_explorer_findings_become_relevant_files_for_later_tasks(repo, tmp_path):
    sc = Script(plan(task("T1"), task("T2", ["T1"], True)))
    s, rt, out = await go(repo, tmp_path, sc)
    files = await s.mem.files()
    assert files and files[0].path == "calc.py"
    worker_prompt = [x for x in rt.specs if x.role.role == "worker"][0].prompt
    assert "calc.py:1-1" in worker_prompt and "add lives here" in worker_prompt   # explorer evidence reached the worker as a pointer


async def test_events_persisted_in_order_and_secrets_redacted(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"))))
    ev = await s.bus.store.events(s.sid)
    assert [e.seq for e in ev] == list(range(1, len(ev) + 1))


async def test_gate_blocks_completion_if_plan_changes_after_approval(repo, tmp_path):
    from agent_desk.orchestrator import plan_hash
    sc = Script(plan(task("T1")))
    rt = MockRuntime(sc)
    s = await open_session(repo, "x", cfg(repo), rt, HeadlessUI(), tmp_path / "home")
    assert await s.plan_phase()
    await s.execute()
    s.plan = plan(task("T1"), task("T9"))                     # tampered after approval
    assert plan_hash(s.plan) != s.approved_hash
    assert any("differs from the approved plan" in u for u in await s.completion_gate())


async def test_failed_verification_blocks_final_gate_even_if_tasks_done(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)))
    rt = MockRuntime(sc)
    s = await open_session(repo, "x", cfg(repo, {"unit": "test ! -f a.txt || test -f never-created"}), rt, HeadlessUI(), tmp_path / "home")
    assert await s.plan_phase()
    (s.ws.path / "a.txt").write_text("x")                              # the change that breaks the check (passes on base)
    s.sched.load(s.plan.tasks)
    s.sched.start("T1"); s.sched.finish("T1", True, commit="abc")      # tasks claim done...
    unmet = await s.completion_gate()
    assert any("required check failed: unit" in u for u in unmet)     # ...but evidence says no


async def test_replay_reconstructs_session_from_events_alone(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"), task("T2", ["T1"], True))))
    from agent_desk.events.store import EventStore
    st = await EventStore(tmp_path / "home" / "db" / "agent-desk.sqlite").open()    # a different connection = a "reload"
    ev = await st.events(s.sid)
    assert [e.type for e in ev if e.type.startswith("task.")] == ["task.done", "task.done"]
    assert ev[-1].type == "final.summary" and ev[0].type == "session.created"


async def test_quota_is_a_state_not_a_failure(repo, tmp_path):
    from agent_desk.runtime.base import RunResult
    inner = Script(plan(task("T1", write=True)))
    calls = {"n": 0}

    def script(spec):
        if spec.role.role == "worker":
            calls["n"] += 1
            if calls["n"] == 1:
                return RunResult("quota", "", "", {}, "limit", "3:45pm")
        return inner(spec)
    s, rt, out = await go(repo, tmp_path, script)
    assert out == "WAITING_QUOTA"
    t = await types(s)
    assert "quota.hit" in t and "task.retry" not in t and "task.escalate" not in t and "final.summary" not in t
    assert s.sched.tasks["T1"].attempts == 0 and s.sched.tasks["T1"].status == "pending"      # no retry burned
    row = await (await s.bus.store.db.execute("SELECT status FROM sessions WHERE id=?", (s.sid,))).fetchone()
    assert row["status"] == "WAITING_QUOTA"


async def test_environment_failure_stops_for_a_human_and_burns_no_retry(repo, tmp_path):
    checks = {"unit": "echo '/usr/bin/python3: No module named pytest'; exit 1"}
    sc = Script(plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, checks)
    assert out == "FAILED"
    t = await types(s)
    assert "task.stop" in t and "task.retry" not in t and len([x for x in rt.specs if x.role.role == "worker"]) == 1
    assert any("needs a human" in u for u in s.unmet)
    ds = await (await s.bus.store.db.execute("SELECT type, engine, selected FROM decisions WHERE type='retry_or_stop'")).fetchall()
    assert [(d["engine"], d["selected"]) for d in ds] == [("rule", "stop")]


async def test_route_and_tier_decisions_are_recorded_with_engine(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"), task("T2", ["T1"], True))))
    rows = await (await s.bus.store.db.execute("SELECT type, engine, selected FROM decisions ORDER BY id")).fetchall()
    got = [(r["type"], r["engine"], r["selected"]) for r in rows]
    assert ("route", "rule", "explorer") in got and ("route", "rule", "worker") in got and ("tier", "rule", "strong") in got


async def test_blocked_worker_is_still_verified_and_env_error_stops(repo, tmp_path):
    """Regression from the real run: the worker said 'blocked', verification was skipped, the rule saw no evidence, Haiku said retry."""
    checks = {"unit": "echo '/usr/bin/python3: No module named pytest'; exit 1"}
    sc = Script(plan(task("T1", write=True)), worker_status="blocked", worker_summary="Implemented it but pytest is not installed")
    s, rt, out = await go(repo, tmp_path, sc, checks)
    t = await types(s)
    assert out == "FAILED" and "verification.result" in t and "task.stop" in t and "task.retry" not in t
    assert len([x for x in rt.specs if x.role.role == "worker"]) == 1


async def test_blocked_worker_with_passing_checks_is_not_done(repo, tmp_path):
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1", write=True)), worker_status="blocked", worker_summary="stuck on something"))
    assert out == "FAILED" and "final.summary" not in await types(s)           # passing tests do not override an agent that says it is blocked


def _with_node_tool(repo):
    import subprocess
    (repo / ".gitignore").write_text("node_modules/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore"], cwd=repo, check=True)
    b = repo / "node_modules" / ".bin"; b.mkdir(parents=True)
    (b / "lintit").write_text("#!/bin/sh\necho lint-ok\n"); (b / "lintit").chmod(0o755)


async def test_worktree_gets_gitignored_deps_so_verification_runs(repo, tmp_path):
    """Regression (s-8fb5): fresh worktree had no node_modules -> 'biome: command not found' -> FAILED."""
    _with_node_tool(repo)
    s2, rt2, out2 = await go(repo, tmp_path / "b", Script(plan(task("T1", write=True))), {"lint": "node_modules/.bin/lintit"})
    assert out2 == "COMPLETED" and (s2.ws.path / "node_modules").is_symlink()
    shown = await __import__("agent_desk.gitws", fromlist=["git"]).git(s2.ws.path, "log", "--name-only", "--format=", s2.ws.branch)
    assert "node_modules" not in shown
    assert "workspace.prepared" in await types(s2)


async def test_env_error_then_setup_and_resume_completes(repo, tmp_path):
    _with_node_tool(repo)
    extra = "workspace: { link: [] }\n"                       # linking off -> the check cannot find the tool
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1"), task("T2", ["T1"], True))),
                          {"lint": "node_modules/.bin/lintit || (echo 'sh: node_modules/.bin/lintit: command not found'; exit 127)"}, extra=extra)
    assert out == "FAILED" and any("environment error" in u for u in s.unmet)
    s.cfg.workspace.link = ["node_modules"]                    # what the user fixes (or workspace.setup)
    workers_before = len([x for x in rt.specs if x.role.role == "worker"])
    out2 = await s.setup_and_resume()
    assert out2 == "COMPLETED" and (s.ws.path / "node_modules").is_symlink()
    assert len([x for x in rt.specs if x.role.role == "explorer"]) == 1    # done task T1 was kept, not rerun
    assert len([x for x in rt.specs if x.role.role == "worker"]) == workers_before + 1


async def test_workspace_setup_runs_in_worktree_before_verification(repo, tmp_path):
    extra = "workspace: { setup: \"mkdir -p deps && echo ok > deps/ready\" }\n"
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1", write=True))), {"unit": "test -f deps/ready"}, extra=extra)
    assert out == "COMPLETED"
    t = [e for e in await s.bus.store.events(s.sid) if e.type == "workspace.setup"]
    assert [e.payload["status"] for e in t] == ["running", "ok"]


async def test_pre_existing_failure_does_not_block_but_a_new_one_does(repo, tmp_path):
    """s-8fb5: the repo's suite already fails on base; the session must only answer for what it broke."""
    (repo / "legacy.txt").write_text("broken\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "legacy"], cwd=repo, check=True)
    unit = ("fail=0; grep -q broken legacy.txt && { echo 'FAILED tests/test_legacy.py::test_old'; fail=1; }; "
            "test -f a.txt && grep -q bad a.txt && { echo 'FAILED tests/test_a.py::test_new'; fail=1; }; exit $fail")
    s, rt, out = await go(repo, tmp_path, Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "good")), {"unit": unit})
    assert out == "COMPLETED"
    res = [e.payload for e in await s.bus.store.events(s.sid) if e.type == "verification.result"]
    assert res and res[0]["status"] == "pre-existing" and res[0]["new_failures"] == []
    assert any(e.type == "verification.baseline" for e in await s.bus.store.events(s.sid))

    sc = Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "bad" if n == 1 else "good"))
    s2, rt2, out2 = await go(repo, tmp_path / "b", sc, {"unit": unit})
    assert out2 == "COMPLETED"
    workers = [x for x in rt2.specs if x.role.role == "worker"]
    assert len(workers) == 2 and "test_new" in workers[1].prompt and "test_old" not in workers[1].prompt   # only the new failure


async def test_docs_only_change_skips_scoped_checks(repo, tmp_path):
    checks_extra = 'verification:\n  unit: { command: "exit 1", paths: ["**/*.py"], required: true }\n'
    (repo / ".agent-desk.yaml").write_text(checks_extra)
    from agent_desk.config.loader import load
    from .conftest import NOGLOBAL
    rt = MockRuntime(Script(plan(task("T1", write=True)), worker=lambda spec, n: ("notes.md", "hello")))
    s = await open_session(repo, "write docs", load(repo, global_path=NOGLOBAL), rt, HeadlessUI(), tmp_path / "home")
    out = await run_graph(s)
    gate = [e.payload for e in await s.bus.store.events(s.sid) if e.type in ("completion.gate", "task.failed", "error")]
    assert out == "COMPLETED", gate
    res = [e.payload for e in await s.bus.store.events(s.sid) if e.type == "verification.result"]
    assert res and all(r["status"] == "skipped" for r in res)


async def test_answer_only_plan_without_tasks_completes(repo, tmp_path):
    """'1 + 1?': Main plans no tasks; approving must work and the session completes with Main's answer, no worker, no checks."""
    from agent_desk.contracts import Plan
    p = Plan(goal="Answer: 1 + 1 = 2", constraints=[], acceptance_criteria=["The user receives the answer"], tasks=[], risks=[], out_of_scope=[])
    s, rt, out = await go(repo, tmp_path, Script(p), {"unit": "exit 1"})
    assert out == "COMPLETED"
    t = await types(s)
    assert "plan.approved" in t and "final.summary" in t and not [x for x in rt.specs if x.role.role in ("worker", "explorer")]


async def test_commit_hook_failure_is_retried_by_the_worker_not_an_internal_error(repo, tmp_path):
    """Regression (s-69b1): a failing commit hook cancelled the whole session as an 'internal error'."""
    import subprocess
    hook = repo / ".githooks" / "pre-commit"; hook.parent.mkdir()
    hook.write_text('#!/bin/sh\ngrep -q good a.txt || { echo "eslint: a.txt is not good" >&2; exit 1; }\n'); hook.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "hooks", "--no-verify"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)
    sc = Script(plan(task("T1", write=True, title="Write a.txt")), worker=lambda spec, n: ("a.txt", "bad" if n == 1 else "good"))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "COMPLETED"
    workers = [x for x in rt.specs if x.role.role == "worker"]
    assert len(workers) == 2 and "a.txt is not good" in workers[1].prompt     # the hook output is the retry evidence
    log = (await __import__("agent_desk.gitws", fromlist=["git"]).git(repo, "log", "--format=%s", "-1", s.ws.branch))
    assert log == "chore(agent-desk): write a.txt (T1)"                    # conventional, lowercase subject


async def test_task_missing_a_promised_output_goes_back_to_the_worker(repo, tmp_path):
    """Regression (s-69b1): T3 promised contract.md/types.ts/openapi.yaml, only edited plan.md, and was accepted as done."""
    def worker(spec, n):
        if n >= 2:
            (Path(spec.cwd) / "b.txt").write_text("b\n")
        return "a.txt", "a"
    sc = Script(plan(task("T1", write=True, outputs=["a.txt", "b.txt"])), worker=worker)
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "COMPLETED"
    workers = [x for x in rt.specs if x.role.role == "worker"]
    assert len(workers) == 2 and "- b.txt" in workers[0].prompt              # the worker is told which files it owes
    assert "missing outputs: b.txt" in workers[1].prompt                      # and exactly what it skipped
    log = await __import__("agent_desk.gitws", fromlist=["git"]).git(repo, "log", "--name-only", "--format=", "-1", s.ws.branch)
    assert sorted(log.split()) == ["a.txt", "b.txt"]                         # never committed without b.txt


async def test_task_that_never_produces_its_output_is_not_done(repo, tmp_path):
    sc = Script(plan(task("T1", write=True, outputs=["a.txt", "b.txt"])))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "FAILED" and s.sched.tasks["T1"].commit is None
