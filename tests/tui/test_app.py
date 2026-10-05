import asyncio
import subprocess
from pathlib import Path

import pytest

from agent_desk.config.loader import load
from agent_desk.contracts import MainReply, Question, Questions
from agent_desk.plugins import default_registry
from agent_desk.runtime.mock import MockRuntime
from agent_desk.runtime.registry import RuntimeRouter
from agent_desk.tui.app import AgentDeskApp
from agent_desk.tui.composer import Composer, complete
from agent_desk.tui.screens import TextScreen

from ..workflow.conftest import NOGLOBAL, Script, plan, qtask, route, task

SIZE = (170, 55)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; (r / "src").mkdir(parents=True)
    (r / "calc.py").write_text("x = 1\n"); (r / "src" / "schedule.py").write_text("y = 2\n")
    (r / ".agent-desk.yaml").write_text('verification:\n  unit: { command: "true" }\n')
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=r, check=True)
    return r


def make_app(repo, tmp_path, script):
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    rt = MockRuntime(script)
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    return app, rt


async def until(pilot, cond, timeout=15):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


async def see(pilot, app, *needles, timeout=10):
    """Wait until the rendered screen shows every needle (the UI repaints on a 0.2 s tick)."""
    for _ in range(int(timeout / 0.05)):
        t = screen_text(app)
        if all(n in t for n in needles):
            return t
        await pilot.pause(0.05)
    raise AssertionError(f"never saw {needles}; screen:\n{screen_text(app)[-1500:]}")


async def send(pilot, app, text):
    c = app.query_one("#composer", Composer)
    c.focus()
    c.load_text(text)
    await pilot.press("enter")
    await pilot.pause(0.1)


def screen_text(app) -> str:
    from io import StringIO
    from rich.console import Console
    out = []
    for w in app.query("Static"):
        if not all(a.display for a in w.ancestors_with_self):
            continue                                                           # inside a hidden ContentSwitcher pane
        r = getattr(w, "content", None)
        if r is None or r == "":
            continue
        try:
            c = Console(file=StringIO(), width=160, force_terminal=False, color_system=None)
            c.print(r)
            out.append(c.file.getvalue())
        except Exception:
            pass
    return "\n".join(out)


async def test_full_flow_compose_plan_approve_run_result(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"), task("T2", ["T1"], True))))
    async with app.run_test(size=SIZE) as pilot:
        assert app.mode() == "HOME" and "What do you want to do" in screen_text(app)
        await send(pilot, app, "Add feature X. See @calc.py")
        await until(pilot, lambda: app.proj.plan is not None and app.session.studio is not None)
        assert app.mode() == "PLAN" and "PLAN v1" in screen_text(app) and "Ready to approve" in screen_text(app)
        assert app.session.sched.plan_locked is False                         # nothing runs before /approve
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.outcome is not None)
        await pilot.pause(0.5)
        assert app.outcome == "COMPLETED" and app.mode() == "RESULT"
        t = screen_text(app)
        assert "COMPLETED" in t and "done" in t.lower() and "branch agent-desk/" in t and "calc" in t or "COMMITS" in t
        assert app.query_one("#composer", Composer).text == ""


async def test_clarify_questions_answered_from_the_composer_become_decisions(repo, tmp_path):
    qs = Questions(questions=[Question(id="Q-1", text="What does 409 return?", options=["currentRevision", "empty"], recommended=0, evidence="PRD s4")])
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), qs))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: "Q-1" in app.proj.questions)
        await see(pilot, app, "1) currentRevision  (recommended)")
        await send(pilot, app, "2")
        await until(pilot, lambda: app.proj.plan is not None)
        d = await app.session.mem.decisions_in_force()
        assert [(x.id, x.verbatim) for x in d] == [("D-001", "empty")]
        assert app.proj.questions["Q-1"]["answered"]


async def test_approve_is_refused_with_reasons_while_a_question_is_open(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"))))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        from agent_desk.studio import Question as SQ
        await app.session.studio.add_question(SQ("Q-9", "Is the import script in scope?", ["yes", "no"], "x"))
        await send(pilot, app, "/approve")
        await until(pilot, lambda: any("cannot approve yet" in m["text"] for m in app.proj.chat if m["who"] == "sys"))
        assert not app.session.sched.plan_locked
        await send(pilot, app, "2")                                            # answers Q-9 (option 2)
        await until(pilot, lambda: app.session.studio.questions["Q-9"].answered)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.session.sched.plan_locked)


