from io import StringIO
from pathlib import Path

from rich.console import Console

from agent_desk.config.check import ProviderInfo, check
from agent_desk.config.loader import load
from agent_desk.contracts import Event
from agent_desk.plugins import default_registry
from agent_desk.tui import views
from agent_desk.tui.projection import Projection
from agent_desk.tui.status import ProviderStatus, StatusInfo

NOG = Path("/nonexistent")


def txt(r, w=140):
    c = Console(file=StringIO(), width=w, force_terminal=False, color_system=None)
    c.print(r)
    return c.file.getvalue()


def ev(seq, t, p=None, source="harness", task=None, run=None):
    return Event(session_id="s1", seq=seq, ts=f"2026-10-03T10:00:{seq:02d}.000Z", source=source, type=t, task_id=task, agent_run_id=run, payload=p or {})


PLAN = {"goal": "G", "constraints": [], "acceptance_criteria": ["AC one", "AC two"], "risks": ["R one"], "out_of_scope": ["OUT one"],
        "tasks": [{"id": "T1", "title": "find", "kind": "investigation", "depends_on": []}, {"id": "T2", "title": "do", "kind": "implementation", "depends_on": ["T1"]}]}


def status(**kw):
    return StatusInfo({"codex": ProviderStatus("codex", "codex", True, "chatgpt plus", "codex 0.159", {"primary": 12.0, "secondary": 3.0}),
                       "claude": ProviderStatus("claude", "claude-cli", False, login_hint="claude auth login", quota={"primary": 100.0})})


def test_status_bar_shows_login_quota_and_decision_engines():
    cfg = load(None, global_path=NOG).config
    s = txt(views.status_bar(cfg, status(), ["claude roles → fallback codex (session)"]))
    assert "codex login" in s and "claude not logged in" in s and "12%" in s and "100%" in s
    assert "decision rules+haiku" in s and "fallback codex (session)" in s and "F2 config" in s


def test_runs_with_summarises_every_role():
    s = txt(views.runs_with(load(None, global_path=NOG).config))
    for needle in ("Main", "gpt-6.1-sol", "Explorer", "gpt-5.6-luna", "Worker", "Architect", "gpt-6-astra", "Decision rules+haiku"):
        assert needle in s


def test_plan_view_marks_pending_changes_questions_and_approval():
    p = Projection()
    p.apply(ev(1, "plan.version", {"version": 2, "hash": "h", "plan": PLAN}))
    p.apply(ev(2, "question.opened", {"id": "Q-4", "text": "import script in scope?", "options": ["yes", "no"], "evidence": "x"}))
    p.apply(ev(3, "proposal.created", {"material": True, "changes": [{"id": "AC-2", "op": "modify", "before": "a", "after": "b", "material": True}]}))
    s = txt(views.plan_view(p, "T2"))
    assert "PLAN v2" in s and "proposal pending" in s and "AC one" in s and "T2" in s and "← T1" in s
    assert "Open questions" in s and "Q-4" in s and "blocks /approve" in s
    p.apply(ev(4, "plan.approved", {"version": 2, "hash": "h"}))
    assert "approved ✓" in txt(views.plan_view(p))


def test_chat_messages_and_proposal_card():
    p = Projection()
    p.apply(ev(2, "chat.user", {"text": "hello"})); p.apply(ev(3, "chat.main", {"text": "hi", "kind": "proposal"}))
    p.apply(ev(4, "proposal.created", {"material": False, "changes": [{"id": "R-1", "op": "add", "after": "new risk", "material": False}]}))
    p.apply(ev(5, "architect.reviewed", {"trigger": "before_plan", "verdict": "revise", "findings": [{"id": "F1", "severity": "major", "file": "a.py:3", "message": "raw SQL"}]}))
    s = txt(views.chat_view(p))
    assert "you" in s and "[proposal]" in s and "architect" in s and "F1" in s and "raw SQL" in s
    s = txt(views.proposal_view(p.pending, True))
    assert "non-material" in s and "[a] accept" in s and "new risk" in s


