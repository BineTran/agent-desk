#!/usr/bin/env python3
"""Fake `agy`: logs argv, replays a captured stream-json run (FAKE_MODE: ok | error | models)."""
import json, os, sys
from pathlib import Path

if log := os.environ.get("FAKE_LOG"):
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("1.2.16"); sys.exit(0)
if args[:1] == ["models"]:
    print("Fetching available models...\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)"); sys.exit(0)
if os.environ.get("FAKE_MODE") == "error":
    print(json.dumps({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "", "error": "RESOURCE_EXHAUSTED: quota exceeded, resets at 5pm"}}))
    sys.exit(1)
if os.environ.get("FAKE_MODE") == "denied":
    print(json.dumps({"event": "result", "result": {"conversation_id": "c9", "status": "SUCCESS", "response": "",
                                                    "denied_actions": [{"action": "write_file"}]}}))
    sys.exit(0)
sys.stdout.write((Path(__file__).parent / "agy_read.ndjson").read_text())
