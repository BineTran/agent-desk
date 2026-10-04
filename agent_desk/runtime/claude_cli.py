"""Claude Code CLI runtime (claude -p, subscription login).

Isolation (Spike C): own system prompt, no MCP/slash/skills/CLAUDE.md/user settings, OS sandbox, dontAsk,
PreToolUse hook -> approvals socket. autoAllowBashIfSandboxed=false so a crashed hook falls back to DENY.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from ..config.check import ProviderInfo
from .base import WRAP_UP, Approval, ApprovalHandler, EventSink, RunResult, RunSpec

ALIASES = {"haiku": [], "sonnet": [], "opus": []}
NO_CONV_RE = re.compile(r"(?i)no conversation found")
QUOTA_RE = re.compile(r"(?i)(hit your .*?limit|usage limit|limit reached)(.*?resets?\s*([^\n.]*))?")
READ_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = READ_TOOLS + ["Edit", "Write", "Bash"]


HOOKED = "Bash|Read|Grep|Glob|mcp__.*"          # commands, file reads (secret gate) and MCP calls all reach the harness policy


def build_settings(spec: RunSpec, hook_cmd: str) -> dict:
    write = spec.role.sandbox == "workspaceWrite"
    sb = {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
          "filesystem": {"allowWrite": (["."] + list(spec.writable_roots)) if write else []},
          "network": {"allowedDomains": [] if not spec.role.network else ["*"]}}
    return {"sandbox": sb, "hooks": {"PreToolUse": [{"matcher": HOOKED, "hooks": [{"type": "command", "command": hook_cmd}]}]}}


def mcp_servers(home: str | None, cwd: str) -> dict[str, dict]:
    """The user's Claude MCP servers: ~/.claude.json (user scope + this project) and the repo's .mcp.json."""
    out: dict[str, dict] = {}
    cfg = Path(home) / ".claude.json" if home else Path.home() / ".claude.json"
    for path, pick in ((cfg, lambda d: {**(d.get("mcpServers") or {}), **((d.get("projects") or {}).get(cwd, {}).get("mcpServers") or {})}),
                       (Path(cwd) / ".mcp.json", lambda d: d.get("mcpServers") or {})):
        try:
            out.update(pick(json.loads(path.read_text())))
        except (OSError, ValueError, AttributeError):
            pass
    return out


def mcp_args(spec: RunSpec, home: str | None = None) -> list[str]:
    """none: no MCP at all; all: the user's servers and connectors; a list: only those servers."""
    m = spec.role.mcp
    if m == "all":
        return []
    if m == "none" or not m:
        return ["--strict-mcp-config"]
    found = mcp_servers(home, spec.cwd)
    return ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {k: v for k, v in found.items() if k in m}})]


def build_argv(binary: str, spec: RunSpec, settings: dict, home: str | None = None) -> list[str]:
    r = spec.role
    write = r.sandbox == "workspaceWrite"
    tools = spec.tools if spec.tools is not None else list(WRITE_TOOLS if write else READ_TOOLS) + (["WebFetch", "WebSearch"] if r.network else [])
    argv = [binary, "-p", spec.prompt, "--model", r.model, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--system-prompt", r.card, *mcp_args(spec, home), "--disable-slash-commands", "--setting-sources", "",
            "--permission-mode", "dontAsk", "--tools", ",".join(tools), "--settings", json.dumps(settings)]
    if allowed := [t for t in tools if t != "Bash"]:
        argv += ["--allowedTools", ",".join(allowed)]
    if r.effort:
        argv += ["--effort", r.effort]
    if spec.output_schema:
        argv += ["--json-schema", json.dumps(spec.output_schema)]
    if spec.thread_id:
        argv += ["--resume", spec.thread_id]
    return argv


class _Run:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.tool_calls = 0
        self.limit: str | None = None
        self.session_id = ""