def test_live_view_shows_label_activity_reasoning_and_reply():
    import time
    p = Projection()
    p.apply(ev(1, "agent.started", {"role": "main", "provider": "codex", "model": "m", "label": "plan"}, "main", None, "main-1"))
    p.apply(ev(0, "live.reasoning", {"delta": "Looking at the scheduler"}, "main", None, "main-1"))
    p.apply(ev(0, "live.text", {"delta": '{"kind":"answer","text":"Because it is atomic'}, "main", None, "main-1"))
    p.apply(ev(2, "tool.started", {"command": "rg updateMany"}, "main", None, "main-1"))
    s = txt(views.bubble_view(p, time.monotonic(), 0))
    assert "main · plan · 00:00" in s and "$ rg updateMany" in s and "Looking at the scheduler" in s and "Because it is atomic" in s
    p.agents["main-1"].last_activity -= 200
    assert "no output for 03:20" in txt(views.bubble_view(p, time.monotonic(), 0))


def test_run_view_decisions_agents_and_architect():
    p = Projection()
    p.apply(ev(1, "decision.made", {"type": "route", "engine": "rule", "selected": "worker"}, task="T1"))
    p.apply(ev(2, "decision.made", {"type": "retry_or_stop", "engine": "llm", "selected": "retry", "confidence": 0.86, "sharp": True}, task="T1"))
    p.apply(ev(3, "agent.started", {"role": "worker", "provider": "claude", "model": "sonnet"}, "worker", "T2", "worker-T2-a1"))
    p.apply(ev(4, "tool.started", {"command": "pnpm test"}, "worker", "T2", "worker-T2-a1"))
    p.apply(ev(5, "verification.result", {"name": "unit", "passed": False, "required": True}))
    p.apply(ev(6, "architect.reviewed", {"trigger": "error_repeats", "verdict": "revise", "findings": [{"message": "wrong fixture, not the code"}]}))
    s = txt(views.run_view(p, views.pv("codex", "gpt-6.1-sol")), 160)
    assert "route T1 → worker" in s and "deterministic" in s and "retry_or_stop T1 → retry" in s and "█" in s
    assert "spawn agents" in s and "agents [1/6]" in s and "review + verify" in s
    assert "0.86" in s and "sharp" in s and "claude" in s and "sonnet" in s and "running" in s and "pnpm test" in s
    assert "✗ unit" in s and "wrong fixture" in s and "✓ error repeats" in s and "DECISION" in s


def test_result_view_failed_quota_and_completed():
    cfg = load(None, global_path=NOG).config
    p = Projection()
    p.apply(ev(1, "agent.started", {"role": "worker", "provider": "claude", "model": "sonnet"}, "worker", "T1", "w1"))
    p.apply(ev(2, "agent.finished", {"status": "completed", "usage": {"inputTokens": 1000, "outputTokens": 234}}, "worker", "T1", "w1"))
    p.apply(ev(3, "decision.recorded", {"id": "D-001", "text": "409 returns currentRevision", "source": "user"}))
    p.apply(ev(4, "completion.gate", {"unmet": ["required check failed: unit"]}))
    p.finish("FAILED")
    s = txt(views.result_view(p, cfg, {"files": ["a.py"], "commits": ["abc123 T1"]}))
    assert "FAILED" in s and "Not complete" in s and "required check failed: unit" in s and "1,234 tokens" in s and "D-001" in s and "abc123 T1" in s
    q = Projection(); q.apply(ev(1, "quota.hit", {"provider": "claude", "reset": "3:45pm"})); q.finish("WAITING_QUOTA")
    assert "resets 3:45pm" in txt(views.result_view(q, cfg, {}))
    r = Projection(); r.apply(ev(1, "final.summary", {"text": "All good", "ac_evidence": ["AC-1: unit pass"]}))
    assert "All good" in txt(views.result_view(r, cfg, {})) and "AC-1: unit pass" in txt(views.result_view(r, cfg, {}))


def test_components_view_has_all_groups_with_sources_and_problems(tmp_path):
    (tmp_path / ".agent-desk.yaml").write_text("roles:\n  worker: { provider: claude, tier: strong }\nverification:\n  unit: { command: pnpm test }\n")
    loaded = load(tmp_path, global_path=NOG)
    reg = default_registry()
    infos = {"codex": ProviderInfo({"gpt-6.1-sol": [], "gpt-5.6-luna": [], "gpt-6-astra": []}, True, True), "claude": ProviderInfo({"sonnet": [], "haiku": [], "opus": []}, False, False, login_hint="claude auth login")}
    rows = check(loaded.config, reg, lambda n: infos.get(n), env={})
    s = txt(views.components_view(loaded, status(), reg, rows), 150)
    for g in ("Providers", "Roles", "Decision points", "Review (Architect)", "Safety", "Verification", "Plugins", "Problems"):
        assert g in s
    assert any(l.startswith(" worker") and " repo " in l for l in s.splitlines())          # the override is attributed to the repo layer
    assert " repo " in s and "claude auth login" in s and "subscription_only ✓" in s and "pnpm test" in s and "not enabled" in s and "available, not enabled" in s


