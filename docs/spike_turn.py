import asyncio, json, sys
from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
from agent_desk.runtime.jsonrpc import JsonRpcClient
from agent_desk.contracts import ArchitectReview

events = []
done = asyncio.Event()
def on_note(m, p):
    events.append((m, p))
    if m == "turn/completed":
        done.set()

async def on_req(m, p):
    print("SERVER REQUEST:", m, json.dumps(p)[:200]); return {"decision": "decline"}

async def main():
    c = JsonRpcClient(["codex", "app-server", "--disable", "multi_agent"], on_note, on_req)
    await c.start()
    await c.request("initialize", {"clientInfo": {"name": "agent-desk", "title": "Agent Desk", "version": "0.1.0"}})
    await c.notify("initialized")
    t = await c.request("thread/start", {"model": "gpt-5.6-luna", "cwd": ROOT, "sandbox": "read-only",
        "approvalPolicy": "never", "developerInstructions": "You are a read-only reviewer. Answer only with JSON matching the schema."})
    tid = t["thread"]["id"]; print("thread:", tid, "keys:", list(t.keys()))
    schema = ArchitectReview.model_json_schema()
    r = await c.request("turn/start", {"threadId": tid, "effort": "low", "outputSchema": schema,
        "input": [{"type": "text", "text": "Review this plan: 'add a revision column and return 409 on stale writes'. Give verdict and at most one finding."}]})
    print("turn/start ->", json.dumps(r)[:200])
    await asyncio.wait_for(done.wait(), 120)
    kinds = {}
    for m, p in events: kinds[m] = kinds.get(m, 0) + 1
    print("notification counts:", kinds)
    last = {}
    for m, p in events:
        if m == "turn/completed": print("turn.status:", p["turn"].get("status"), "| turn keys:", list(p["turn"].keys()))
        if m == "item/completed" and p.get("item", {}).get("type") == "agentMessage":
            txt = p["item"].get("text", ""); print("final text:", txt[:400])
            try: ArchitectReview.model_validate_json(txt); print("SCHEMA VALID")
            except Exception as e: print("SCHEMA INVALID:", str(e)[:200])
        if m == "thread/tokenUsage/updated": last = p
    print("tokenUsage:", json.dumps(last)[:400])
    await c.close()
asyncio.run(main())
