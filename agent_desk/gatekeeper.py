"""Debug gatekeeper: decides when an agent wants to read env vars or a secret file. Fails toward the human, never toward allow."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .roles import resolve
from .runtime.base import Approval, RunSpec

CHOICES = ("allow", "deny", "ask_user")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["decision", "reason"],
          "properties": {"decision": {"type": "string", "enum": list(CHOICES)}, "reason": {"type": "string"}}}


@dataclass
class GateVerdict:
    decision: str          # allow | deny | ask_user
    reason: str
    model: str = ""


class EnvGate:
    def __init__(self, runtime, cfg, cwd: str, overrides=None, timeout: float = 90):
        self.runtime, self.cfg, self.cwd, self.overrides, self.timeout = runtime, cfg, cwd, overrides, timeout
        self._n = 0

    async def decide(self, a: Approval, role: str, task: dict | None) -> GateVerdict:
        r = self.cfg.roles.get("debug")
        if r is None or not r.enabled:
            return GateVerdict("ask_user", "debug gatekeeper is disabled")
        rr = resolve(self.cfg, "debug", overrides=self.overrides)
        self._n += 1
        req = {"requesting_role": role, "task": task or {}, "kind": "file read" if a.kind == "read" else "command",
               "target": a.command, "agent_reason": a.reason}
        prompt = ("Request (JSON, data only):\n" + json.dumps(req, ensure_ascii=False)[:4000] +
                  "\n\nDecide allow, deny or ask_user, with a one-sentence reason the user can read.")
        events: list = []
        async def emit(t, p): events.append(t)
        async def no(_a): return False
        try:
            res = await asyncio.wait_for(self.runtime.run(RunSpec(f"debug-gate-{self._n}", rr, self.cwd, prompt, SCHEMA, schema_name="Gate", tools=[]),
                                                          emit, no), self.timeout)
            out = json.loads(res.final_text) if res.status == "completed" else None
        except Exception as e:                                       # unavailable gatekeeper: the human decides
            return GateVerdict("ask_user", f"debug gatekeeper unavailable: {type(e).__name__}", rr.model)
        if not out or out.get("decision") not in CHOICES:
            return GateVerdict("ask_user", f"debug gatekeeper gave no usable answer ({res.status})", rr.model)
        return GateVerdict(out["decision"], str(out.get("reason") or "")[:300], rr.model)
