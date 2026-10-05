from agent_desk.contracts import Event
from agent_desk.tui.projection import Projection


def ev(seq, type_, payload=None, source="harness", task=None, run=None):
    return Event(session_id="s1", seq=seq, ts=f"2026-10-03T10:00:{seq:02d}.000Z", source=source, type=type_, task_id=task, agent_run_id=run, payload=payload or {})


PLAN = {"goal": "g", "constraints": [], "acceptance_criteria": ["AC"], "tasks": [{"id": "T1", "title": "t"}], "risks": [], "out_of_scope": []}


def test_idempotent_on_seq():
    p = Projection()
    assert p.apply(ev(1, "session.created", {"branch": "b"})) and not p.apply(ev(1, "session.created", {"branch": "other"}))
    assert p.branch == "b" and len(p.log) == 1
    assert not p.apply(ev(0, "session.planning"))                     # older than last_seq


def test_replay_twice_gives_same_state():
    seq = [ev(1, "session.created", {"branch": "b"}), ev(2, "session.planning"),
           ev(3, "plan.version", {"version": 1, "hash": "h1", "plan": PLAN}), ev(4, "plan.approved", {"version": 1, "hash": "h1"}),
           ev(5, "agent.started", {"role": "worker", "provider": "codex", "model": "m"}, "worker", "T1", "worker-T1-a1"),
           ev(6, "agent.finished", {"status": "completed", "usage": {"inputTokens": 10, "outputTokens": 5}}, "worker", "T1", "worker-T1-a1"),
           ev(7, "task.done", {"commit": "abc"}, task="T1")]
    a, b = Projection(), Projection()
    for e in seq: a.apply(e)
    for e in seq + seq: b.apply(e)
    assert (a.status, a.mode, a.tasks, a.tokens, a.plan_version) == (b.status, b.mode, b.tasks, b.tokens, b.plan_version)
    assert a.mode == "RUN" and a.tasks["T1"] == "done" and a.tokens["codex|worker"] == 15 and a.agents["worker-T1-a1"].status == "done"


def test_mode_transitions_and_final():
    p = Projection()
    p.apply(ev(1, "session.planning")); assert p.mode == "PLAN"
    p.apply(ev(2, "plan.approved", {"version": 1, "hash": "h"})); assert p.mode == "RUN"
    p.apply(ev(3, "final.summary", {"text": "ok", "ac_evidence": []})); assert (p.mode, p.status) == ("RESULT", "COMPLETED")
    q = Projection(); q.apply(ev(1, "plan.approved", {"version": 1, "hash": "h"})); q.finish("FAILED")
    assert (q.mode, q.status) == ("RESULT", "FAILED")


def test_questions_proposals_chat_and_decisions():
    p = Projection()
    p.apply(ev(1, "question.opened", {"id": "Q-1", "text": "scope?", "options": ["y", "n"], "evidence": "f"}))
    p.apply(ev(2, "proposal.created", {"material": True, "changes": [{"id": "AC-1"}]}))
    p.apply(ev(3, "chat.user", {"text": "hello", "item": "T2"})); p.apply(ev(4, "chat.main", {"text": "hi", "kind": "answer"}))
    assert p.pending["material"] and not p.questions["Q-1"]["answered"]
    p.apply(ev(5, "decision.recorded", {"id": "D-001", "text": "scope? -> n", "question": "Q-1"}))
    p.apply(ev(6, "proposal.rejected"))
    assert p.questions["Q-1"]["answered"] and p.pending is None and [c["who"] for c in p.chat] == ["you", "main", "sys"]


def test_decisions_architect_verify_quota():
    p = Projection()
    p.apply(ev(1, "decision.made", {"type": "retry_or_stop", "engine": "llm", "selected": "retry", "confidence": 0.9, "sharp": True}, task="T1"))
    p.apply(ev(2, "verification.result", {"name": "unit", "passed": False, "required": True, "fingerprint": "fp"}))
    p.apply(ev(3, "architect.reviewed", {"trigger": "error_repeats", "verdict": "revise", "findings": [{"message": "wrong fixture"}], "via_fallback": True}))
    p.apply(ev(4, "quota.hit", {"role": "worker", "provider": "claude", "reset": "3:45pm"}))
    p.apply(ev(5, "session.waiting_quota"))
    assert p.decisions_count == 1 and p.decisions_log[0]["selected"] == "retry" and not p.verify["unit"]["passed"]
    assert p.architect["advice"] == "wrong fixture" and p.architect["reviews"][0]["fallback"] and p.quota_hit["reset"] == "3:45pm" and p.status == "WAITING_QUOTA"


def test_noisy_events_do_not_flood_the_log():
    p = Projection()
    for i in range(1, 50):
        p.apply(ev(i, "usage.updated", {"inputTokens": 1}))
    assert p.log == [] and p.last_seq == 49


def test_accepting_a_proposal_clears_it():
    p = Projection()
    p.apply(ev(1, "plan.version", {"version": 1, "hash": "a", "plan": PLAN}))
    p.apply(ev(2, "proposal.created", {"material": False, "changes": []}))
    assert p.pending
    p.apply(ev(3, "plan.version", {"version": 2, "hash": "b", "plan": PLAN}))
    assert p.pending is None and p.plan_version == 2


def test_triage_and_invalidation_show_in_chat():
    p = Projection()
    p.apply(ev(1, "architect.triaged", {"trigger": "before_plan", "from": 1, "to": 2, "items": [
        {"finding_id": "F1", "verdict": "fixed", "reason": "ok"}, {"finding_id": "F3", "verdict": "rejected", "reason": "decided in D-001"},
        {"finding_id": "F4", "verdict": "question", "reason": "", "question": "Q-1"}]}))
    p.apply(ev(2, "architect.invalidated", {"version": 3}))
    assert p.chat[0]["text"] == "before_plan triage · v1→v2 · F1 fixed · F3 rejected: decided in D-001 · F4 question Q-1"
    assert "plan v3 changed" in p.chat[1]["text"]


def test_chat_stays_in_chat_mode_and_a_job_resets_only_per_job_state():
    p = Projection()
    for e in [ev(1, "session.created", {"branch": None, "worktree": None}), ev(2, "chat.user", {"text": "q"}, "user"),
              ev(3, "chat.main", {"kind": "answer", "text": "a"}, "main")]:
        p.apply(e)
    assert p.mode == "CHAT" and [m["who"] for m in p.chat] == ["you", "main"] and p.branch == ""
    p.apply(ev(4, "workspace.created", {"branch": "agent-desk/s1", "worktree": "/w"}))
    p.apply(ev(5, "job.started", {"job": 1, "kind": "plan", "brief": "x"}))
    p.apply(ev(6, "plan.version", {"version": 1, "hash": "h1", "plan": PLAN}))
    p.apply(ev(7, "plan.approved", {"version": 1, "hash": "h1", "by": "user"}))
    p.apply(ev(8, "task.done", {"commit": "abc"}, task="T1"))
    p.apply(ev(9, "final.summary", {"text": "ok", "ac_evidence": []}))
    assert p.branch == "agent-desk/s1" and p.mode == "RESULT" and p.tasks == {"T1": "done"}
    p.apply(ev(10, "job.started", {"job": 2, "kind": "quick", "brief": "y"}))
    assert p.plan is None and p.tasks == {} and p.summary is None and p.mode == "RUN" and p.approved is False
    assert p.chat[0]["text"] == "q" and p.chat[-1]["text"].startswith("— J2 quick")        # chat survives; a divider marks the new job
