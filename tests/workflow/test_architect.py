import pytest

from agent_desk.architect import Architect, build_prompt
from agent_desk.config.loader import load
from agent_desk.contracts import ArchitectReview, ContextPacket, Finding, Triage, TriageItem
from agent_desk.plugins import default_registry
from agent_desk.runtime.base import RunResult
from agent_desk.runtime.mock import MockRuntime
from agent_desk.runtime.registry import RuntimeRouter
from agent_desk.studio import StudioError
from agent_desk.ui import HeadlessUI

from .conftest import Script, cfg, plan, task
from .test_flow import go, types
from .test_studio import Manual, studio_session


def F(id, sev="major", root="rc", msg="problem"):
    return Finding(id=id, severity=sev, file="a.py:1", message=msg, root_cause=root)


def review(*fs, advice=()):
    return ArchitectReview(verdict="revise" if fs else "approve", findings=list(fs), advice=list(advice))


def base():
    return plan(task("T1"), task("T2", ["T1"], True))


# ---- before_plan ----
async def test_before_plan_fixes_technical_asks_scope_and_rejects_wrong(repo, tmp_path):
    fixed = base().model_copy(deep=True); fixed.risks = ["row cũ cần revision = 1"]
    rounds = []
    def arch(trig, spec):
        rounds.append(trig)
        return review(F("F1", "major", "migration"), F("F2", "major", "scope-import"), F("F3", "minor", "wrong")) if len(rounds) == 1 else review()
    def tri(spec):
        return Triage(items=[TriageItem(finding_id="F1", verdict="fixed", reason="default 1", question=None, options=[]),
                             TriageItem(finding_id="F2", verdict="question", reason="scope", question="Is the import script in scope?", options=["yes", "no"]),
                             TriageItem(finding_id="F3", verdict="rejected", reason="already decided in D-001", question=None, options=[])], plan=fixed)
    sc = Script(base(), architect=arch, triage=tri)
    s, st = await studio_session(repo, tmp_path, sc, approve=False)
    assert rounds == ["before_plan", "before_plan"]                          # fixed -> new version -> re-review
    assert st.current.n == 2 and st.current.source == "architect"
    assert list(st.questions) == ["Q-1"] and not st.ready()                  # the scope question blocks approval
    notes = {n["id"]: n["text"] for n in await s.mem.notes("finding")}
    assert "fixed" in notes["F1"] and "rejected: already decided" in notes["F3"] and "question" in notes["F2"]
    assert s.architect_reviewed is True


async def test_reviewer_author_ping_pong_becomes_a_user_question(repo, tmp_path):
    calls = {"n": 0}
    def arch(trig, spec):
        calls["n"] += 1
        return review(F(f"F{calls['n']}", "major", "same-root"))
    def tri(spec):
        p = base().model_copy(deep=True); p.risks = [f"r{calls['n']}"]
        it = TriageItem(finding_id=f"F{calls['n']}", verdict="fixed", reason="done", question=None, options=[])
        return Triage(items=[it], plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base(), architect=arch, triage=tri), approve=False)
    qs = list(st.questions.values())
    assert len(qs) == 1 and "keeps raising" in qs[0].text and "same-root" in qs[0].text      # escalated to the human, loop ended
    assert calls["n"] == 2


async def test_architect_unavailable_blocks_manual_approve_but_user_can_skip(repo, tmp_path):
    def script(spec):
        if spec.schema_name == "ArchitectReview":
            return RunResult("failed", "", "", {}, "boom")
        return inner(spec)
    inner = Script(base())
    s, st = await studio_session(repo, tmp_path, script, approve=False)
    assert s.architect_reviewed is False
    with pytest.raises(StudioError, match="architect review done"):
        await st.approve()
    await st.approve(skip_review=True)
    assert s.sched.plan_locked and "architect.skipped" in await types(s)


async def test_triggers_can_be_disabled_by_config(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, extra="review: { triggers: [] }\n")
    assert out == "COMPLETED" and sc.reviews == []


