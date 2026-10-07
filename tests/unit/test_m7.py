"""M7: Codex isolation, explicit effort, per-run usage, budgets, Main cache discipline, rotation, resume fallback."""
import asyncio
import json

from agent_desk.config.loader import load
from agent_desk.contracts import MAIN_FIELD, Event, MainTurn, Plan, Questions, Summary
from agent_desk.reasoner import Reasoner, ReasonerError
from agent_desk.roles import resolve
from agent_desk.runtime.base import RunResult, RunSpec
from agent_desk.runtime.codex_app_server import CodexAppServerRuntime
from agent_desk.runtime.jsonrpc import RpcError
from agent_desk.tui.projection import Projection

from ..workflow.conftest import NOGLOBAL

CFG = load(None, global_path=NOGLOBAL).config


class FakeRpc:
    """Plays the app-server: each turn emits `steps` (method, params) notes, then completes unless interrupted."""

    def __init__(self, rt, steps_per_turn):
        self.rt, self.steps, self.calls, self.n = rt, steps_per_turn, [], 0

    async def start(self): pass
    async def notify(self, *a): pass
    async def close(self): pass

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "codex/0.159.0 x"}
        if method == "skills/list":
            return {"data": [{"cwd": "/w", "errors": [], "skills": [{"name": "big-skill", "enabled": True}, {"name": "off", "enabled": False}]}]}
        if method == "thread/start":
            return {"thread": {"id": "T-new"}}
        if method == "thread/resume":
            if params["threadId"] == "gone":
                raise RpcError({"code": -1, "message": "no rollout"})
            return {}
        if method == "turn/start":
            self.n += 1
            tid, n = params["threadId"], self.n
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._play(tid, n)))
            return {"turn": {"id": f"turn-{n}"}}
        if method == "turn/interrupt":
            self.rt._on_note("turn/completed", {"threadId": params["threadId"], "turn": {"status": "interrupted"}})
            return {}
        return {}

    async def _play(self, tid, n):
        for m, p in self.steps(n):
            await asyncio.sleep(0)
            run = self.rt._by_thread.get(tid)
            if run is None or run.done.done():
                return
            self.rt._on_note(m, {"threadId": tid, **p})
        self.rt._on_note("turn/completed", {"threadId": tid, "turn": {"status": "completed"}})


def usage(total_in, last_in, cached=0, out=0, win=100_000):
    return ("thread/tokenUsage/updated", {"tokenUsage": {"total": {"inputTokens": total_in, "cachedInputTokens": cached, "outputTokens": out},
                                                         "last": {"inputTokens": last_in}, "modelContextWindow": win}})


def tool(cmd="rg -n x"):
    return ("item/started", {"item": {"type": "commandExecution", "command": cmd}})


def msg(text):
    return ("item/completed", {"item": {"type": "agentMessage", "text": text}})


def runtime(steps):
    import tempfile
    rt = CodexAppServerRuntime(argv=["true"], home=tempfile.mkdtemp())    # empty profile: the user's own MCP servers never leak into tests
    rt.rpc = FakeRpc(rt, steps)
    return rt


async def run(rt, role="explorer", thread_id=None, **kw):
    events = []
    async def emit(t, p): events.append((t, p))
    async def ok(a): return True
    r = resolve(CFG, role)
    if kw:
        from dataclasses import replace
        r = replace(r, **kw)
    res = await rt.run(RunSpec("x-1", r, "/w", "go", {"type": "object"}, thread_id=thread_id), emit, ok)
    return res, events


async def test_thread_is_isolated_from_agents_md_and_skills_and_effort_is_explicit():
    rt = runtime(lambda n: [msg("{}")])
    res, _ = await run(rt, "architect", effort=None)
    start = dict(rt.rpc.calls)["thread/start"]
    assert start["config"] == {"project_doc_max_bytes": 0, "skills": {"config": [{"name": "big-skill", "enabled": False}]}}
    assert dict(rt.rpc.calls)["turn/start"]["effort"] == "medium"          # never left to ~/.codex/config.toml
    assert res.status == "completed"


async def test_defaults_give_every_role_an_effort():
    assert all(resolve(CFG, r).effort for r in CFG.roles)


