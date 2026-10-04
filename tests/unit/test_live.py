"""Live streaming: ephemeral bus channel, runtime delta mapping, projection buffers."""
import asyncio

from agent_desk.contracts import Event
from agent_desk.events.bus import EventBus
from agent_desk.events.store import EventStore
from agent_desk.runtime.base import labelled
from agent_desk.runtime.codex_app_server import CodexAppServerRuntime
from agent_desk.tui.projection import Projection, partial_text


async def test_live_events_reach_subscribers_but_are_never_persisted():
    s = await EventStore(":memory:").open()
    await s.create_session("s1", "/r", "t", {}, "h", "now")
    bus = EventBus(s)
    got = []

    async def sub():
        async for e in bus.subscribe("s1"):
            got.append(e.type)
            if len(got) == 3:
                return
    task = asyncio.create_task(sub())
    await asyncio.sleep(0)
    await bus.emit(Event(session_id="s1", source="t", type="a"))
    bus.publish_live(Event(session_id="s1", source="t", type="live.text", payload={"delta": "x"}))
    bus.publish_live(Event(session_id="s1", source="t", type="live.text", payload={"delta": "y"}))
    await asyncio.wait_for(task, 1)
    assert got == ["a", "live.text", "live.text"]
    assert [e.type for e in await s.events("s1")] == ["a"]
    await s.close()


async def test_labelled_sink_sets_run_id_and_label():
    out = []
    async def emit(t, p): out.append((t, p))
    f = labelled(emit, "main-3", "plan")
    await f("agent.started", {"model": "m"})
    await f("live.text", {"delta": "x"})
    assert out == [("agent.started", {"model": "m", "_run": "main-3", "label": "plan"}), ("live.text", {"delta": "x", "_run": "main-3"})]


def test_codex_notifications_map_to_live_and_milestones():
    n = CodexAppServerRuntime._normalize
    class R: usage = {}
    assert n(R, "item/agentMessage/delta", {"delta": "he"}) == [("live.text", {"delta": "he"})]
    assert n(R, "item/reasoning/summaryTextDelta", {"delta": "th"}) == [("live.reasoning", {"delta": "th"})]
    assert n(R, "item/commandExecution/outputDelta", {"delta": "ok\n"}) == [("live.output", {"delta": "ok\n"})]
    assert n(R, "item/started", {"item": {"type": "fileChange", "changes": [{"path": "a.py"}, {"path": "b.py"}]}}) == [("tool.started", {"tool": "edit", "command": "a.py, b.py"})]
    assert n(R, "item/completed", {"item": {"type": "reasoning", "summary": ["Looking at tests"]}}) == [("agent.reasoning", {"text": "Looking at tests"})]
    assert n(R, "item/completed", {"item": {"type": "reasoning", "summary": []}}) == []
    assert n(R, "item/completed", {"item": {"type": "agentMessage", "text": "I'll look"}}) == [("agent.message", {"text": "I'll look"})]


def test_partial_text_handles_truncation_and_escapes():
    assert partial_text('{"kind":"answer","text":"a \\"b\\" c\\nd') == 'a "b" c\nd'
    assert partial_text('{"kind":"ans') is None
    assert partial_text('{"text": "ab", "plan": null}') == "ab"
    assert partial_text('{"text":"x\\') == "x"


def ev(seq, type_, run="main-1", **p):
    return Event(session_id="s", seq=seq, source="main", type=type_, agent_run_id=run, payload=p)


def test_projection_live_buffers_follow_the_run_lifecycle():
    pj = Projection()
    pj.apply(ev(1, "agent.started", role="main", label="plan", provider="codex", model="m"))
    a = pj.agents["main-1"]
    assert a.label == "plan" and a.running
    pj.apply(ev(0, "live.reasoning", delta="thinking "))
    pj.apply(ev(0, "live.text", delta='{"kind":"answer","text":"Hel'))
    pj.apply(ev(0, "live.text", delta='lo'))
    assert a.live_reasoning == "thinking " and a.reply_preview() == "Hello"
    pj.apply(ev(2, "tool.started", command="rg foo"))
    assert a.activity == "$ rg foo"
    assert pj.log[-1][2] == "tool.started"
    pj.apply(ev(3, "agent.finished", status="completed"))
    assert not a.running and a.live_text == "" and a.live_reasoning == ""
    assert not pj.apply(ev(0, "live.text", delta="late"))     # deltas after finish are dropped


def test_plan_annotations_and_architect_chat():
    pj = Projection()
    plan = {"goal": "g", "acceptance_criteria": ["a"], "risks": [], "out_of_scope": [], "tasks": [{"id": "T1", "title": "x"}]}
    pj.apply(ev(1, "plan.version", version=1, hash="h1", plan=plan, source="main"))
    plan2 = {**plan, "acceptance_criteria": ["a", "b"], "tasks": [{"id": "T1", "title": "y"}]}
    pj.apply(ev(2, "plan.version", version=2, hash="h2", plan=plan2, source="architect"))
    assert pj.annotations == {"AC-2": "changed in v2 (architect)", "T1": "changed in v2 (architect)"}
    pj.apply(ev(3, "architect.reviewed", trigger="before_plan", verdict="changes", findings=[{"id": "F1", "severity": "major", "file": "a.py", "message": "m"}]))
    assert pj.chat[-1]["who"] == "arch" and pj.chat[-1]["findings"][0]["id"] == "F1"
    assert [t["id"] for t in pj.queued_tasks()] == ["T1"]


def test_claude_limit_shows_status_and_reset_in_status_bar():
    from agent_desk.tui import views
    from agent_desk.tui.status import ProviderStatus, StatusInfo
    from agent_desk.config.loader import load
    from tests.workflow.conftest import NOGLOBAL
    pj = Projection()
    pj.apply(ev(1, "provider.limit", provider="claude", status="allowed_warning", resetsAt=1791034200, rateLimitType="five_hour"))
    st = StatusInfo({"claude": ProviderStatus("claude", "claude-cli", True)})
    s = views.status_bar(load(None, global_path=NOGLOBAL).config, st, [], pj.limits).plain
    assert "claude login 5h near limit · reset" in s
    assert "quota: after first run" in views.status_bar(load(None, global_path=NOGLOBAL).config, st, [], {}).plain