async def test_discuss_then_comment_proposal_accept_with_keys(repo, tmp_path):
    def reply(spec):
        if "adding a risk" in spec.prompt:
            p = plan(task("T1")).model_copy(deep=True); p.risks = ["watch migration"]
            return MainReply(kind="proposal", text="added a risk", plan=p)
        return MainReply(kind="answer", text="because of atomicity (a.py:3)", plan=None)
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), reply=reply))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "why updateMany?")
        await until(pilot, lambda: any(m["who"] == "main" for m in app.proj.chat))
        assert len(app.session.studio.versions) == 1
        await see(pilot, app, "atomicity")
        await send(pilot, app, "[T1] adding a risk please")
        await until(pilot, lambda: app.proj.pending is not None)
        await see(pilot, app, "proposal", "[a] accept")
        await until(pilot, lambda: app.focused is app.query_one("#pcard"))    # the card takes the keys by itself, no Esc first
        await pilot.press("a")
        await until(pilot, lambda: app.proj.pending is None and app.proj.plan_version == 2)
        assert app.proj.plan["risks"] == ["watch migration"]


async def test_at_completion_inserts_file_and_chips_appear(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"))))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)                                                # repo files load
        c = app.query_one("#composer", Composer)
        c.focus()
        for ch in "see @sched":
            await pilot.press("space" if ch == " " else ch)
        await pilot.pause(0.1)
        assert c.items and c.items[0][0] == "@src/schedule.py"
        assert "src/schedule.py" in screen_text(app)                          # popup is visible
        await pilot.press("tab")
        assert c.text == "see @src/schedule.py "
        assert "src/schedule.py" in screen_text(app)                          # chip below the box


def test_complete_function_pure(tmp_path):
    files = ["src/roster/roster.repository.ts", "src/roster/schedule.mapper.ts", "test/roster/schedule.e2e-spec.ts", "docs/readme.md"]
    r = complete("@sched", False, files, tmp_path)
    assert [x[0] for x in r][:2] == ["@src/roster/schedule.mapper.ts", "@test/roster/schedule.e2e-spec.ts"]
    assert complete("@zzz", False, files, tmp_path) == []
    assert [x[0] for x in complete("/ap", True, files, tmp_path)] == ["/approve"]
    assert complete("/ap", False, files, tmp_path) == []                       # a slash mid-sentence is not a command
    (tmp_path / "docs").mkdir(); (tmp_path / "docs" / "prd.md").write_text("x")
    assert complete(f"@{tmp_path}/doc", False, files, tmp_path) == [(f"@{tmp_path}/docs/", "dir")]
    assert complete(f"@{tmp_path}/docs/", False, files, tmp_path) == [(f"@{tmp_path}/docs/prd.md", "file")]


async def test_f2_opens_components_and_model_command_switches_role(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"))))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("f2")
        await pilot.pause(0.2)
        assert isinstance(app.screen, TextScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, TextScreen)
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/model architect claude opus")
        await until(pilot, lambda: app.session.overrides.get("architect") is not None)
        await until(pilot, lambda: any(o.get("role") == "architect" for o in app.proj.overrides))
        await see(pilot, app, "architect → claude·opus (session)")           # the status bar shows it without opening any file
        await send(pilot, app, "/engine retry_or_stop none")
        await until(pilot, lambda: app.session.pipeline.point("retry_or_stop").engine == "none")
        await send(pilot, app, "/model worker nobody x")
        await until(pilot, lambda: any("worker" in m["text"] and m["who"] == "sys" and "provider" in m["text"].lower() or "nobody" in m["text"] for m in app.proj.chat))


async def test_stop_flow_and_quota_banner(repo, tmp_path):
    from agent_desk.runtime.base import RunResult
    inner = Script(plan(task("T1", write=True)))
    n = {"k": 0}
    def script(spec):
        if spec.role.role == "worker":
            n["k"] += 1
            if n["k"] == 1:
                return RunResult("quota", "", "", {}, "limit", "3:45pm")
        return inner(spec)
    app, rt = make_app(repo, tmp_path, script)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.proj.status == "WAITING_QUOTA")
        await pilot.pause(0.5)
        t = screen_text(app)
        assert "Quota reached" in t and "3:45pm" in t and "quota reached" in t.lower()
        await send(pilot, app, "/resume")
        await until(pilot, lambda: app.outcome == "COMPLETED")


class ApprovingRuntime(MockRuntime):
    """A runtime whose worker asks permission mid-run, like codex requestApproval / the claude hook bridge do."""
    def __init__(self, script, command):
        super().__init__(script)
        self.command, self.answers = command, []

    async def run(self, spec, emit, approve):
        if spec.role.role == "worker":
            from agent_desk.runtime.base import Approval
            self.answers.append(await approve(Approval(spec.run_id, "command", self.command, str(spec.cwd), None)))
        return await super().run(spec, emit, approve)


async def test_approval_modal_appears_mid_run_and_blocks_until_answered(repo, tmp_path):
    from agent_desk.tui.screens import ChoiceScreen
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    rt = ApprovingRuntime(Script(plan(task("T1", write=True))), "pnpm prisma migrate dev --name x")
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))          # the run is paused on the user
        assert app.outcome is None and not rt.answers
        await pilot.pause(0.5)                                                    # the base screen keeps repainting under the modal
        await pilot.press("a")
        await until(pilot, lambda: app.outcome is not None)
        assert rt.answers == [True] and app.outcome == "COMPLETED"


