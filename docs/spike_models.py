import asyncio, json, sys
from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
from agent_desk.runtime.jsonrpc import JsonRpcClient

async def main():
    notes = []
    c = JsonRpcClient(["codex", "app-server", "--disable", "multi_agent"], on_notification=lambda m, p: notes.append(m))
    await c.start()
    init = await c.request("initialize", {"clientInfo": {"name": "agent-desk", "title": "Agent Desk", "version": "0.1.0"}})
    print("initialize ->", json.dumps(init)[:300])
    await c.notify("initialized")
    ml = await c.request("model/list", {})
    print("model/list keys:", list(ml.keys()))
    for m in (ml.get("data") or ml.get("models") or [])[:12]:
        print(" ", {k: m.get(k) for k in ("id", "model", "displayName", "defaultReasoningEffort", "supportedReasoningEfforts") if k in m})
    try:
        print("caps ->", json.dumps(await c.request("modelProvider/capabilities/read", {})))
    except Exception as e:
        print("caps err", e)
    print("notifications seen:", sorted(set(notes)))
    await c.close()

asyncio.run(main())
