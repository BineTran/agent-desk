"""L2 session memory (curated, provenance-tagged) backed by SQLite memory_items."""
from __future__ import annotations

import json
from typing import Iterable

from ..contracts import Decision, FileRef, InputItem, now
from ..events.store import EventStore


class Memory:
    def __init__(self, store: EventStore, sid: str):
        self.s, self.sid = store, sid

    async def _put(self, kind: str, id_: str, data: dict, provenance: str | None = None, status: str = "active") -> None:
        await self.s.db.execute(
            "INSERT INTO memory_items(id,session_id,kind,data_json,status,provenance,updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id,kind,id) DO UPDATE SET data_json=excluded.data_json,status=excluded.status,"
            "provenance=excluded.provenance,updated_at=excluded.updated_at",
            (id_, self.sid, kind, json.dumps(data), status, provenance, now()))
        await self.s.db.commit()

    async def _all(self, kind: str, status: str | None = "active") -> list[dict]:
        q = "SELECT id,data_json,status,provenance FROM memory_items WHERE session_id=? AND kind=?"
        args: list = [self.sid, kind]
        if status:
            q += " AND status=?"
            args.append(status)
        cur = await self.s.db.execute(q + " ORDER BY rowid", args)
        return [{"id": r["id"], "status": r["status"], "prov": r["provenance"], **json.loads(r["data_json"])} for r in await cur.fetchall()]

    # brief / acceptance
    async def set_brief(self, text: str) -> None:
        await self._put("brief", "brief", {"text": text})

    async def brief(self) -> str:
        r = await self._all("brief")
        return r[0]["text"] if r else ""

    # inputs
    async def add_input(self, i: InputItem) -> None:
        await self._put("input", i.id, i.model_dump())

    async def inputs(self) -> list[InputItem]:
        return [InputItem(**{k: v for k, v in r.items() if k not in ("status", "prov")}) for r in await self._all("input")]

    # decisions (immutable; superseded rather than edited)
    async def add_decision(self, d: Decision) -> None:
        if d.supersedes:
            old = [r for r in await self._all("decision", None) if r["id"] == d.supersedes]
            if old:
                await self._put("decision", d.supersedes, {k: v for k, v in old[0].items() if k not in ("id", "status", "prov")},
                                status=f"superseded by {d.id}")
        await self._put("decision", d.id, d.model_dump())

    async def decisions_in_force(self) -> list[Decision]:
        return [Decision(**{k: v for k, v in r.items() if k not in ("status", "prov")}) for r in await self._all("decision")]

    async def next_decision_id(self) -> str:
        return f"D-{len(await self._all('decision', None)) + 1:03d}"

    # relevant files
    async def add_file(self, f: FileRef, provenance: str) -> None:
        await self._put("file", f"{f.path}", f.model_dump(), provenance)

    async def files(self) -> list[FileRef]:
        return [FileRef(path=r["path"], lines=r["lines"], why=r["why"], commit=r["commit"], stale=r.get("stale", False))
                for r in await self._all("file")]

    async def mark_stale(self, changed: Iterable[str]) -> int:
        n = 0
        changed = set(changed)
        for f in await self.files():
            if f.path in changed and not f.stale:
                await self._put("file", f.path, f.model_copy(update={"stale": True}).model_dump())
                n += 1
        return n

    # findings / notes / follow-ups
    async def add_note(self, kind: str, id_: str, text: str, provenance: str) -> None:
        await self._put(kind, id_, {"text": text}, provenance)

    async def notes(self, kind: str) -> list[dict]:
        return await self._all(kind)