async def test_push_approval_shows_branch_remote_and_commits(repo, tmp_path):
    """The user must see where a push goes, resolved from git, not just the agent's command line."""
    from agent_desk.tui.screens import ChoiceScreen
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=repo, check=True)
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    rt = ApprovingRuntime(Script(plan(task("T1", write=True))), "/bin/zsh -lc 'git push origin HEAD:docs-x'")
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
        body = str(app.screen.body)
        assert "→ origin/docs-x" in body and str(bare) in body and "new branch" in body and "0 commit(s)" in body
        await pilot.press("r")
        await until(pilot, lambda: app.outcome is not None)


async def test_rejecting_an_approval_returns_false_to_the_agent(repo, tmp_path):
    from agent_desk.tui.screens import ChoiceScreen
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    rt = ApprovingRuntime(Script(plan(task("T1", write=True))), "pnpm prisma migrate dev")
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
        await pilot.press("r")
        await until(pilot, lambda: app.outcome is not None)
        assert rt.answers == [False]


async def test_safe_commands_never_open_the_modal(repo, tmp_path):
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    rt = ApprovingRuntime(Script(plan(task("T1", write=True))), "git status")
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.outcome is not None)
        assert rt.answers == [True] and app.outcome == "COMPLETED"


async def test_startup_problems_are_visible_not_silent(tmp_path):
    plain = tmp_path / "plain"; plain.mkdir()                                   # not a git repository
    loaded = load(plain, global_path=NOGLOBAL)
    reg = default_registry()
    app = AgentDeskApp(plain, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": MockRuntime(Script(plan(task("T1"))))}), tmp_path / "home")
    async with app.run_test(size=SIZE) as pilot:
        await see(pilot, app, "is not a git repository", timeout=20)  # shown before the user even types
        await send(pilot, app, "do something")
        await see(pilot, app, "is not a git repository")
        assert app.session is None


async def test_missing_mention_is_reported_and_text_kept(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"))))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "look at @does/not/exist.py please")
        await see(pilot, app, "cannot find @does/not/exist.py")
        assert app.session is None


def make_streaming_app(repo, tmp_path, script, delay=1.0):
    loaded = load(repo, global_path=NOGLOBAL)
    reg = default_registry()
    def stream(spec):
        if spec.role.role != "main":
            return [("tool.started", {"command": "pnpm test"}), ("live.output", {"delta": "PASS calc\n"})]
        return [("tool.started", {"command": "rg schedule"}), ("live.reasoning", {"delta": "Reading the scheduler first"}),
                ("live.text", {"delta": '{"kind":"answer","text":"Drafting the '}), ("live.text", {"delta": 'answer now'})]
    rt = MockRuntime(script, delay=delay, stream=stream)
    app = AgentDeskApp(repo, loaded, reg, lambda l: RuntimeRouter(l.config, reg, {"codex": rt, "claude": rt, "antigravity": rt}), tmp_path / "home")
    return app, rt


async def test_main_streams_into_a_live_bubble_while_it_works(repo, tmp_path):
    app, rt = make_streaming_app(repo, tmp_path, Script(plan(task("T1"))), delay=3)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        t = await see(pilot, app, "main ·", "$ rg schedule", "Reading the scheduler first", "Drafting the answer now")
        assert "00:0" in t                                                     # elapsed clock on the bubble
        await until(pilot, lambda: app.proj.plan is not None)
        await until(pilot, lambda: not app.proj.running_agents())
        assert "Drafting the answer now" not in screen_text(app)              # the bubble goes away when Main finishes
        assert "waiting for the first output" not in screen_text(app)


async def test_question_card_takes_keys_without_esc_and_accepts_free_text(repo, tmp_path):
    qs = Questions(questions=[Question(id="Q-1", text="What does 409 return?", options=["currentRevision", "empty"], recommended=0, evidence="PRD s4"),
                              Question(id="Q-2", text="Old clients?", options=["allow", "reject"], recommended=1, evidence="")])
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), qs))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.focused is app.query_one("#qcard"))     # focus moved to the card by itself
        await see(pilot, app, "▸ 1) currentRevision")
        await pilot.press("down", "enter")                                    # Q-1 -> option 2
        await until(pilot, lambda: app.query_one("#qcard").qid == "Q-2")
        await see(pilot, app, "▸ 2) reject")                                  # cursor starts on the recommendation
        await pilot.press("m")                                                # any other key -> free text in the composer
        c = app.query_one("#composer", Composer)
        assert app.focused is c and c.text == "m"
        for ch in "aybe later":
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")
        await until(pilot, lambda: app.proj.plan is not None)
        d = await app.session.mem.decisions_in_force()
        assert [(x.id, x.verbatim) for x in d] == [("D-001", "empty"), ("D-002", "maybe later")]


