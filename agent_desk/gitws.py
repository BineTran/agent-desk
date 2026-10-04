"""One worktree + branch per session. Never touches the user's checkout, never pushes."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


class GitError(Exception):
    pass


async def git(cwd: Path | str, *args: str, check: bool = True, raw: bool = False) -> str:
    p = await asyncio.create_subprocess_exec("git", "-c", "user.name=agent-desk", "-c", "user.email=agent-desk@local", *args,
                                             cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if check and p.returncode:
        raise GitError(f"git {' '.join(args)}: {err.decode().strip()}")
    return out.decode() if raw else out.decode().strip()


async def common_dir(ws: "Workspace") -> str:
    """The repository's shared git dir (refs, objects): a worktree's merges and pushes write there, outside the worktree."""
    d = Path(await git(ws.path, "rev-parse", "--git-common-dir"))
    return str(d if d.is_absolute() else (Path(ws.path) / d).resolve())


@dataclass
class Workspace:
    repo: Path
    path: Path
    branch: str
    base_commit: str
    linked: list[str] = field(default_factory=list)      # dependency dirs symlinked from the user's checkout


def linked_paths(ws: Workspace) -> list[str]:
    """Symlinks in the worktree that point into the user's checkout (made by link_deps); found on disk so reopened sessions know them."""
    out, root = [], ws.path
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0] == ".git" or len(rel.parts) >= 3:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for n in dirnames + filenames:
            full = Path(dirpath) / n
            if full.is_symlink() and str(os.path.realpath(full)).startswith(str(ws.repo.resolve()) + os.sep):
                out.append(str((rel / n).as_posix()))
    return sorted(set(out))


async def _add_all(ws: Workspace) -> None:
    """git add -A without our dependency links. Only links .gitignore does not already cover get an exclude:
    naming an ignored path in a pathspec makes git add fail ('paths are ignored by one of your .gitignore files')."""
    links = ws.linked or linked_paths(ws)
    if links:
        ignored = set((await git(ws.path, "check-ignore", *links, check=False)).splitlines())
        links = [x for x in links if x not in ignored]
    await git(ws.path, "add", "-A", *(["--", "."] + [f":(exclude){x}" for x in links] if links else []))


async def link_deps(ws: Workspace, names: list[str]) -> list[str]:
    """Symlink gitignored dependency dirs (node_modules, .venv, ...) from the checkout, also next to nested package.json files."""
    cands = list(names)
    if "node_modules" in names:
        files = (await git(ws.repo, "ls-files", "*package.json", check=False)).splitlines()
        cands += [str(Path(f).parent / "node_modules") for f in files if f.count("/") and f.count("/") <= 3]
    made = []
    for rel in dict.fromkeys(cands):
        src, dst = ws.repo / rel, ws.path / rel
        if src.exists() and not dst.exists() and not dst.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(src, dst)
            made.append(rel)
    ws.linked = sorted(set(ws.linked) | set(made) | set(linked_paths(ws)))
    return made


async def run_setup(ws: Workspace, command: str, timeout: float = 1800) -> tuple[bool, str]:
    """workspace.setup: one shell command in the worktree (e.g. yarn install --frozen-lockfile). Returns (ok, output tail)."""
    p = await asyncio.create_subprocess_shell(command, cwd=str(ws.path), stdin=asyncio.subprocess.DEVNULL,
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return False, f"timed out after {int(timeout)}s"
    return p.returncode == 0, out.decode(errors="replace")[-1500:]


async def create(repo: Path, session_dir: Path, sid: str) -> Workspace:
    repo = repo.resolve()
    if await git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise GitError(f"{repo} is not a git repository")
    base = await git(repo, "rev-parse", "HEAD")
    branch = f"agent-desk/{sid}"
    path = session_dir / "worktree"
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "add", "-q", str(path), "-b", branch, base)
    return Workspace(repo, path, branch, base)


async def changed_files(ws: Workspace) -> list[str]:
    out = await git(ws.path, "status", "--porcelain", raw=True)
    links = set(ws.linked or linked_paths(ws))
    return [f for f in (l[3:].split(" -> ")[-1] for l in out.splitlines() if l) if f.rstrip("/") not in links]


