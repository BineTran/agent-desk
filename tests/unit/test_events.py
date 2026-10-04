import asyncio

from agent_desk.contracts import ControlDecision, Event
from agent_desk.events.bus import EventBus
from agent_desk.events.redact import redact_text
from agent_desk.events.store import EventStore


async def mk():
    s = await EventStore(":memory:").open()
    await s.create_session("s1", "/r", "t", {"a": 1}, "h", "now")
    return s, EventBus(s)


async def test_seq_monotonic_and_replay_order():
    s, bus = await mk()
    for i in range(5):
        await bus.emit(Event(session_id="s1", source="t", type="x", payload={"i": i}))
    evs = await s.events("s1")
    assert [e.seq for e in evs] == [1, 2, 3, 4, 5]
    assert [e.seq for e in await s.events("s1", after=3)] == [4, 5]


async def test_persist_before_publish_and_no_duplicates_on_resubscribe():
    s, bus = await mk()
    await bus.emit(Event(session_id="s1", source="t", type="a"))
    got = []

    async def sub():
        async for e in bus.subscribe("s1"):
            got.append(e.seq)
            if len(got) == 3:
                return
    t = asyncio.create_task(sub())
    await asyncio.sleep(0.01)
    await bus.emit(Event(session_id="s1", source="t", type="b"))
    await bus.emit(Event(session_id="s1", source="t", type="c"))
    await asyncio.wait_for(t, 1)
    assert got == [1, 2, 3]
    assert len(await s.events("s1")) == 3          # everything published was already persisted


async def test_payload_redacted_before_persist():
    s, bus = await mk()
    await bus.emit(Event(session_id="s1", source="t", type="x",
                         payload={"cmd": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'", "token": "hunter2hunter2"}))
    p = (await s.events("s1"))[0].payload
    assert "abcdefghij" not in str(p) and "hunter2" not in str(p)


async def test_decision_recorded():
    s, _ = await mk()
    await s.record_decision("s1", ControlDecision(type="route", engine="jev", selected="worker", confidence=0.94,
                                                  distribution={"worker": 0.94}, sharp=True), "T1", "now")
    cur = await s.db.execute("SELECT engine, sharp FROM decisions")
    r = await cur.fetchone()
    assert (r["engine"], r["sharp"]) == ("jev", 1)


def test_redact_text():
    assert "sk-" not in redact_text("key sk-abcdefghijklmnopqrstuv end")