async def test_chat_keeps_full_history_and_follows_the_bottom(repo, tmp_path):
    reply = lambda spec: MainReply(kind="answer", text="answer " + spec.prompt.rsplit("User message: ", 1)[-1].split("\n")[0], plan=None)
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), reply=reply))
    async with app.run_test(size=(170, 30)) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio)
        for i in range(10):
            await send(pilot, app, f"question number {i}")
            await until(pilot, lambda: any(m["text"] == f"answer question number {i}" for m in app.proj.chat))
        await pilot.pause(0.5)
        log = app.query_one("#chat")
        assert len(log.query(".msg")) >= 20                                  # nothing dropped
        assert log.max_scroll_y > 0 and log.scroll_offset.y >= log.max_scroll_y - 1


async def test_run_screen_lists_queued_tasks_and_inspects_the_selected_agent(repo, tmp_path):
    app, rt = make_streaming_app(repo, tmp_path, Script(plan(task("T1"), task("T2", ["T1"], True))), delay=1.5)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "do it")
        await until(pilot, lambda: app.session and app.session.studio and app.session.studio.ready() and not app.proj.running_agents(), timeout=20)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.mode() == "RUN" and any(a.task == "T1" and a.running for a in app.proj.agents.values()))
        t = await see(pilot, app, "⏸ queued", "T2", "agents [1/", "spawn agents", "review + verify", "$ pnpm test")
        await until(pilot, lambda: app.focused is app.query_one("#agents"))    # RUN: the dashboard has the keys
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        await pilot.press("escape")
        await pilot.pause(0.2)
        await pilot.press("h")                                                # typing on the dashboard starts a message
        c = app.query_one("#composer", Composer)
        assert app.focused is c and c.text == "h"
        c.load_text("")
        await until(pilot, lambda: app.outcome is not None, timeout=30)


async def test_reopen_failed_session_lists_it_then_resume_completes(repo, tmp_path):
    from agent_desk.graph import open_session, run_graph
    from agent_desk.ui import HeadlessUI
    # passes on the base commit (no a.txt yet), so a bad a.txt is a regression that blocks
    (repo / ".agent-desk.yaml").write_text('verification:\n  unit: { command: "test ! -f a.txt || grep -q good a.txt" }\n')
    loaded = load(repo, global_path=NOGLOBAL)
    bad = Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "bad"))
    s = await open_session(repo.resolve(), "make a.txt good", loaded, MockRuntime(bad), HeadlessUI(), tmp_path / "home")
    assert await run_graph(s) == "FAILED"
    await s.close()

    app, rt = make_app(repo, tmp_path, Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "good")))
    async with app.run_test(size=SIZE) as pilot:
        await see(pilot, app, "Earlier sessions", s.sid, "FAILED")
        await send(pilot, app, f"/open {s.sid}")
        await until(pilot, lambda: app.mode() == "RESULT" and app.outcome == "FAILED")
        await see(pilot, app, "/resume continues")
        await send(pilot, app, "/resume")
        await until(pilot, lambda: app.outcome == "COMPLETED")
        assert [x for x in rt.specs if x.role.role == "worker"]


async def test_main_answers_show_on_the_result_screen(repo, tmp_path):
    """Regression (s-69b1): after FAILED, Main answered the chat but the RESULT screen never showed it."""
    (repo / ".agent-desk.yaml").write_text('verification:\n  unit: { command: "test ! -f a.txt || grep -q good a.txt" }\n')
    reply = lambda spec: MainReply(kind="answer", text="the output is a docs-only contract", plan=None)
    msgs = []
    def routed(spec):                                         # the first message is a plan; later ones are plain questions
        msgs.append(1)
        return route("plan", "plan it") if len(msgs) == 1 else route("answer", "the output is a docs-only contract")
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1", write=True)), worker=lambda spec, n: ("a.txt", "bad"), reply=reply, route=routed))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "make a.txt good")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.outcome == "FAILED" and app.mode() == "RESULT", timeout=30)
        await send(pilot, app, "what is your output?")
        await see(pilot, app, "CHAT WITH MAIN", "what is your output?", "docs-only contract")


