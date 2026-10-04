"""M7: reopening a persisted session, continuing it, and not reviewing/verifying read-only work."""
import asyncio

from agent_desk.graph import load_session, open_session, run_graph
from agent_desk.runtime.mock import MockRuntime
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, task


async def types(s):
    return [e.type for e in await s.bus.store.events(s.sid)]


async def test_read_only_plan_skips_final_review_and_verification(repo, tmp_path):
    sc = Script(plan(task("T1")))
    s = await open_session(repo, "review the ranking flow", cfg(repo, {"unit": "exit 1"}), MockRuntime(sc), HeadlessUI(), tmp_path / "home")
    assert await run_graph(s) == "COMPLETED"                       # the failing check never ran: nothing was written
    assert "before_done" not in sc.reviews and "architect.skipped" in await types(s)


async def test_reopen_after_failure_continues_only_unfinished_tasks_on_the_same_main_thread(repo, tmp_path):
    checks = {"unit": "test ! -f a.txt || grep -q good a.txt || { echo 'AssertionError: not good'; exit 1; }"}
    home = tmp_path / "home"
    s = await open_session(repo, "feature X", cfg(repo, checks), MockRuntime(Script(plan(task("T1"), task("T2", ["T1"], True)),
                                                                                 worker=lambda spec, n: ("a.txt", "bad"))), HeadlessUI(), home)
    assert await run_graph(s) == "FAILED"
    await s.close()

    sc = Script(plan(task("T1"), task("T2", ["T1"], True)), worker=lambda spec, n: ("a.txt", "good"))
    rt = MockRuntime(sc)
    s2 = await load_session(s.sid, cfg(repo, checks), rt, HeadlessUI(), home)
    assert s2.ws.path == s.ws.path and s2.studio.locked and s2.sched.tasks["T1"].status == "done" and s2.sched.tasks["T2"].status == "pending"
    assert s2.reasoner.thread_id == s.reasoner.thread_id and s2.generation == 1
    assert await s2.resume_after_quota() == "COMPLETED"
    ran = [x.run_id for x in rt.specs]
    assert not any(r.startswith("explorer-T1") for r in ran) and any(r.startswith("worker-T2") and r.endswith("-r1") for r in ran)
    assert rt.specs and all(x.thread_id == s.reasoner.thread_id for x in rt.specs if x.role.role == "main")
    t = await types(s2)
    assert "session.reopened" in t and t[-1] != "error"
    await s2.close()


async def test_reopen_while_planning_goes_back_to_the_studio_without_replanning(repo, tmp_path):
    class Hang(HeadlessUI):
        async def run_studio(self, studio):
            await asyncio.Event().wait()
    home = tmp_path / "home"
    s = await open_session(repo, "feature X", cfg(repo), MockRuntime(Script(plan(task("T1")))), Hang(), home)
    g = asyncio.create_task(run_graph(s))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if s.studio and s.studio.versions:
            break
    g.cancel()
    await s.close()                                                 # the process "died" while the user was in the plan studio

    rt = MockRuntime(Script(plan(task("T1"))))
    s2 = await load_session(s.sid, cfg(repo), rt, HeadlessUI(), home)
    assert s2.outcome == "INTERRUPTED" and not s2.studio.locked and len(s2.studio.versions) == 1
    assert await run_graph(s2) == "COMPLETED"
    assert not [x for x in rt.specs if x.schema_name in ("Questions", "Plan")]     # no clarify / plan again
    await s2.close()
