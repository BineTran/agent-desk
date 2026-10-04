"""SQLite event store: the canonical history. Persist first, publish second."""
from __future__ import annotations

import json
import os
from pathlib import Path

import aiosqlite

from ..contracts import ControlDecision, Event
from .redact import redact

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, base_commit TEXT, branch TEXT, worktree TEXT,
  title TEXT, status TEXT NOT NULL, config_json TEXT, config_hash TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL,
  source TEXT NOT NULL, type TEXT NOT NULL, task_id TEXT, agent_run_id TEXT, payload_json TEXT NOT NULL,
  UNIQUE(session_id, seq));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL,
  engine TEXT NOT NULL, policy_version TEXT, selected TEXT, confidence REAL, distribution_json TEXT,
  sharp INTEGER, input_json TEXT, task_id TEXT);
CREATE TABLE IF NOT EXISTS memory_items(
  id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', provenance TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, kind, id));
PRAGMA user_version=1;
"""


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db: aiosqlite.Connection | None = None
        self._seq: dict[str, int] = {}
        self._env = dict(os.environ)

    async def open(self) -> "EventStore":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def create_session(self, sid: str, repo: str, title: str, config_json: dict, config_hash: str, ts: str) -> None:
        await self.db.execute(
            "INSERT INTO sessions(id,repo,title,status,config_json,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, repo, title, "CREATED", json.dumps(redact(config_json, self._env)), config_hash, ts, ts))
        await self.db.commit()

    async def set_status(self, sid: str, status: str) -> None:
        await self.db.execute("UPDATE sessions SET status=?, updated_at=datetime('now') WHERE id=?", (status, sid))
        await self.db.commit()

    async def append(self, ev: Event) -> Event:
        """Assign seq, redact, commit. Returns the persisted event (publish only after this)."""
        sid = ev.session_id
        if sid not in self._seq:
            cur = await self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?", (sid,))
            self._seq[sid] = (await cur.fetchone())[0]
        self._seq[sid] += 1
        ev = ev.model_copy(update={"seq": self._seq[sid], "payload": redact(ev.payload, self._env)})
        await self.db.execute(
            "INSERT INTO events(session_id,seq,ts,source,type,task_id,agent_run_id,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (sid, ev.seq, ev.ts, ev.source, ev.type, ev.task_id, ev.agent_run_id, json.dumps(ev.payload)))
        await self.db.commit()
        return ev

    async def events(self, sid: str, after: int = 0) -> list[Event]:
        cur = await self.db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq", (sid, after))
        return [Event(session_id=r["session_id"], seq=r["seq"], ts=r["ts"], source=r["source"], type=r["type"],
                      task_id=r["task_id"], agent_run_id=r["agent_run_id"], payload=json.loads(r["payload_json"]))
                for r in await cur.fetchall()]

    async def record_decision(self, sid: str, d: ControlDecision, task_id: str | None, ts: str) -> None:
        await self.db.execute(
            "INSERT INTO decisions(session_id,ts,type,engine,policy_version,selected,confidence,distribution_json,sharp,input_json,task_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, ts, d.type, d.engine, d.policy_version, d.selected, d.confidence, json.dumps(d.distribution),
             None if d.sharp is None else int(d.sharp), json.dumps(redact(d.input_snapshot, self._env)), task_id))
        await self.db.commit()
