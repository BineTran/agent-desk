"""Render L2 memory to session.md (read-only projection; never edited by hand)."""
from __future__ import annotations

from .memory import Memory


async def render(mem: Memory, title: str = "") -> str:
    out = [f"# session {mem.sid} {title}".strip(), "", "## Brief", await mem.brief() or "(none)", "", "## Inputs"]
    for i in await mem.inputs():
        out.append(f"- {i.id} [{i.kind}] {i.ref}" + (f" sha256:{i.sha256[:10]}" if i.sha256 else ""))
    out += ["", "## Decisions in force"]
    for d in await mem.decisions_in_force():
        out.append(f"- {d.id} ({d.source}) {d.text}" + (f' — "{d.verbatim}"' if d.verbatim else ""))
    out += ["", "## Relevant files"]
    for f in await mem.files():
        out.append(f"- {f.path}:{f.lines} @{f.commit} — {f.why}" + (" [STALE]" if f.stale else ""))
    for kind, head in (("note", "Pinned notes"), ("finding", "Findings"), ("followup", "Follow-ups")):
        items = await mem.notes(kind)
        if items:
            out += ["", f"## {head}"] + [f"- {n['id']} {n['text']}" for n in items]
    return "\n".join(out) + "\n"
