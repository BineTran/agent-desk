"""EventBus: persist-then-publish. Subscribers resume by seq; consumers must be idempotent on seq."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..contracts import Event
from .store import EventStore


class EventBus:
    def __init__(self, store: EventStore):
        self.store = store
        self._subs: dict[str, list[asyncio.Queue[Event]]] = {}

    async def emit(self, ev: Event) -> Event:
        persisted = await self.store.append(ev)      # 1. commit
        for q in self._subs.get(ev.session_id, []):  # 2. publish
            q.put_nowait(persisted)
        return persisted

    def publish_live(self, ev: Event) -> None:
        """Ephemeral progress (token deltas, command output): published, never persisted, no seq."""
        for q in self._subs.get(ev.session_id, []):
            q.put_nowait(ev.model_copy(update={"seq": 0}))

    async def subscribe(self, sid: str, after: int = 0) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subs.setdefault(sid, []).append(q)     # register first so nothing is missed
        try:
            last = after
            for ev in await self.store.events(sid, after):  # replay history
                last = ev.seq
                yield ev
            while True:
                ev = await q.get()
                if ev.type.startswith("live."):
                    yield ev
                elif ev.seq > last:                    # drop duplicates from the replay/live overlap
                    last = ev.seq
                    yield ev
        finally:
            self._subs[sid].remove(q)
