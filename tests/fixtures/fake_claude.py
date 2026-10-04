#!/usr/bin/env python3
"""Fake `claude` CLI: records argv, emulates stream-json, honours FAKE_MODE."""
import json, os, sys, time
argv = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(argv) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "tools": []})
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1791034200, "rateLimitType": "five_hour"}})
for _ in range(3): out({"type": "system", "subtype": "thinking_tokens"})
for d in ({"type": "thinking_delta", "thinking": "Let me "}, {"type": "thinking_delta", "thinking": "check."}, {"type": "text_delta", "text": "Running tests"}):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": d}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Running tests"}]}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pnpm test"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok", "is_error": False}]}})
if mode == "slow":
    time.sleep(30)
if mode == "quota":
    out({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your session limit · resets 3:45pm", "session_id": "S1", "usage": {}})
    sys.exit(1)
if mode == "error":
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom", "session_id": "S1", "usage": {}})
    sys.exit(1)
if mode == "noconv":
    print("No conversation found with session ID: X", file=sys.stderr)
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "X", "usage": {},
         "errors": ["No conversation found with session ID: X"]})
    sys.exit(1)
if mode == "nores":
    sys.exit(3)
if mode == "hook":
    # emulate the CLI running our PreToolUse hook for a command given in FAKE_CMD
    import subprocess
    env = dict(os.environ); 
    settings = json.loads(argv[argv.index("--settings") + 1])
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    p = subprocess.run(cmd, shell=True, input=json.dumps({"tool_name": "Bash", "tool_input": {"command": os.environ["FAKE_CMD"]}, "cwd": os.getcwd()}), capture_output=True, text=True)
    open(os.environ["FAKE_LOG"] + ".hook", "w").write(p.stdout)
out({"type": "result", "subtype": "success", "is_error": False, "result": "plain text", "structured_output": {"status": "completed", "ok": True},
     "session_id": "S1", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 7}})
