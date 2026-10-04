"""Codex app-server runtime. One process, one thread per agent run; success only on turn.status == completed.

Isolation (Spike M7): threads never see the repo's AGENTS.md or the user's skills (the packet is the only repo context),
effort is always explicit (never ~/.codex/config.toml), usage is reported per run, and a role's limits interrupt the run
and ask for a report with what the agent already has.
"""
from __future__ import annotations

import asyncio
import os
import tomllib
from pathlib import Path
from typing import Any

from .base import WRAP_UP, Approval, ApprovalHandler, EventSink, RunResult, RunSpec
from .jsonrpc import JsonRpcClient, RpcError

SANDBOX = {"readOnly": "read-only", "workspaceWrite": "workspace-write"}


def mcp_server_names(home: str | None) -> list[str]:
    """MCP servers in the profile's config.toml. Threads inherit them unless a thread config turns them off."""
    path = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        return list((tomllib.loads(path.read_text()).get("mcp_servers") or {}).keys())
    except (OSError, tomllib.TOMLDecodeError):
        return []


def thread_config(spec: RunSpec, base: dict, servers: list[str]) -> tuple[dict, str]:
    """(config overrides, approvalPolicy) for one thread: role's MCP servers, network and extra writable roots.
    A writer with network (deployer) runs under 'untrusted' so every non-trivial command reaches the harness policy."""
    r = spec.role
    cfg = dict(base)
    allowed = servers if r.mcp == "all" else [] if r.mcp in ("none", "", None) else [s for s in servers if s in r.mcp]
    if off := [s for s in servers if s not in allowed]:
        cfg["mcp_servers"] = {s: {"enabled": False} for s in off}
    write = r.sandbox == "workspaceWrite"
    if write and (r.network or spec.writable_roots):
        cfg["sandbox_workspace_write"] = {"network_access": bool(r.network), "writable_roots": list(spec.writable_roots)}
    policy = ("untrusted" if r.network else "on-request") if write else "never"
    return cfg, policy
CLIENT = {"name": "agent-desk", "title": "Agent Desk", "version": "0.1.0"}
DEFAULT_EFFORT = "medium"
TOOL_ITEMS = ("commandExecution", "fileChange", "webSearch", "mcpToolCall")


class _Run:
    def __init__(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler):
        self.spec, self.emit, self.approve = spec, emit, approve
        self.thread_id = ""
        self.turn_id = ""
        self.done: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self.last_message = ""
        self.usage: dict[str, int] = {}
        self.base: dict[str, int] | None = None          # thread totals before this run (Codex reports cumulative totals)
        self.tool_calls = 0
        self.limit: str | None = None                    # why the budget tripped
        self.interrupting = False
        self.wrapping = False                            # in the wrap-up turn: limits no longer apply
        self.cancelled = False                           # a user stop is never mistaken for a budget interrupt
        self.q: asyncio.Queue = asyncio.Queue()             # notifications are emitted in arrival order (deltas must not interleave)
        self.pump: asyncio.Task | None = None

    async def drain(self) -> None:
        while True:
            t, p = await self.q.get()
            try:
                await self.emit(t, p)
            except Exception:
                pass
            finally:
                self.q.task_done()


