"""Antigravity CLI runtime (agy -p, Google subscription login).

Spike (docs/spike.md, agy 1.2.16): stream-json events are {event: init|step_update|result}. Headless print mode cannot
prompt, so every tool that needs a permission (write_file, commands not in the user's allow-list) is auto-denied:
read-only by construction, but there is no approval bridge and no write sandbox -> registered without sandbox_write.
"""
from __future__ import annotations

import asyncio
import json
import re
import signal
from dataclasses import replace

from ..config.check import ProviderInfo
from .base import WRAP_UP, ApprovalHandler, EventSink, RunResult, RunSpec

# a denied tool ends the headless turn with an empty response, so say it up front
READ_ONLY = (" You run read-only: never write, edit or delete files and never run shell commands; any such tool call is denied"
             " and ends your turn without an answer. Use only reading/search tools.")
QUOTA_RE = re.compile(r"(?i)(quota|rate limit|resource.?exhausted|limit reached)(.*?resets?\s*([^\n.]*))?")


def build_argv(binary: str, spec: RunSpec) -> list[str]:
    r = spec.role
    prompt = f"{r.card}{READ_ONLY}\n\n{spec.prompt}" if not spec.thread_id else spec.prompt   # no system-prompt flag: prepend the card
    argv = [binary, "-p", prompt, "--model", r.model, "--output-format", "stream-json", "--disable-slash-commands", "--sandbox"]
    if r.effort:
        argv += ["--effort", r.effort]
    if spec.output_schema:
        argv += ["--json-schema", json.dumps(spec.output_schema)]
    if spec.thread_id:
        argv += ["--conversation", spec.thread_id]
    return argv


def parse_models(text: str) -> dict[str, list[str]]:
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and " " not in parts[0].strip():
            out[parts[0].strip()] = []
    return out


class _Run:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.tool_calls = 0
        self.limit: str | None = None
        self.conversation = ""


class AgyCliRuntime:
    def __init__(self, binary: str = "agy"):
        self.binary = binary
        self._runs: dict[str, _Run] = {}

    async def _out(self, *args: str, timeout: float = 60) -> str:
        p = await asyncio.create_subprocess_exec(self.binary, *args, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode()

    async def info(self) -> ProviderInfo:
        hint = "agy (sign in interactively)"
        try:
            ver = (await self._out("--version", timeout=15)).strip().split()[-1]
            models = parse_models(await self._out("models"))
        except (FileNotFoundError, IndexError, asyncio.TimeoutError):
            return ProviderInfo({}, False, False, "", "", hint)
        return ProviderInfo(models, True, bool(models), "google subscription" if models else "", f"agy {ver}", hint)

    async def run(self, spec: RunSpec, emit: EventSink, approve: ApprovalHandler) -> RunResult:
        run = self._runs[spec.run_id] = _Run()
        res = RunResult("failed")
        try:
            run.proc = await asyncio.create_subprocess_exec(*build_argv(self.binary, spec), cwd=spec.cwd, stdin=asyncio.subprocess.DEVNULL,
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
                if ev.get("event") == "result":
                    final = ev.get("result") or {}
                    continue
                su = ev.get("step_update") or {}
                if su.get("conversation_id"):
                    run.conversation = su["conversation_id"]
                elif ev.get("conversation_id"):
                    run.conversation = ev["conversation_id"]
                if su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
                    run.tool_calls += 1
                await self._normalize(ev, emit)
                mx = spec.role.max_tool_calls
                if mx and run.tool_calls > mx and run.limit is None:
                    run.limit = f"{run.tool_calls} tool calls > {mx}"
                    run.proc.send_signal(signal.SIGINT)
            await run.proc.wait()
            stderr = (await err_task).decode(errors="replace")[-400:]
            res = self._result(final, stderr, run.cancelled or run.limit is not None, run.proc.returncode)
            res.thread_id = res.thread_id or run.conversation
        except FileNotFoundError:
            res = RunResult("failed", error=f"{self.binary} not found")
        finally:
            self._runs.pop(spec.run_id, None)
        await emit("agent.finished", {"status": res.status, "error": res.error, "usage": res.usage, "reset": res.reset, "limit": run.limit})
        if run.limit and not run.cancelled and res.thread_id:
            await emit("agent.limit", {"reason": run.limit})
            role = replace(spec.role, max_tool_calls=None, max_input_tokens=None)
            return await self.run(replace(spec, role=role, prompt=WRAP_UP.format(why=run.limit), thread_id=res.thread_id, tools=[]), emit, approve)
        return res

    @staticmethod
    def _result(final: dict | None, stderr: str, cancelled: bool, code: int | None) -> RunResult:
        cid = (final or {}).get("conversation_id", "")
        if cancelled:
            return RunResult("interrupted", thread_id=cid)
        if final is None:
            return RunResult("failed", error=(stderr.strip() or f"agy exited {code} without a result"))
        u = final.get("usage") or {}
        usage = {"inputTokens": u.get("input_tokens", 0) + u.get("cache_read_tokens", 0), "cachedInputTokens": u.get("cache_read_tokens", 0),
                 "outputTokens": u.get("output_tokens", 0), "reasoningOutputTokens": u.get("thinking_tokens", 0)}
        text = final.get("response") or ""
        if final.get("status") != "SUCCESS":
            err = final.get("error") or text or stderr.strip()
            if m := QUOTA_RE.search(err):
                return RunResult("quota", text, cid, usage, err[:200], (m.group(3) or "").strip() or None)
            return RunResult("failed", text, cid, usage, err[:300] or final.get("status"))
        denied = [d.get("action") for d in final.get("denied_actions") or []]
        if denied and not text.strip() and final.get("structured_output") is None:
            return RunResult("failed", "", cid, usage, f"agy denied {', '.join(denied)} (read-only runtime) and produced no answer")
        out = final.get("structured_output")
        return RunResult("completed", json.dumps(out) if out is not None else text.rstrip("\n"), cid, usage)

    async def _normalize(self, ev: dict, emit: EventSink) -> None:
        su = ev.get("step_update")
        if not su:
            return
        kind, state = su.get("step_type"), su.get("state")
        if kind == "agent_response" and su.get("text_delta"):
            await emit("live.text", {"delta": su["text_delta"]})
        elif kind == "tool" and su.get("tool_name") != "finish":
            ti = su.get("tool_info") or {}
            params = ti.get("parameters") or {}
            if state == "ACTIVE":
                cmd = (params.get("CommandLine") or params.get("AbsolutePath") or params.get("TargetFile") or params.get("Query")
                       or params.get("SearchPath") or params.get("Url") or next((str(v) for v in params.values()), None))
                await emit("tool.started", {"tool": su.get("tool_name"), "command": cmd})
            elif state in ("DONE", "ERROR"):
                err = ti.get("error")
                await emit("tool.completed", {"is_error": state == "ERROR", "tail": str(err or ti.get("output") or "")[-200:]})

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