class _C:
    def __init__(self, label, ok=True, blocking=True, detail=""):
        self.label, self.ok, self.blocking, self.detail = label, ok, blocking, detail


READY = [_C("no open blocking questions"), _C("architect review done"), _C("verification configured", ok=False, blocking=False)]


def step(mode="PLAN", checks=READY, open_q=None, busy="", status="PLANNING", pending=None, **kw):
    p = Projection()
    p.status, p.pending = status, pending
    for k, v in kw.items():
        setattr(p, k, v)
    bar, hint = views.next_step(p, mode, checks, open_q, busy, 0, "T2 (worker)")
    return bar.plain, hint


def test_next_step_says_what_to_do_in_each_plan_state():
    bar, hint = step()
    assert bar.startswith("✓ Ready") and "/approve runs the plan" in bar and "! verification configured" in bar and hint.startswith("/approve")
    bar, hint = step(open_q=[{"id": "Q-3"}, {"id": "Q-5"}])
    assert bar.startswith("✗ 2 questions wait for you: Q-3, Q-5") and "1–9" in hint
    bar, _ = step(checks=[_C("architect review done", ok=False)])
    assert "architect has not reviewed" in bar and "/review" in bar
    bar, hint = step(pending={"changes": []})
    assert bar.startswith("Proposal waiting") and "a accept" in hint
    bar, _ = step(busy="Architect is reviewing…")
    assert "Architect is reviewing" in bar and "type comments meanwhile" in bar
    assert step(checks=[_C("no tasks", ok=False)])[0].startswith("✗ not ready: no tasks")


def test_next_step_for_run_result_chat_and_quota():
    assert step("RUN", tasks={"T1": "done", "T2": "pending"}, plan={"tasks": [{}, {}]})[0].count("1/2 done") == 1
    assert step("RESULT", status="COMPLETED")[0].startswith("✓ COMPLETED")
    assert "/resume" in step("RESULT", status="FAILED")[0]
    assert step("CHAT")[0].startswith("Ask anything")
    assert "quota reached" in step("PLAN", status="WAITING_QUOTA", quota_hit={"provider": "codex", "reset": "04:10"})[0]


def test_checklist_is_one_line_when_ready_and_lists_only_what_is_missing_otherwise():
    t = views.checklist_view(READY).plain
    assert t.splitlines()[0] == "Ready to approve? ✓" and "! verification configured" in t and "no open blocking" not in t
    t = views.checklist_view([_C("no open blocking questions", ok=False, detail="Q-1"), _C("tasks")]).plain
    assert "Ready to approve? ✗ 1 left" in t and "✗ no open blocking questions — Q-1" in t and "tasks" not in t


def test_architect_message_is_one_short_line_per_finding():
    long = "x" * 400
    m = {"who": "arch", "text": "revise", "trigger": "before_plan",
         "findings": [{"id": "F1", "severity": "major", "file": "src/a/b/leave.dto.ts", "message": long}]}
    lines = views.message_view(m).plain.splitlines()
    assert len(lines) == 3 and all(len(l) <= 140 for l in lines) and "leave.dto.ts" in lines[1] and "/findings" in lines[2]


def test_plan_view_lists_findings_with_their_state():
    p = Projection()
    p.plan = {"goal": "g", "constraints": [], "acceptance_criteria": ["AC"], "tasks": [], "risks": [], "out_of_scope": []}
    p.questions = {"Q-1": {"id": "Q-1", "text": "t", "answered": False}, "Q-2": {"id": "Q-2", "text": "t", "answered": True}}
    p.findings = {"F1": {"severity": "major", "file": "a.ts", "verdict": "fixed", "question": None},
                  "F2": {"severity": "minor", "file": "b.ts", "verdict": "question", "question": "Q-1"},
                  "F3": {"severity": "minor", "file": "c.ts", "verdict": "question", "question": "Q-2"},
                  "F4": {"severity": "nit", "file": "d.ts", "verdict": "rejected", "question": None}}
    t = txt(views.plan_view(p))
    assert "Architect review" in t and "fixed ✓" in t and "Q-1 open ← blocks /approve" in t and "Q-2 answered ✓" in t and "rejected" in t