class ClaudeCliRuntime:
    def __init__(self, binary: str = "claude", hook_python: str | None = None, home: str | None = None):
        self.binary, self.home = binary, home
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": home} if home else None
        self.hook_python = hook_python or sys.executable
        self._runs: dict[str, _Run] = {}

    async def _out(self, *args: str) -> str:
        p = await asyncio.create_subprocess_exec(self.binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self.env)
        out, _ = await p.communicate()
        return out.decode()

    async def info(self) -> ProviderInfo:
        try:
            st = json.loads(await self._out("auth", "status"))
            ver = (await self._out("--version")).split()[0]
        except (FileNotFoundError, json.JSONDecodeError, IndexError):
            return ProviderInfo({}, False, False, "", "", self._hint())
        ident = " · ".join(x for x in (st.get("email"), st.get("orgName")) if x) if st.get("loggedIn") else ""
        acct = " ".join(x for x in (ident, st.get("subscriptionType")) if x) or st.get("authMethod", "")
        return ProviderInfo(dict(ALIASES), True, bool(st.get("loggedIn")), acct, f"claude {ver}", self._hint(), ident)

    def _hint(self) -> str:
        return f"CLAUDE_CONFIG_DIR={self.home} claude auth login" if self.home else "claude auth login"

    system_home = "~/.claude"                    # the user's everyday profile: Agent Desk never logs in/out there

    def login_command(self, email: str | None = None, browser: str | None = None) -> tuple[list[str], dict | None, bool]:
        """(argv, env, opens_url_itself). claude opens the sign-in URL with $BROWSER when set (verified in 2.1.236)."""
        env = {**(self.env or os.environ), "BROWSER": browser} if browser else self.env
        return [self.binary, "auth", "login"] + (["--email", email] if email else []), env, True

    def logout_command(self) -> tuple[list[str], dict | None]:
        return [self.binary, "auth", "logout"], self.env

    # ---- approvals socket ----
    async def _serve(self, sock_path: str, approve: ApprovalHandler) -> asyncio.AbstractServer:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                req = json.loads(await reader.readline())
                inp, tool = req.get("input") or {}, req.get("tool") or "Bash"
                if tool.startswith("mcp__"):
                    kind, cmd = "mcp", tool
                elif tool in ("Read", "Grep", "Glob"):
                    kind, cmd = "read", inp.get("file_path") or inp.get("path") or (inp.get("pattern") if tool == "Glob" else "") or ""
                else:
                    kind, cmd = "command", inp.get("command") or inp.get("file_path") or ""
                ok = await approve(Approval(req.get("run_id", ""), kind, cmd, req.get("cwd"), inp.get("description")))
                writer.write((json.dumps({"allow": bool(ok), "reason": None if ok else "denied by policy or user"}) + "\n").encode())
                await writer.drain()
            except Exception:
                writer.write((json.dumps({"allow": False, "reason": "bridge error"}) + "\n").encode())
            finally:
                writer.close()
        return await asyncio.start_unix_server(handle, path=sock_path)

    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult:
        run = self._runs[spec.run_id] = _Run()
        tmp = tempfile.mkdtemp(prefix="ad-")                    # short path: unix sockets are limited to ~104 chars
        sock = os.path.join(tmp, "a.sock")
        server = await self._serve(sock, approve)
        hook_cmd = f"AGENT_DESK_RUN={spec.run_id} AGENT_DESK_SOCK={sock} {self.hook_python} -m agent_desk.runtime.claude_hook"
        argv = build_argv(self.binary, spec, build_settings(spec, hook_cmd), self.home)
        res = RunResult("failed")
        try:
            run.proc = await asyncio.create_subprocess_exec(*argv, cwd=spec.cwd, stdin=asyncio.subprocess.DEVNULL, env=self.env,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                            limit=16 * 1024 * 1024)
            err_task = asyncio.create_task(run.proc.stderr.read())
            await emit("agent.started", {"model": spec.role.model, "provider": spec.role.provider})
            final: dict | None = None
            async for raw in run.proc.stdout:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("session_id"):
                    run.session_id = ev["session_id"]
                if ev.get("type") == "result":
                    final = ev
                elif ev.get("type") == "rate_limit_event":     # the only quota signal the CLI gives: status + reset, no percent
                    await emit("provider.limit", {"provider": spec.role.provider, **(ev.get("rate_limit_info") or {})})
                else:
                    await self._normalize(ev, emit)
                    run.tool_calls += sum(1 for b in ((ev.get("message") or {}).get("content") or [])
                                          if ev.get("type") == "assistant" and isinstance(b, dict) and b.get("type") == "tool_use")
                    mx = spec.role.max_tool_calls
                    if mx and run.tool_calls > mx and run.limit is None:
                        run.limit = f"{run.tool_calls} tool calls > {mx}"
                        run.proc.send_signal(signal.SIGINT)
            await run.proc.wait()
            stderr = (await err_task).decode(errors="replace")[-400:]
            res = self._result(final, stderr, run.cancelled or run.limit is not None, run.proc.returncode, resumed=bool(spec.thread_id))
            res.thread_id = res.thread_id or run.session_id
        except FileNotFoundError:
            res = RunResult("failed", error=f"{self.binary} not found")
        finally:
            server.close()
            self._runs.pop(spec.run_id, None)
            try:
                os.unlink(sock); os.rmdir(tmp)
            except OSError:
                pass
        await emit("agent.finished", {"status": res.status, "error": res.error, "usage": res.usage, "reset": res.reset, "limit": run.limit})
        if run.limit and not run.cancelled and res.thread_id:
            await emit("agent.limit", {"reason": run.limit})
            role = replace(spec.role, max_tool_calls=None, max_input_tokens=None)
            return await self.run(replace(spec, role=role, prompt=WRAP_UP.format(why=run.limit), thread_id=res.thread_id, tools=[]), emit, approve)
        return res

    @staticmethod
    def _result(final: dict | None, stderr: str, cancelled: bool, code: int | None, resumed: bool = False) -> RunResult:
        if cancelled:
            return RunResult("interrupted", thread_id=(final or {}).get("session_id", ""))
        if final is None:
            msg = stderr.strip() or f"claude exited {code} without a result"
            return RunResult("failed", error=f"resume failed: {msg}" if resumed and NO_CONV_RE.search(msg) else msg)
        u = final.get("usage") or {}
        usage = {"inputTokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                 "cachedInputTokens": u.get("cache_read_input_tokens", 0), "outputTokens": u.get("output_tokens", 0), "reasoningOutputTokens": 0}
        text = final.get("result") or ""
        sid = final.get("session_id", "")
        if final.get("is_error") or final.get("subtype") != "success":
            m = QUOTA_RE.search(text) or QUOTA_RE.search(stderr)
            if m:
                return RunResult("quota", text, sid, usage, text[:200], (m.group(3) or "").strip() or None)
            errs = final.get("errors")
            msg = ("; ".join(map(str, errs)) if isinstance(errs, list) and errs else str(errs or "")) or text or final.get("subtype") or ""
            if resumed and (NO_CONV_RE.search(msg) or NO_CONV_RE.search(stderr)):   # the profile no longer has the thread
                return RunResult("failed", text, "", usage, f"resume failed: {msg[:300]}")
            return RunResult("failed", text, sid, usage, msg[:300])
        out = final.get("structured_output")
        return RunResult("completed", json.dumps(out) if out is not None else text, sid, usage)

    async def _normalize(self, ev: dict, emit: EventSink) -> None:
        t = ev.get("type")
        if t == "stream_event":                                  # --include-partial-messages: token deltas, live only
            se = ev.get("event") or {}
            if se.get("type") == "content_block_delta":
                d = se.get("delta") or {}
                kind = {"text_delta": ("live.text", "text"), "thinking_delta": ("live.reasoning", "thinking"),
                        "input_json_delta": ("live.text", "partial_json")}.get(d.get("type"))
                if kind and d.get(kind[1]):
                    await emit(kind[0], {"delta": d[kind[1]]})
            return
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for b in content:
            if t == "assistant" and b.get("type") == "tool_use" and b.get("name") != "StructuredOutput":
                inp = b.get("input") or {}
                await emit("tool.started", {"tool": b.get("name"), "command": inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or inp.get("query")})
            elif t == "assistant" and b.get("type") == "text" and b.get("text", "").strip():
                await emit("agent.message", {"text": b["text"][:500]})
            elif t == "assistant" and b.get("type") == "thinking" and b.get("thinking", "").strip():
                await emit("agent.reasoning", {"text": b["thinking"][-600:]})
            elif t == "user" and b.get("type") == "tool_result":
                await emit("tool.completed", {"is_error": bool(b.get("is_error")), "tail": str(b.get("content"))[-200:]})

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if not run or not run.proc or run.proc.returncode is not None:
            return
        run.cancelled = True
        run.proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(run.proc.wait(), 3)
        except asyncio.TimeoutError:
            run.proc.terminate()

    async def close(self) -> None:
        for rid in list(self._runs):
            await self.cancel(rid)