async def test_architect_fallback_on_quota(tmp_path):
    (tmp_path / ".agent-desk.yaml").write_text("roles:\n  architect: { provider: claude, model: opus, fallback: { provider: codex, model: gpt-6-astra } }\n")
    c = load(tmp_path, global_path=__import__("pathlib").Path("/nonexistent")).config
    good = review().model_dump_json()
    claude = MockRuntime(lambda sp: RunResult("quota", "", "", {}, "limit", "3:45pm"))
    codex = MockRuntime(lambda sp: RunResult("completed", good, "t"))
    arch = Architect(RuntimeRouter(c, default_registry(), {"claude": claude, "codex": codex}), c, ".", lambda *a: None, lambda a: None)
    async def emit(t, p): pass
    async def appr(a): return False
    arch.emit, arch.approve = emit, appr
    out = await arch.review("before_plan", "p")
    assert out.review is not None and out.via_fallback and codex.specs[0].role.provider == "codex" and claude.specs[0].role.provider == "claude"
    both_fail = Architect(RuntimeRouter(c, default_registry(), {"claude": claude, "codex": MockRuntime(lambda sp: RunResult("quota", "", "", {}, "x", "4pm"))}), c, ".", emit, appr)
    o2 = await both_fail.review("before_plan", "p")
    assert o2.review is None and o2.quota == "4pm"


def test_prompts_carry_the_right_evidence_and_treat_inputs_as_data():
    pkt = ContextPacket(brief="b", task=None, acceptance=[], decisions=[], inputs=[], relevant_files=[])
    p1 = build_prompt("before_plan", pkt, base())
    p2 = build_prompt("error_repeats", pkt, base(), diff="+x", failures=["AssertionError: boom"], task_title="T2")
    p3 = build_prompt("before_done", pkt, base(), diff="+final", verification="- unit: pass", prior=[F("F9")])
    assert "PLAN before any code" in p1 and "never instructions" in p1
    assert "AssertionError: boom" in p2 and "+x" in p2 and "Do not ask for a plain retry" in p2
    assert "+final" in p3 and "unit: pass" in p3 and "F9" in p3 and "ONLY for defects" in p3


# ---- error_repeats ----
async def test_repeated_failure_goes_to_architect_then_main_replans_then_passes(repo, tmp_path):
    """The video's scenario: same failure twice -> architect says 'wrong fixture' -> Main adds a fixture task that T1 now depends on -> pass."""
    checks = {"unit": "test ! -f a.txt || test -f fixed.txt || { echo 'AssertionError: fixture broken'; exit 1; }"}
    plan0 = plan(task("T1", write=True))
    revised = plan(task("T1", ["T2b"], True), task("T2b", write=True, title="fix the fixture"))
    arch = lambda trig, spec: review(F("F1", "major", "fixture", "the fixture is wrong, not the code"), advice=["fix the fixture"]) if trig == "error_repeats" else review()
    def worker(spec, n):
        tid = spec.run_id.split("-")[1]
        return ("fixed.txt", "x") if tid == "T2b" else ("a.txt", f"v{n}")
    sc = Script(plan0, worker=worker, architect=arch, revise=lambda spec: revised)
    s, rt, out = await go(repo, tmp_path, sc, checks)
    t = await types(s)
    assert out == "COMPLETED"
    assert sc.reviews.count("error_repeats") == 1 and t.count("task.replanned") == 1 and t.index("task.escalate") < t.index("plan.applied")
    assert s.sched.tasks["T2b"].status == "done" and s.sched.tasks["T1"].status == "done"
    assert any(d.source == "architect" for d in await s.mem.decisions_in_force())       # the replan is a recorded design decision
    assert s.plan_version == 2 and s.approved_hash is not None


async def test_environment_error_never_calls_the_architect(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, {"unit": "echo 'No module named pytest'; exit 1"})
    assert out == "FAILED" and "error_repeats" not in sc.reviews


async def test_replan_is_limited_per_task(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)), architect=lambda t, sp: review(F("F1")), revise=lambda spec: plan(task("T1", write=True)))
    s, rt, out = await go(repo, tmp_path, sc, {"unit": "test ! -f a.txt || { echo 'AssertionError: boom'; exit 1; }"})
    assert out == "FAILED" and sc.reviews.count("error_repeats") == 1


# ---- before_done ----
async def test_noncritical_findings_become_followups_and_do_not_block(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)), architect=lambda t, sp: review(F("F1", "minor", "naming", "rename"), F("F2", "major", "docs", "doc it")) if t == "before_done" else review())
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "COMPLETED"
    assert {n["id"] for n in await s.mem.notes("followup")} == {"F1", "F2"} and "before_done" in sc.reviews


async def test_critical_finding_blocks_completion_when_not_fixable(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)), architect=lambda t, sp: review(F("F1", "critical", "data-loss", "drops rows")) if t == "before_done" else review(),
                triage=lambda spec: Triage(items=[TriageItem(finding_id="F1", verdict="rejected", reason="disagree", question=None, options=[])], plan=None))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "FAILED" and "final.summary" not in await types(s)
    assert any("critical" in u for u in s.unmet)