async def test_reopen_while_planning_is_not_stuck_busy_and_approve_works(repo, tmp_path):
    """Regression (s-0c1f): /open on a PLANNING session showed 'back to the plan…' forever and blocked /approve."""
    import asyncio
    from agent_desk.contracts import Plan
    from agent_desk.graph import open_session, run_graph
    from agent_desk.ui import HeadlessUI

    class Walkaway(HeadlessUI):                               # user leaves while the plan is a draft
        async def run_studio(self, studio):
            await asyncio.Event().wait()

    answer = Plan(goal="Answer: 1 + 1 = 2", constraints=[], acceptance_criteria=["The user gets the answer"], tasks=[], risks=[], out_of_scope=[])
    loaded = load(repo, global_path=NOGLOBAL)
    s = await open_session(repo.resolve(), "1 + 1 = ?", loaded, MockRuntime(Script(answer)), Walkaway(), tmp_path / "home")
    t = asyncio.create_task(run_graph(s))
    for _ in range(200):
        if s.studio is not None and s.studio.versions:
            break
        await asyncio.sleep(0.02)
    t.cancel()
    await asyncio.sleep(0.05)
    await s.close()

    app, rt = make_app(repo, tmp_path, Script(answer))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, f"/open {s.sid}")
        await until(pilot, lambda: app.studio_future is not None)
        assert app.busy == ""
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.outcome == "COMPLETED")


async def test_commands_get_through_while_busy(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1"))))
    async with app.run_test(size=SIZE) as pilot:
        app.busy = "thinking hard…"
        await send(pilot, app, "hello")
        await see(pilot, app, "still working on: thinking hard…", "/stop")
        await send(pilot, app, "/help")
        await see(pilot, app, "/settings")


async def test_diff_shows_committed_and_uncommitted_changes(repo, tmp_path):
    """/diff used to compare against HEAD only: empty as soon as a task had committed."""
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1", write=True, title="Write a.txt")), worker=lambda spec, n: ("a.txt", "hello-from-agent")))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "write a.txt")
        await until(pilot, lambda: app.session and app.session.studio)
        await send(pilot, app, "/approve")
        await until(pilot, lambda: app.outcome == "COMPLETED", timeout=30)
        (app.session.ws.path / "b.txt").write_text("not committed\n")
        await send(pilot, app, "/diff")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        body = app.screen.query_one("Static").content if hasattr(app.screen.query_one("Static"), "content") else ""
        t = str(body)
        assert "chore(agent-desk): write a.txt (T1)" in t and "+hello-from-agent" in t
        assert "not committed yet: b.txt" in t and "+not committed" in t


async def test_first_question_is_answered_in_chat_without_plan_or_worktree(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), route=lambda sp: route("answer", "calc.py holds x = 1")))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "what is in @calc.py ?")
        await see(pilot, app, "calc.py holds x = 1")
        assert app.mode() == "CHAT" and app.session.ws is None and app.busy == ""
        assert not [x for x in rt.specs if x.role.role != "main"]


async def test_quick_goes_chat_run_result_and_a_message_from_result_starts_job_two(repo, tmp_path):
    n = []
    def routed(sp):
        n.append(1)
        return route("quick", "edit", task=qtask("a.txt" if len(n) == 1 else "b.txt"))
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), route=routed, worker=lambda sp, k: ("a.txt", "1") if "a.txt" in sp.prompt else ("b.txt", "2")))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "create a.txt")
        await until(pilot, lambda: app.outcome == "COMPLETED" and app.mode() == "RESULT", timeout=30)
        assert app.session.job == 1 and not app.session.studio.pending
        await send(pilot, app, "now b.txt")
        await until(pilot, lambda: app.session.job == 2 and app.outcome == "COMPLETED" and app.mode() == "RESULT", timeout=30)
        assert (app.session.ws.path / "b.txt").exists()


async def test_plan_command_forces_the_plan_flow(repo, tmp_path):
    app, rt = make_app(repo, tmp_path, Script(plan(task("T1")), route=lambda sp: route("answer", "never used")))
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello")
        await see(pilot, app, "never used")
        await send(pilot, app, "/plan add feature X")
        await until(pilot, lambda: app.mode() == "PLAN" and app.proj.plan is not None)
