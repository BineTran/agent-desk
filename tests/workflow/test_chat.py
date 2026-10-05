import json

from agent_desk.contracts import Plan
from agent_desk.gitws import git
from agent_desk.graph import load_session, open_chat, open_session, run_graph, run_turn
from agent_desk.runtime.mock import MockRuntime
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, qtask, route, task


async def chat(repo, tmp_path, script, checks=None, extra=""):
    rt = MockRuntime(script)
    return await open_chat(repo, "hi", cfg(repo, checks, extra), rt, HeadlessUI(), tmp_path / "home"), rt


async def types(s):
    return [e.type for e in await s.bus.store.events(s.sid)]


def roles(rt):
    return [x.role.role for x in rt.specs]


async def test_answer_creates_no_worktree_and_reads_the_checkout(repo, tmp_path):
    s, rt = await chat(repo, tmp_path, Script(plan(task("T1")), route=lambda sp: route("answer", "add() sums two numbers")))
    assert await run_turn(s, "what does add do?") == "ANSWERED"
    assert s.ws is None and roles(rt) == ["main"] and rt.specs[0].cwd == str(repo.resolve())
    assert "agent-desk/" not in await git(repo, "branch", "--list")
    assert "workspace.created" not in await types(s) and "plan.version" not in await types(s)
    assert (await s.bus.store.events(s.sid))[-1].payload["text"] == "add() sums two numbers"
    await s.close()


async def test_quick_runs_one_worker_without_clarify_plan_or_architect(repo, tmp_path):
    sc = Script(plan(task("T1")), route=lambda sp: route("quick", "rename", task=qtask("a.txt")))
    s, rt = await chat(repo, tmp_path, sc)
    assert await run_turn(s, "create a.txt") == "COMPLETED"
    assert sorted(set(roles(rt))) == ["main", "worker"]
    assert [x.schema_name for x in rt.specs if x.role.role == "main"] == ["Route"]
    t = await types(s)
    assert "architect.reviewed" not in t and "clarify.asked" not in t and t.count("plan.version") == 1
    assert next(e for e in await s.bus.store.events(s.sid) if e.type == "plan.approved").payload["by"] == "route"
    assert (s.ws.path / "a.txt").exists() and not (repo / "a.txt").exists()
    assert len((await git(repo, "log", "--oneline", s.ws.branch)).splitlines()) == 2
    await s.close()


async def test_quick_is_still_blocked_by_failing_verification(repo, tmp_path):
    sc = Script(plan(task("T1")), route=lambda sp: route("quick", task=qtask("a.txt")))
    s, rt = await chat(repo, tmp_path, sc, extra="chat: { quick_review: [] }\n", checks={"unit": "test ! -f a.txt"})   # fails only because of what the task adds
    assert await run_turn(s, "create a.txt") != "COMPLETED"
    assert "task.done" not in await types(s)
    await s.close()


async def test_guard_upgrades_quick_to_plan_for_keywords_and_many_files(repo, tmp_path):
    for text, t in (("push this to origin", qtask("a.txt")), ("do it", qtask("a", "b", "c", "d")), ("do it", qtask("src/*.py"))):
        s, rt = await chat(repo, tmp_path / text[:3] / str(len(t.outputs)), Script(plan(task("T1")), route=lambda sp, t=t: route("quick", task=t)))
        r = await s.route(text)
        assert r.kind == "plan" and r.task is None
        assert any(e.type == "chat.routed" and e.payload["by"] == "rule" for e in await s.bus.store.events(s.sid))
        await s.close()


async def test_route_plan_config_skips_main_routing(repo, tmp_path):
    s, rt = await chat(repo, tmp_path, Script(plan(task("T1"))), extra="chat: { route: plan }\n")
    r = await s.route("anything")
    assert r.kind == "plan" and not rt.specs
    await s.close()


async def test_ask_never_starts_work_even_if_main_proposes_it(repo, tmp_path):
    s, rt = await chat(repo, tmp_path, Script(plan(task("T1")), route=lambda sp: route("quick", "x", task=qtask())))
    r = await s.route("explain", forced="answer")
    assert r.kind == "answer" and s.ws is None
    await s.close()


async def test_cascade_chat_answers_trivia_and_escalates_the_rest(repo, tmp_path):
    extra = "chat: { triage: cascade }\nroles:\n  chat: { enabled: true }\n"
    sc = Script(plan(task("T1")), chat=lambda sp: route("answer", "hello!") if "hi" in sp.prompt.split("User message")[1][:12] else route("escalate", ""),
                route=lambda sp: route("answer", "from main"))
    s, rt = await chat(repo, tmp_path, sc, extra=extra)
    assert (await s.route("hi")).text == "hello!" and roles(rt) == ["chat"]
    assert (await s.route("explain the architecture")).text == "from main" and roles(rt) == ["chat", "chat", "main"]
    await s.close()


