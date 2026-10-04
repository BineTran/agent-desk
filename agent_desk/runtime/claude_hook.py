"""PreToolUse hook entry for claude-cli. FAIL-CLOSED: any error prints a deny. Run: python -m agent_desk.runtime.claude_hook"""
from __future__ import annotations

import json
import os
import socket
import sys


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


def main() -> None:
    try:
        data = json.load(sys.stdin)
        req = {"run_id": os.environ["AGENT_DESK_RUN"], "tool": data.get("tool_name"), "input": data.get("tool_input") or {}, "cwd": data.get("cwd")}
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(float(os.environ.get("AGENT_DESK_HOOK_TIMEOUT", "900")))   # a human may take a while
        s.connect(os.environ["AGENT_DESK_SOCK"])
        s.sendall((json.dumps(req) + "\n").encode())
        reply = json.loads(s.makefile().readline())
        if reply.get("allow") is True:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "agent-desk policy"}}))
        else:
            deny(reply.get("reason") or "blocked by agent-desk policy")
    except BaseException as e:                     # noqa: BLE001 — never let a failure become an implicit allow
        deny(f"agent-desk approval bridge unavailable: {type(e).__name__}")
    sys.exit(0)


if __name__ == "__main__":
    main()
