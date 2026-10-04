"""Baseline: the configured checks on the base commit, in a throwaway worktree (never the session's).

A session check failure only blocks when it is new compared to this baseline (verifier.classify). Results are cached per
repo + commit + exact command under ~/.agent-desk/baseline, so later sessions on the same commit pay nothing. The same
machinery is the check doctor: run the checks on a clean HEAD and say which commands are broken before any agent works.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from . import gitws, verifier
from .config.schema import Check, WorkspaceCfg

SAMPLE_FILES = 3


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


class BaselineRunner:
    def __init__(self, repo: Path, commit: str, workdir: Path, cache_root: Path, ws_cfg: WorkspaceCfg | None = None):
        self.repo, self.commit, self.workdir = repo.resolve(), commit, workdir
        self.cache = cache_root / _h(str(self.repo)) / commit
        self.ws_cfg = ws_cfg or WorkspaceCfg()
        self._wt: Path | None = None
        self._wt_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    async def worktree(self) -> Path:
        async with self._wt_lock:
            if self._wt is None:
                path = await gitws.add_detached(self.repo, self.workdir, self.commit)
                ws = gitws.Workspace(self.repo, path, "", self.commit)
                await gitws.link_deps(ws, self.ws_cfg.link)
                if self.ws_cfg.setup and not ws.linked:
                    await gitws.run_setup(ws, self.ws_cfg.setup)
                self._wt = path
            return self._wt

    async def get(self, name: str, check: Check, changed: list[str] | None, refresh: bool = False) -> dict:
        """{passed, exit_code, keys, summary, command, duration_ms, wrote, skipped}. Cached by the exact command."""
        if changed is not None and "{changed}" in check.command:      # unscoped commands: the base run is the same command
            wt = await self.worktree()
            changed = [f for f in changed if (wt / f).exists()]          # new files cannot fail on the base commit
            cmd, why = verifier.expand(check, changed)
            if cmd is None:
                return {"passed": True, "exit_code": 0, "keys": [], "summary": why, "command": check.command, "skipped": True}
            run_check, key = check.model_copy(update={"command": cmd, "paths": None}), cmd
        else:
            if "{changed}" in check.command:
                return {"passed": None, "keys": [], "summary": "needs the changed files", "command": check.command, "skipped": True}
            run_check, key = check, check.command
        f = self.cache / f"{_h(key)}.json"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if f.exists() and not refresh:
                try:
                    return json.loads(f.read_text())
                except json.JSONDecodeError:
                    pass
            wt = await self.worktree()
            r = await verifier.run_check(name, run_check, str(wt), guard=True)
            out = {"passed": r.passed, "exit_code": r.exit_code, "keys": r.keys, "summary": r.summary, "command": r.command,
                   "duration_ms": r.duration_ms, "wrote": r.wrote, "skipped": False, "at": time.time()}
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(out))
            return out

    async def sample(self, check: Check) -> list[str]:
        """A few tracked files the check applies to: lets the doctor try a {changed} command for real."""
        files = (await gitws.git(self.repo, "ls-tree", "-r", "--name-only", self.commit, check=False)).splitlines()
        return verifier.matching(files, check.paths or ["**/*"])[:SAMPLE_FILES]

    async def close(self) -> None:
        if self._wt is not None:
            await gitws.remove_worktree(self.repo, self._wt)
            self._wt = None


async def doctor(runner: BaselineRunner, checks: dict[str, Check], refresh: bool = False) -> list[dict]:
    """Each check on a clean checkout: ok | fails on clean checkout | slow | writes files."""
    rows = []
    for name, c in checks.items():
        files = await runner.sample(c) if "{changed}" in c.command else None
        if files == []:
            rows.append({"name": name, "state": "untested", "detail": f"no tracked files match {c.paths}", "command": c.command}); continue
        b = await runner.get(name, c, files, refresh=refresh)
        from .decision.rules import environment_error
        real = [k for k in b.get("keys") or [] if not k.startswith(f"{name}#")]     # parsed diagnostics, not a generic fingerprint
        if b.get("passed"):
            state = "ok"
        elif real and not environment_error([b.get("summary", "")]):
            state = "works (existing failures)"                     # the command runs and reports real findings: old debt, not breakage
        else:
            state = "fails on clean checkout"
        notes = []
        if b.get("duration_ms", 0) > 120_000:
            notes.append(f"slow ({b['duration_ms'] // 1000}s) — consider paths/{{changed}}")
        if b.get("wrote"):
            notes.append(f"rewrites files ({len(b['wrote'])}) — use a non-writing command or set writes: true")
        if state == "works (existing failures)":
            notes.append(f"{len(real)} failures already on {'these files' if files else 'the base commit'} — sessions only block on new ones")
        rows.append({"name": name, "state": state, "detail": "" if b.get("passed") else b.get("summary", ""),
                     "keys": len(b.get("keys") or []), "notes": notes, "command": b.get("command", c.command), "required": c.required})
    return rows