class CodexAppServerRuntime:
    def __init__(self, argv: list[str] | None = None, extra_config: list[str] | None = None, home: str | None = None):
        argv = argv or ["codex", "app-server", "--disable", "multi_agent"]
        for c in extra_config or []:
            argv += ["-c", c]
        self.home, self.binary = home, argv[0]
        self.env = {**os.environ, "CODEX_HOME": home} if home else None   # another account: its own auth.json/sessions
        self.rpc = JsonRpcClient(argv, self._on_note, self._on_request, env=self.env)
        self._by_thread: dict[str, _Run] = {}
        self._by_run: dict[str, _Run] = {}
        self._started = False
        self.user_agent = ""
        self._lock = asyncio.Lock()
        self._known: set[str] = set()                    # threads started or resumed in THIS app-server process
        self._isolation: dict[str, dict] = {}            # cwd -> thread config

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            await self.rpc.start()
            self.user_agent = (await self.rpc.request("initialize", {"clientInfo": CLIENT})).get("userAgent", "")
            await self.rpc.notify("initialized")
            self._started = True

    async def list_models(self) -> dict[str, list[str]]:
        await self.start()
        res = await self.rpc.request("model/list", {})
        return {m["id"]: [e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])] for m in res["data"]}

    async def info(self):
        from ..config.check import ProviderInfo
        await self.start()
        acct = (await self.rpc.request("account/read", {})).get("account")
        caps = await self.capabilities()
        ver = (self.user_agent.split("/")[1].split(" ")[0]) if "/" in self.user_agent else ""
        return ProviderInfo(await self.list_models(), bool(caps.get("webSearch")), acct is not None,
                            " ".join(x for x in (acct.get("email"), acct["type"], acct.get("planType")) if x) if acct else "",
                            f"codex {ver}", f"CODEX_HOME={self.home} codex login" if self.home else "codex login",
                            (acct.get("email") or acct["type"]) if acct else "")

    system_home = "~/.codex"

    def login_command(self, email: str | None = None, browser: str | None = None) -> tuple[list[str], dict | None, bool]:
        """codex opens the browser through LaunchServices (ignores $BROWSER): for a chosen browser use the device flow,
        whose printed URL the caller opens itself."""
        if browser:
            return [self.binary, "login", "--device-auth"], self.env, False
        return [self.binary, "login"], self.env, True

    def logout_command(self) -> tuple[list[str], dict | None]:
        return [self.binary, "logout"], self.env

    async def quota(self) -> dict[str, float]:
        """Percent used of the 5h and weekly subscription windows."""
        await self.start()
        rl = (await self.rpc.request("account/rateLimits/read", {}))["rateLimits"]
        return {k: float(rl[k]["usedPercent"]) for k in ("primary", "secondary") if rl.get(k)}

    async def capabilities(self) -> dict[str, Any]:
        await self.start()
        return await self.rpc.request("modelProvider/capabilities/read", {})

    # ---- server -> us ----
    def _on_note(self, method: str, p: dict) -> None:
        tid = p.get("threadId") or (p.get("thread") or {}).get("id")
        run = self._by_thread.get(tid or "")
        if not run:
            return
        if method == "turn/completed":
            if not run.done.done():
                run.done.set_result(p["turn"])
        elif method == "item/completed" and p["item"].get("type") == "agentMessage":
            run.last_message = p["item"].get("text", "")
        elif method == "thread/tokenUsage/updated":
            self._account(run, p["tokenUsage"])
        elif method == "item/started" and (p.get("item") or {}).get("type") in TOOL_ITEMS:
            run.tool_calls += 1
        self._check_limits(run)
        for t, payload in self._normalize(run, method, p):
            run.q.put_nowait((t, payload))

    @staticmethod
    def _account(run: _Run, tu: dict) -> None:
        """Per-run usage = thread total now - thread total before this run's first model call."""
        keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
        tot, last = tu.get("total") or {}, tu.get("last") or {}
        if run.base is None:
            run.base = {k: tot.get(k, 0) - last.get(k, 0) for k in keys}
        run.usage = {k: tot.get(k, 0) - run.base[k] for k in keys}
        run.usage["contextTokens"] = last.get("inputTokens", 0)          # size of the context right now
        if tu.get("modelContextWindow"):
            run.usage["contextWindow"] = tu["modelContextWindow"]

    def _check_limits(self, run: _Run) -> None:
        r = run.spec.role
        if run.wrapping:
            return
        if run.limit is None:
            if r.max_tool_calls and run.tool_calls > r.max_tool_calls:
                run.limit = f"{run.tool_calls} tool calls > {r.max_tool_calls}"
            elif r.max_input_tokens and run.usage.get("inputTokens", 0) > r.max_input_tokens:
                run.limit = f"{run.usage['inputTokens']:,} input tokens > {r.max_input_tokens:,}"
        if run.limit and not run.interrupting and run.turn_id and not run.done.done():
            run.interrupting = True
            asyncio.get_running_loop().create_task(self._interrupt(run))

    async def _interrupt(self, run: _Run) -> None:
        try:
            await self.rpc.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
        except Exception:
            pass

    @staticmethod
    def _normalize(run: _Run, method: str, p: dict) -> list[tuple[str, dict]]:
        it = p.get("item") or {}
        typ = it.get("type")
        if method == "item/agentMessage/delta":
            return [("live.text", {"delta": p.get("delta", "")})]
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            return [("live.reasoning", {"delta": p.get("delta", "")})]
        if method == "item/reasoning/summaryPartAdded":
            return [("live.reasoning", {"delta": "\n"})]        # keep summary parts on separate lines
        if method == "item/commandExecution/outputDelta":
            return [("live.output", {"delta": p.get("delta", "")})]
        if method == "turn/diff/updated":
            return [("diff.updated", {"diff": p.get("diff", "")[:200_000]})]
        if method == "thread/tokenUsage/updated":
            return [("usage.updated", run.usage)]
        if method == "item/started":
            if typ == "commandExecution":
                return [("tool.started", {"command": it.get("command")})]
            if typ == "fileChange":
                return [("tool.started", {"tool": "edit", "command": ", ".join(c.get("path", "") for c in it.get("changes") or [])})]
            if typ == "webSearch":
                return [("tool.started", {"tool": "search", "command": it.get("query")})]
        if method == "item/completed":
            if typ == "commandExecution":
                return [("tool.completed", {"command": it.get("command"), "exit_code": it.get("exitCode")})]
            if typ == "agentMessage" and it.get("text", "").strip():
                return [("agent.message", {"text": it["text"][:500]})]   # closes this message's live text
            if typ == "reasoning":
                text = "\n".join(x if isinstance(x, str) else str(x.get("text", "")) for x in it.get("summary") or [])
                return [("agent.reasoning", {"text": text[-600:]})] if text.strip() else []
        return []

    async def _on_request(self, method: str, p: dict) -> dict:
        run = self._by_thread.get(p.get("threadId", ""))
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval") and run:
            kind = "command" if "command" in method else "file_change"
            ok = await run.approve(Approval(run.spec.run_id, kind, p.get("command"), p.get("cwd"), p.get("reason")))
            return {"decision": "accept" if ok else "decline"}
        return {"decision": "decline"}          # unknown server request: refuse, never guess

    # ---- us -> server ----
    async def isolation(self, cwd: str) -> dict:
        """Per-thread config: no AGENTS.md, no skills catalogue (25k+ chars each, re-sent on every model call)."""
        if cwd not in self._isolation:
            cfg: dict[str, Any] = {"project_doc_max_bytes": 0}
            try:
                res = await self.rpc.request("skills/list", {"cwds": [cwd]})
                skills = [s for e in res.get("data", []) for s in e.get("skills", []) if s.get("enabled")]
                if skills:
                    cfg["skills"] = {"config": [{"name": s["name"], "enabled": False} for s in skills]}
            except RpcError:
                pass
            self._isolation[cwd] = cfg
        return self._isolation[cwd]

    async def _open_thread(self, spec: RunSpec) -> str:
        r = spec.role
        cfg, policy = thread_config(spec, await self.isolation(spec.cwd), mcp_server_names(self.home))
        common = {"cwd": spec.cwd, "sandbox": SANDBOX[r.sandbox], "config": cfg, "approvalPolicy": policy, "developerInstructions": r.card}
        if spec.thread_id:
            if spec.thread_id not in self._known:          # a thread from an earlier process (resumed session)
                await self.rpc.request("thread/resume", {"threadId": spec.thread_id, "excludeTurns": True, **common})
                self._known.add(spec.thread_id)
            return spec.thread_id
        t = await self.rpc.request("thread/start", {"model": r.model, **common})
        self._known.add(t["thread"]["id"])
        return t["thread"]["id"]

    async def _turn(self, run: _Run, text: str) -> dict:
        r, spec = run.spec.role, run.spec
        run.done = asyncio.get_running_loop().create_future()
        run.turn_id = ""
        inp: list[dict] = [{"type": "text", "text": text}] + [{"type": "localImage", "path": i} for i in spec.images]
        params: dict[str, Any] = {"threadId": run.thread_id, "input": inp, "summary": "auto",     # reasoning summaries stream to the UI
                                  "effort": r.effort or DEFAULT_EFFORT}
        if spec.output_schema:
            params["outputSchema"] = spec.output_schema
        started = await self.rpc.request("turn/start", params)
        run.turn_id = started["turn"]["id"]
        self._check_limits(run)                           # a limit may have tripped before we knew the turn id
        return await run.done

    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult:
        await self.start()
        run = _Run(spec, emit, approve)
        self._by_run[spec.run_id] = run
        r = spec.role
        try:
            try:
                run.thread_id = await self._open_thread(spec)
            except RpcError as e:
                if not spec.thread_id:
                    raise
                res = RunResult("failed", error=f"resume failed: {e}")
                await emit("agent.finished", {"status": res.status, "error": res.error, "usage": {}})
                return res
            self._by_thread[run.thread_id] = run
            run.pump = asyncio.create_task(run.drain())
            await emit("agent.started", {"thread_id": run.thread_id, "model": r.model, "provider": r.provider, "effort": r.effort or DEFAULT_EFFORT})
            turn = await self._turn(run, spec.prompt)
            if run.limit and turn.get("status") == "interrupted" and not run.cancelled:
                await emit("agent.limit", {"reason": run.limit})
                run.wrapping = True
                turn = await self._turn(run, WRAP_UP.format(why=run.limit))
            status = turn.get("status")
            err = (turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else turn.get("error")
            res = RunResult("completed" if status == "completed" else "interrupted" if status == "interrupted" else "failed",
                            run.last_message, run.thread_id, run.usage, err)
        except (RpcError, ConnectionError, asyncio.TimeoutError) as e:
            res = RunResult("failed", run.last_message, run.thread_id, run.usage, str(e))
        finally:
            self._by_thread.pop(run.thread_id, None)
            self._by_run.pop(spec.run_id, None)
            if run.pump:
                try:
                    await asyncio.wait_for(run.q.join(), 5)     # everything the turn produced is emitted before agent.finished
                except asyncio.TimeoutError:
                    pass
                run.pump.cancel()
        await emit("agent.finished", {"status": res.status, "error": res.error, "usage": res.usage, "limit": run.limit})
        return res

    async def cancel(self, run_id: str) -> None:
        run = self._by_run.get(run_id)
        if run:
            run.cancelled = True
        if run and run.turn_id:
            await self.rpc.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})

    async def close(self) -> None:
        await self.rpc.close()