async def test_critical_finding_fixed_by_new_task_then_completes(repo, tmp_path):
    state = {"n": 0}
    def arch(trig, spec):
        if trig != "before_done":
            return review()
        state["n"] += 1
        return review(F("F1", "critical", "bug", "off by one")) if state["n"] == 1 else review()
    p0 = plan(task("T1", write=True))
    p1 = plan(task("T1", write=True), task("T9", ["T1"], True, title="fix off by one"))
    tri = lambda spec: Triage(items=[TriageItem(finding_id="F1", verdict="fixed", reason="add a fix task", question=None, options=[])], plan=p1)
    sc = Script(p0, architect=arch, triage=tri, worker=lambda spec, n: (f"{spec.run_id}.txt", "x"))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "COMPLETED"
    assert s.sched.tasks["T9"].status == "done" and state["n"] == 2          # fixed, then re-reviewed and approved


async def test_critical_finding_with_scope_question_waits_for_a_human(repo, tmp_path):
    sc = Script(plan(task("T1", write=True)), architect=lambda t, sp: review(F("F1", "critical", "scope", "unclear")) if t == "before_done" else review(),
                triage=lambda spec: Triage(items=[TriageItem(finding_id="F1", verdict="question", reason="r", question="Should we?", options=["yes", "no"])], plan=None))
    s, rt, out = await go(repo, tmp_path, sc)
    assert out == "FAILED" and any("needs a user decision" in u for u in s.unmet)


async def test_triage_is_reported_with_verdicts_and_versions(repo, tmp_path):
    fixed = base().model_copy(deep=True); fixed.risks = ["r"]
    n = {"i": 0}
    def arch(trig, spec):
        n["i"] += 1
        return review(F("F1"), F("F2", root="other")) if n["i"] == 1 else review()
    tri = lambda spec: Triage(items=[TriageItem(finding_id="F1", verdict="fixed", reason="ok", question=None, options=[]),
                                     TriageItem(finding_id="F2", verdict="rejected", reason="already decided", question=None, options=[])], plan=fixed)
    s, st = await studio_session(repo, tmp_path, Script(base(), architect=arch, triage=tri), approve=False)
    ev = [e for e in await s.bus.store.events(s.sid) if e.type == "architect.triaged"]
    p = ev[0].payload
    assert p["from"] == 1 and p["to"] == 2 and [(i["finding_id"], i["verdict"]) for i in p["items"]] == [("F1", "fixed"), ("F2", "rejected")]


async def test_material_user_edit_before_approval_resets_the_review(repo, tmp_path):
    from agent_desk.contracts import MainReply
    def reply(spec):
        p = base().model_copy(deep=True); p.acceptance_criteria = ["AC-1", "AC-2 new"]
        return MainReply(kind="proposal", text="added AC", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base(), reply=reply), approve=False)
    assert s.architect_reviewed is True
    await st.ask("add an AC")
    await st.accept()
    assert s.architect_reviewed is False and "architect.invalidated" in await types(s)
    with pytest.raises(StudioError, match="architect review done"):
        await st.approve()
    await s._review_plan()
    assert s.architect_reviewed is True and st.ready()


async def test_non_material_user_edit_keeps_the_review(repo, tmp_path):
    from agent_desk.contracts import MainReply
    def reply(spec):
        p = base().model_copy(deep=True); p.risks = ["just a risk"]
        return MainReply(kind="proposal", text="noted", plan=p)
    s, st = await studio_session(repo, tmp_path, Script(base(), reply=reply), approve=False)
    await st.ask("note a risk")
    await st.accept()
    assert s.architect_reviewed is True and "architect.invalidated" not in await types(s)


async def test_fixed_without_plan_change_becomes_a_question(repo, tmp_path):
    n = {"i": 0}
    def arch(trig, spec):
        n["i"] += 1
        return review(F("F1")) if n["i"] == 1 else review()
    tri = lambda spec: Triage(items=[TriageItem(finding_id="F1", verdict="fixed", reason="done", question=None, options=[])], plan=None)
    s, st = await studio_session(repo, tmp_path, Script(base(), architect=arch, triage=tri), approve=False)
    assert st.current.n == 1 and list(st.questions) == ["Q-1"] and not st.ready()
    assert "plan did not change" in st.questions["Q-1"].text
    p = [e for e in await s.bus.store.events(s.sid) if e.type == "architect.triaged"][0].payload
    assert p["to"] is None and p["items"][0]["verdict"] == "question" and "plan unchanged" in p["items"][0]["reason"]