async def diff(ws: Workspace) -> str:
    await _add_all(ws)
    return await git(ws.path, "diff", "--cached", "--no-color", check=False)


async def diff_since_base(ws: Workspace) -> str:
    """Everything the session changed: its commits plus anything not yet committed."""
    await _add_all(ws)
    return await git(ws.path, "diff", "--cached", "--no-color", ws.base_commit, check=False)


async def commit_all(ws: Workspace, message: str) -> str | None:
    """Commit only if something changed; returns the commit sha. The repo's hooks run (husky's .husky/_ is linked by link_deps)."""
    if not await changed_files(ws):
        return None
    await _add_all(ws)
    if not (await git(ws.path, "diff", "--cached", "--name-only", check=False)):
        return None
    await git(ws.path, "commit", "-q", "-m", message)
    return await git(ws.path, "rev-parse", "--short", "HEAD")


async def changed_since_base(ws: Workspace) -> list[str]:
    """Files the session changed so far (committed or not, new included), minus dependency links and deletions."""
    out = (await git(ws.path, "diff", "--name-only", ws.base_commit, check=False)).splitlines()
    out += (await git(ws.path, "ls-files", "--others", "--exclude-standard", check=False)).splitlines()
    links = set(ws.linked or linked_paths(ws))
    return [f for f in dict.fromkeys(out) if f and (ws.path / f).exists() and not any(f == l or f.startswith(l + "/") for l in links)]


async def add_detached(repo: Path, path: Path, commit: str) -> Path:
    """A throwaway checkout of `commit` (baseline runs, check doctor). Reused if it already exists."""
    if (path / ".git").exists():
        await git(path, "checkout", "-q", "--force", "--detach", commit, check=False)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "add", "-q", "--detach", "--force", str(path), commit)
    return path


async def remove_worktree(repo: Path, path: Path) -> None:
    await git(repo, "worktree", "remove", "--force", str(path), check=False)
    await git(repo, "worktree", "prune", check=False)


async def push_preview(cwd: Path | str, command: str) -> dict | None:
    """What a `git push` would send: local branch -> remote/target, the remote URL and the commits. None if not a push."""
    try:
        toks = shlex.split(command.strip())
        if toks and re.fullmatch(r"(/bin/)?(ba|z)?sh", toks[0]) and len(toks) > 2 and toks[1] in ("-c", "-lc"):
            toks = shlex.split(toks[2])                        # /bin/zsh -lc "git push ..."
    except ValueError:
        return None
    if "push" not in toks or "git" not in toks[:toks.index("push")]:
        return None
    rest = toks[toks.index("push") + 1:]
    rest = rest[:next((i for i, t in enumerate(rest) if t in ("&&", "||", ";", "|")), len(rest))]     # git push ... && echo done
    args = [t for t in rest if not t.startswith("-")]
    if any(t in ("--all", "--mirror", "--tags") for t in rest):
        return {"unknown": "pushes more than one branch (" + " ".join(t for t in rest if t.startswith("--")) + ")"}
    cur = await git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    upstream = await git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    up_remote, _, up_branch = upstream.partition("/") if upstream and "fatal" not in upstream else ("", "", "")
    remote = args[0] if args else (up_remote or "origin")
    if len(args) > 1:
        src, _, dst = args[1].lstrip("+").partition(":")
        src = cur if src in ("HEAD", "") else src
        dst = dst or src
    elif len(args) == 1 or not up_branch:
        src = dst = cur
    else:
        src, dst = cur, up_branch
    dst = dst.removeprefix("refs/heads/")
    url = await git(cwd, "remote", "get-url", remote, check=False)
    known = await git(cwd, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{dst}", check=False)
    if known:
        rng = f"{remote}/{dst}..{src}"
    else:                                                    # a new branch: what is not on any branch of that remote yet
        rng = src
    log = await git(cwd, "log", "--oneline", "-15", rng, *([] if known else ["--not", f"--remotes={remote}"]), check=False)
    return {"local": src, "remote": remote, "target": dst, "url": url, "new_branch": not known,
            "commits": [c for c in log.splitlines() if c]}
