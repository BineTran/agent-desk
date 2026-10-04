import pytest

from agent_desk.contracts import MainReply
from agent_desk.graph import open_session
from agent_desk.runtime.mock import MockRuntime
from agent_desk.studio import Question, StudioError, diff_plans
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, task


class Manual(HeadlessUI):
    """Lets the test drive the studio itself; approve=True locks the plan like a user typing /approve."""
    def __init__(self, approve): self.studio, self.approve = None, approve
    async def run_studio(self, studio):
        self.studio = studio
        if self.approve:
            await studio.approve()
        return True


async def studio_session(repo, tmp_path, script, checks=None, approve=True):
    ui = Manual(approve)
    s = await open_session(repo, "feature X", cfg(repo, checks), MockRuntime(script), ui, tmp_path / "home")
    assert await s.plan_phase()
    return s, ui.studio


def base_plan():
    return plan(task("T1"), task("T2", ["T1"], True))


# ---- pure diff / classification ----
def test_diff_classifies_material_vs_not():
    a = base_plan()
    b = a.model_copy(deep=True)
    b.risks = ["new risk"]
    b.tasks[1].title = "renamed"
    d = diff_plans(a, b)
    assert not d.material and {c.id for c in d.changes} == {"R-1", "T2"}
    c = a.model_copy(deep=True); c.acceptance_criteria = ["AC-1", "AC-2 new"]
    assert diff_plans(a, c).material
    e = a.model_copy(deep=True); e.tasks[1].depends_on = []
    assert diff_plans(a, e).material
    f = a.model_copy(deep=True); f.tasks.append(task("T3"))
    assert [(x.id, x.op) for x in diff_plans(a, f).changes] == [("T3", "add")]
    assert diff_plans(a, a).empty


# ---- conversation ----
async def test_discuss_does_not_change_plan_or_create_version(repo, tmp_path):
    sc = Script(base_plan(), reply=lambda spec: MainReply(kind="answer", text="because X (a.py:3)", plan=None))
    s, st = await studio_session(repo, tmp_path, sc)
    r = await st.ask("why updateMany?")
    assert r.kind == "answer" and len(st.versions) == 1 and st.pending is None
    assert "why updateMany?" in sc.prompts[-1]


async def test_comment_proposal_then_accept_creates_version_and_verbatim_decision(repo, tmp_path):
    def reply(spec):
        p = base_plan().model_copy(deep=True); p.tasks[1].goal = "do T2 including concurrent bulk case"
        return MainReply(kind="proposal", text="added the concurrent case", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=reply))
    r = await st.ask("Add the case where bulk and PATCH run together", item="T2")
    assert st.pending and st.pending.diff.material and st.pending.item == "T2"
    assert len(st.versions) == 1                                             # nothing changes until the user accepts
    v = await st.accept()
    assert v.n == 2 and "concurrent" in st.current.plan.tasks[1].goal and st.pending is None
    ds = await s.mem.decisions_in_force()
    assert ds[-1].verbatim == "Add the case where bulk and PATCH run together" and ds[-1].source == "user"


async def test_reject_leaves_plan_untouched_and_second_proposal_blocked_while_pending(repo, tmp_path):
    def reply(spec):
        p = base_plan().model_copy(deep=True); p.risks = ["r"]
        return MainReply(kind="proposal", text="t", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=reply))
    await st.ask("x")
    with pytest.raises(StudioError, match="proposal is waiting"):
        await st.ask("y")
    await st.reject()
    assert st.pending is None and len(st.versions) == 1


async def test_noop_proposal_is_downgraded_to_answer(repo, tmp_path):
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=lambda spec: MainReply(kind="proposal", text="same", plan=base_plan())))
    r = await st.ask("change nothing")
    assert r.kind == "answer" and st.pending is None