async def test_usage_is_per_run_even_on_a_reused_thread():
    # the thread already holds 50k from earlier turns; this run adds two model calls of 20k and 25k
    rt = runtime(lambda n: [usage(70_000, 20_000, cached=10_000, out=100), usage(95_000, 25_000, cached=30_000, out=300), msg("{}")])
    rt._known.add("T-old")
    res, _ = await run(rt, thread_id="T-old")
    assert res.usage["inputTokens"] == 45_000 and res.usage["contextTokens"] == 25_000 and res.usage["contextWindow"] == 100_000
    assert not [c for c in rt.rpc.calls if c[0] == "thread/resume"]


async def test_unknown_thread_is_resumed_and_a_lost_one_fails_cleanly():
    rt = runtime(lambda n: [msg("{}")])
    res, _ = await run(rt, thread_id="T-earlier-process")
    assert ("thread/resume" in [c[0] for c in rt.rpc.calls]) and res.status == "completed"
    res, _ = await run(rt, thread_id="gone")
    assert res.status == "failed" and res.error.startswith("resume failed")


async def test_tool_budget_interrupts_then_asks_for_a_wrap_up_report():
    rt = runtime(lambda n: [tool(), tool(), tool(), tool()] if n == 1 else [msg('{"status":"partial"}')])
    res, events = await run(rt, max_tool_calls=2)
    starts = [p for m, p in rt.rpc.calls if m == "turn/start"]
    assert "turn/interrupt" in [c[0] for c in rt.rpc.calls] and len(starts) == 2
    assert "budget for this run is used up" in starts[1]["input"][0]["text"] and starts[1]["outputSchema"] == {"type": "object"}
    assert res.status == "completed" and res.final_text == '{"status":"partial"}'
    assert ("agent.limit", {"reason": "3 tool calls > 2"}) in events


async def test_user_stop_is_not_turned_into_a_wrap_up():
    rt = runtime(lambda n: [tool(), tool(), tool()])
    task = asyncio.create_task(run(rt, max_tool_calls=100))
    for _ in range(20):
        await asyncio.sleep(0)
        if rt._by_run.get("x-1") and rt._by_run["x-1"].turn_id:
            break
    await rt.cancel("x-1")
    res, _ = await task
    assert res.status == "interrupted" and len([c for c in rt.rpc.calls if c[0] == "turn/start"]) == 1


# ---------------- Main ----------------
class MainRt:
    """Answers Main through the MainTurn envelope and records every spec."""

    def __init__(self, ctx=(1000, 100_000), fail_resume=False):
        self.specs, self.ctx, self.fail_resume = [], ctx, fail_resume

    async def run(self, spec, emit, approve):
        self.specs.append(spec)
        if self.fail_resume and spec.thread_id == "old":
            return RunResult("failed", error="resume failed: gone")
        inner = {"Questions": Questions(questions=[]), "Summary": Summary(text="t", ac_evidence=[]),
                 "Plan": Plan(goal="g", constraints=[], acceptance_criteria=[], tasks=[], risks=[], out_of_scope=[])}[spec.schema_name]
        out = {k: None for k in MAIN_FIELD.values()} | {MAIN_FIELD[spec.schema_name]: json.loads(inner.model_dump_json())}
        return RunResult("completed", json.dumps(out), spec.thread_id or "th-1",
                         {"inputTokens": 5, "contextTokens": self.ctx[0], "contextWindow": self.ctx[1]})


def pkt():
    from agent_desk.contracts import ContextPacket
    return ContextPacket(brief="b " * 400, task=None, acceptance=[], decisions=[], inputs=[], relevant_files=[])


async def mk(rt, **kw):
    ev = []
    async def emit(t, p): ev.append((t, p))
    async def ok(a): return True
    async def memory(): return "# session memory"
    return Reasoner(rt, CFG, "/w", emit, ok, memory=memory, **kw), ev