async def test_answer_then_quick_then_plan_share_one_branch(repo, tmp_path):
    n = {"i": 0}
    def r(sp):
        n["i"] += 1
        return [route("answer", "it adds"), route("quick", "one", task=qtask("a.txt", title="add a")),
                route("quick", "two", task=qtask("b.txt", title="add b"))][n["i"] - 1]
    sc = Script(plan(task("T1")), route=r, worker=lambda sp, k: ("a.txt", "1") if "a.txt" in sp.prompt else ("b.txt", "2"))
    s, rt = await chat(repo, tmp_path, sc)
    assert await run_turn(s, "q") == "ANSWERED" and s.ws is None
    assert await run_turn(s, "make a.txt") == "COMPLETED" and s.job == 1
    assert await run_turn(s, "make b.txt") == "COMPLETED" and s.job == 2
    assert len((await git(repo, "log", "--oneline", s.ws.branch)).splitlines()) == 3
    assert any(x.run_id.startswith("worker-T1-a1-j2") for x in rt.specs)
    ev = await s.bus.store.events(s.sid)
    assert [e.type for e in ev].count("job.started") == 2 and [e.type for e in ev].count("job.finished") == 2
    assert [x["id"] for x in await s.mem.notes("job")] == ["J1", "J2"] and "J1" in await __import__("agent_desk.context.render", fromlist=["render"]).render(s.mem)
    await s.close()


async def test_workspace_creation_rotates_main_and_main_then_reads_the_worktree(repo, tmp_path):
    n = {"i": 0}
    def r(sp):
        n["i"] += 1
        return route("quick", "x", task=qtask()) if n["i"] == 2 else route("answer", "ok")
    s, rt = await chat(repo, tmp_path, Script(plan(task("T1")), route=r))
    await run_turn(s, "first")
    await run_turn(s, "make a.txt")
    await run_turn(s, "third")
    mains = [x for x in rt.specs if x.role.role == "main"]
    assert mains[0].cwd == mains[1].cwd == str(repo.resolve())          # routing happens before the worktree exists
    assert mains[2].cwd == str(s.ws.path) and mains[2].thread_id is None and mains[0].thread_id is None
    assert "main.rotated" in await types(s)
    await s.close()


async def test_leftovers_of_a_failed_job_are_stashed_not_committed(repo, tmp_path):
    n = {"i": 0}
    def r(sp):
        n["i"] += 1
        return route("quick", "x", task=qtask("a.txt" if n["i"] == 1 else "b.txt"))
    sc = Script(plan(task("T1")), route=r, worker=lambda sp, k: ("junk.txt", "j") if "a.txt" in sp.prompt else ("b.txt", "b"))
    s, rt = await chat(repo, tmp_path, sc, extra="chat: { quick_review: [] }\n")
    assert await run_turn(s, "one") != "COMPLETED"            # promised a.txt, wrote junk.txt
    assert await run_turn(s, "two") == "COMPLETED"
    assert "workspace.stashed" in await types(s)
    files = (await git(repo, "show", "--name-only", "--format=", s.ws.branch)).split()
    assert files == ["b.txt"] and not (s.ws.path / "junk.txt").exists()
    await s.close()


async def test_stop_during_quick_makes_no_commit(repo, tmp_path):
    sc = Script(plan(task("T1")), route=lambda sp: route("quick", "x", task=qtask()))
    rt = MockRuntime(sc, delay=0.3)
    s = await open_chat(repo, "hi", cfg(repo), rt, HeadlessUI(), tmp_path / "home")
    import asyncio
    async def stopper():
        while not s.active_runs:
            await asyncio.sleep(0.02)
        await s.stop()
    t = asyncio.create_task(stopper())
    assert await run_turn(s, "make a.txt") in ("CANCELLED", "FAILED", "INTERRUPTED")
    await t
    assert len((await git(repo, "log", "--oneline", s.ws.branch)).splitlines()) == 1
    await s.close()


async def test_reopen_chat_only_and_after_two_jobs(repo, tmp_path):
    n = {"i": 0}
    def r(sp):
        n["i"] += 1
        return route("answer", "ok") if n["i"] == 1 else route("quick", "x", task=qtask("a.txt" if n["i"] == 2 else "b.txt"))
    sc = Script(plan(task("T1")), route=r, worker=lambda sp, k: ("a.txt", "1") if "a.txt" in sp.prompt else ("b.txt", "2"))
    s, rt = await chat(repo, tmp_path, sc)
    await run_turn(s, "q")
    sid = s.sid
    await s.close()
    s2 = await load_session(sid, cfg(repo), MockRuntime(sc), HeadlessUI(), tmp_path / "home")
    assert s2.ws is None and s2.outcome == "IDLE" and s2.job == 0
    await run_turn(s2, "a")
    await run_turn(s2, "b")
    await s2.close()
    s3 = await load_session(sid, cfg(repo), MockRuntime(sc), HeadlessUI(), tmp_path / "home")
    assert s3.job == 2 and s3.job_kind == "quick" and s3.studio.locked and list(s3.sched.tasks) == ["T1"]
    assert s3.ws.base_commit == s3.job_base and s3.ws.session_base != s3.job_base
    await s3.close()