async def test_chat_never_reaches_agent_packets_but_pins_do(repo, tmp_path):
    sc = Script(base_plan(), reply=lambda spec: MainReply(kind="answer", text="SECRET-CHAT-ANSWER", plan=None))
    s, st = await studio_session(repo, tmp_path, sc)
    await st.ask("a question with UNIQUE-USER-WORDS")
    await st.pin("pinned: use updateMany")
    ev = [e.type for e in await s.bus.store.events(s.sid)]
    assert "chat.user" in ev and "chat.main" in ev                          # in the audit log
    from agent_desk.context import builder
    for role in ("worker", "explorer", "main"):
        pkt = (await builder.build(s.mem, role, base_plan().tasks[1])).model_dump_json()
        assert "UNIQUE-USER-WORDS" not in pkt and "SECRET-CHAT-ANSWER" not in pkt
    assert "pinned: use updateMany" in (s.home / "x").parent.name or "N-1" in str(await s.mem.notes("note"))


# ---- readiness / approval ----
async def test_approve_refused_with_reasons_until_questions_answered(repo, tmp_path):
    s, st = await studio_session(repo, tmp_path, Script(base_plan()), approve=False)
    await st.add_question(Question("Q-4", "Is the import script in scope?", ["yes", "no"], "scripts/import.ts:40"))
    assert not st.ready()
    with pytest.raises(StudioError, match="Q-4"):
        await st.approve()
    assert not s.sched.plan_locked                                          # nothing may run
    d = await st.answer("Q-4", option=1)
    assert d.verbatim == "no" and st.ready()
    await st.approve()
    assert s.sched.plan_locked and s.approved_hash == st.current.hash


async def test_approve_requires_acceptance_criteria_and_no_pending_proposal(repo, tmp_path):
    def reply(spec):
        p = base_plan().model_copy(deep=True); p.risks = ["r"]
        return MainReply(kind="proposal", text="t", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=reply), approve=False)
    await st.ask("x")
    with pytest.raises(StudioError, match="pending proposal"):
        await st.approve()


# ---- after approval ----
async def test_non_material_message_during_run_is_auto_applied_material_waits(repo, tmp_path):
    flip = {"material": False}
    def reply(spec):
        p = base_plan().model_copy(deep=True)
        if flip["material"]:
            p.acceptance_criteria = ["AC-1", "new AC"]
        else:
            p.risks = ["watch the migration"]
        return MainReply(kind="proposal", text="ok", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=reply))
    h0 = s.approved_hash
    await s.user_message("note a risk")
    assert s.studio.pending is None and s.plan_version == 2 and s.approved_hash != h0       # auto-acknowledged, new hash
    applied = [e for e in await s.bus.store.events(s.sid) if e.type == "plan.applied"][-1]
    assert applied.payload["acknowledged"] == "auto" and applied.payload["material"] is False
    flip["material"] = True
    await s.user_message("add an acceptance criterion")
    assert s.studio.pending is not None and s.plan_version == 2                                # waits for the user
    await s.studio.accept()
    assert s.plan_version == 3
    assert [e for e in await s.bus.store.events(s.sid) if e.type == "plan.applied"][-1].payload["acknowledged"] == "user"


async def test_material_change_marks_finished_task_stale_and_removed_dropped(repo, tmp_path):
    def reply(spec):
        p = base_plan().model_copy(deep=True)
        p.tasks[0].goal = "investigate differently"; p.tasks = [p.tasks[0]]          # T1 modified, T2 removed
        return MainReply(kind="proposal", text="t", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=reply))
    s.sched.start("T1"); s.sched.finish("T1", True)
    await s.user_message("rethink")
    await st.accept()
    assert s.sched.tasks["T1"].status == "stale" and s.sched.tasks["T2"].status == "dropped"
    assert not s.sched.all_done()                                                    # dropped tasks don't count, stale ones must rerun


async def test_stop_message_cancels_scheduler(repo, tmp_path):
    s, st = await studio_session(repo, tmp_path, Script(base_plan(), reply=lambda spec: MainReply(kind="stop", text="stopping", plan=None)))
    await s.user_message("stop everything")
    assert s.sched.cancelled


async def test_main_thread_rotates_with_session_memory_after_approval(repo, tmp_path):
    s, st = await studio_session(repo, tmp_path, Script(base_plan()))
    assert s.reasoner.thread_id is None and "Decisions in force" in (s.reasoner.seed or "")      # new thread, seeded from L2 memory
    s2, st2 = await studio_session(repo, tmp_path / "b", Script(base_plan()), approve=False)
    assert s2.reasoner.seed is None                                                            # not rotated while still planning