async def test_main_uses_one_schema_and_does_not_resend_unchanged_context():
    rt = MainRt()
    r, _ = await mk(rt)
    await r.clarify(pkt(), [])
    await r.plan(pkt(), [])
    await r.summarize(pkt(), "- facts")
    schemas = {json.dumps(s.output_schema, sort_keys=True) for s in rt.specs}
    assert len(schemas) == 1 and rt.specs[0].output_schema["title"] == "MainTurn"
    assert "b b b" in rt.specs[0].prompt and "b b b" not in rt.specs[1].prompt and "unchanged since your previous turn" in rt.specs[1].prompt
    assert "fill" in rt.specs[1].prompt.lower() and "`plan`" in rt.specs[1].prompt


async def test_main_rotates_past_the_threshold_and_resends_context():
    rt = MainRt(ctx=(70_000, 100_000))                        # 70% >= main_rotate_at 0.6
    r, ev = await mk(rt)
    await r.clarify(pkt(), [])
    assert r.thread_id is None and any(t == "main.rotated" for t, _ in ev)
    await r.plan(pkt(), [])
    assert rt.specs[1].thread_id is None and "Session memory (authoritative)" in rt.specs[1].prompt and "b b b" in rt.specs[1].prompt


async def test_lost_main_thread_falls_back_to_session_memory():
    rt = MainRt(fail_resume=True)
    r, ev = await mk(rt, thread_id="old")
    out = await r.plan(pkt(), [])
    assert isinstance(out, Plan) and rt.specs[-1].thread_id is None and "# session memory" in rt.specs[-1].prompt
    assert ("main.thread", {"thread_id": "th-1", "provider": "codex", "account": "codex", "cwd": "/w"}) in ev


async def test_null_field_is_repaired_then_fails():
    class Bad(MainRt):
        async def run(self, spec, emit, approve):
            self.specs.append(spec)
            return RunResult("completed", json.dumps({k: None for k in MAIN_FIELD.values()}), "th-1", {})
    r, _ = await mk(Bad())
    try:
        await r.plan(pkt(), [])
        raise AssertionError("expected failure")
    except ReasonerError:
        pass


def test_projection_counts_fresh_and_cached_tokens():
    p = Projection()
    ev = lambda seq, t, pl: Event(session_id="s", seq=seq, source="explorer", type=t, agent_run_id="e-1", payload=pl)
    p.apply(ev(1, "agent.started", {"role": "explorer", "provider": "codex", "model": "m"}))
    p.apply(ev(2, "agent.finished", {"status": "completed", "usage": {"inputTokens": 1000, "cachedInputTokens": 800, "outputTokens": 50}}))
    assert p.token_detail["codex|explorer"] == {"fresh": 200, "cached": 800, "output": 50} and p.tokens["codex|explorer"] == 1050


def test_thread_config_mcp_network_and_policy():
    from dataclasses import replace
    from agent_desk.runtime.codex_app_server import thread_config
    servers = ["clickup", "hr-dev-db"]
    spec = lambda role, **kw: RunSpec("x", replace(resolve(CFG, role), **kw), "/w", "go")
    cfg, pol = thread_config(spec("explorer"), {"a": 1}, servers)                     # default mcp: none -> every server off
    assert cfg["mcp_servers"] == {"clickup": {"enabled": False}, "hr-dev-db": {"enabled": False}} and pol == "never" and cfg["a"] == 1
    cfg, _ = thread_config(spec("explorer", mcp=("clickup",)), {}, servers)
    assert cfg["mcp_servers"] == {"hr-dev-db": {"enabled": False}}
    cfg, _ = thread_config(spec("explorer", mcp="all"), {}, servers)
    assert "mcp_servers" not in cfg
    cfg, pol = thread_config(spec("worker"), {}, [])
    assert pol == "on-request" and "sandbox_workspace_write" not in cfg
    s = spec("deployer"); s.writable_roots.append("/repo/.git")
    cfg, pol = thread_config(s, {}, [])
    assert pol == "untrusted" and cfg["sandbox_workspace_write"] == {"network_access": True, "writable_roots": ["/repo/.git"]}


def test_route_lives_in_the_one_main_schema():
    assert "route" in MainTurn.model_fields and MAIN_FIELD["Route"] == "route" and set(MAIN_FIELD.values()) <= set(MainTurn.model_fields)
