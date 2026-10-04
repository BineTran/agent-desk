from pathlib import Path

import pytest

from agent_desk.context.builder import build, est_tokens
from agent_desk.context.inputs import build_inputs, parse_mentions
from agent_desk.context.memory import Memory
from agent_desk.context.render import render
from agent_desk.contracts import Decision, FileRef, InputItem, TaskSpec
from agent_desk.events.store import EventStore

TASK = TaskSpec(id="T2", title="impl", goal="add revision check", kind="implementation", depends_on=[],
                relevant_files=["a.py"], files_known=True, requires_write=True, acceptance_criteria=["AC-1"])


@pytest.fixture
async def mem():
    s = await EventStore(":memory:").open()
    await s.create_session("s1", "/r", "t", {}, "h", "now")
    return Memory(s, "s1")


def test_parse_mentions():
    m, u = parse_mentions("see @src/a.py and @~/Downloads/p.md, https://x.io/doc.")
    assert m == ["src/a.py", "~/Downloads/p.md"] and u == ["https://x.io/doc"]


def test_outside_repo_is_snapshotted_with_hash(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "prd.md"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x")
    outside.write_text("PRD v1")
    items = build_inputs(f"@src/a.py @{outside}", repo, tmp_path / "sess", "abc123")
    assert items[0].ref == "src/a.py@abc123" and items[0].snapshot is None
    assert items[1].snapshot and Path(items[1].snapshot).read_text() == "PRD v1" and items[1].sha256
    outside.write_text("PRD v2")                       # later edits must not change what agents saw
    assert Path(items[1].snapshot).read_text() == "PRD v1"


def test_missing_mention_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_inputs("@nope.py", tmp_path, tmp_path / "s", "c")


async def test_packet_has_conclusions_not_chat_and_filters_images(mem):
    await mem.set_brief("goal")
    await mem.add_input(InputItem(id="I-1", kind="doc", ref="prd.md"))
    await mem.add_input(InputItem(id="I-2", kind="image", ref="toast.png"))
    await mem.add_decision(Decision(id="D-001", text="409 returns currentRevision", verbatim="tra currentRevision", source="user"))
    await mem.add_note("chat", "c1", "long planning conversation", "plan-studio")   # must never reach a packet
    p = await build(mem, "worker", TASK)
    assert [i.id for i in p.inputs] == ["I-1"]
    assert [d.id for d in p.decisions] == ["D-001"]
    assert "planning conversation" not in p.model_dump_json()
    assert "I-2" in (await build(mem, "main", None)).model_dump_json()


async def test_packet_budget_trims_inputs_but_keeps_failures(mem):
    await mem.set_brief("goal")
    for n in range(40):
        await mem.add_input(InputItem(id=f"I-{n}", kind="doc", ref="x" * 400))
    p = await build(mem, "worker", TASK, max_tokens=800, failure_tail=[f"line {i}" for i in range(100)])
    assert est_tokens(p) <= 800 or not p.inputs
    assert len(p.failure_tail) == 60 and p.failure_tail[-1] == "line 99"


async def test_decision_supersede_and_stale_files(mem):
    await mem.add_decision(Decision(id="D-001", text="old", source="user"))
    await mem.add_decision(Decision(id="D-002", text="new", source="user", supersedes="D-001"))
    assert [d.id for d in await mem.decisions_in_force()] == ["D-002"]
    await mem.add_file(FileRef(path="a.py", lines="1-9", why="w", commit="c1"), "run-1")
    assert await mem.mark_stale(["a.py", "zzz.py"]) == 1
    assert (await mem.files())[0].stale
    md = await render(mem)
    assert "[STALE]" in md and "D-002" in md and "D-001" not in md
