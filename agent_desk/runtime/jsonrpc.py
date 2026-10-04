"""Minimal newline-delimited JSON-RPC 2.0 client over a subprocess's stdio."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

Handler = Callable[[str, dict], Awaitable[Any]]


class RpcError(Exception):
    def __init__(self, err: dict):
        super().__init__(f"{err.get('code')}: {err.get('message')}")
        self.err = err


class JsonRpcClient:
    def __init__(self, argv: list[str], on_notification: Callable[[str, dict], None] | None = None,
                 on_request: Handler | None = None, env: dict | None = None):
        self.argv, self.on_notification, self.on_request, self.env = argv, on_notification, on_request, env
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024, env=self.env)
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._drain_err())

    async def _drain_err(self) -> None:
        async for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.decode(errors="replace").rstrip()])[-50:]

    async def _read(self) -> None:
        try:
            async for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self._serve(msg))
                elif "method" in msg:
                    if self.on_notification:
                        self.on_notification(msg["method"], msg.get("params") or {})
                elif "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if "error" in msg:
                        fut.set_exception(RpcError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result"))
        finally:
            for fut in self._pending.values():       # process died: fail loudly, never leave callers hanging
                if not fut.done():
                    fut.set_exception(ConnectionError("codex app-server exited"))

    async def _serve(self, msg: dict) -> None:
        try:
            result = await self.on_request(msg["method"], msg.get("params") or {}) if self.on_request else None
            await self._send({"id": msg["id"], "result": result})
        except Exception as e:
            await self._send({"id": msg["id"], "error": {"code": -32000, "message": str(e)}})

    async def _send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None, timeout: float = 120) -> Any:
        self._id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._send({"id": self._id, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, timeout)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
