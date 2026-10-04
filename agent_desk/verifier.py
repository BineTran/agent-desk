"""Deterministic verification: run configured checks in the worktree. 'Agent says done' is never evidence.

A failing check only blocks when the session made it worse: results are compared with a baseline (the same check on the
base commit, see baseline.py) by *failure keys* — test ids, file:line diagnostics — so pre-existing failures never become
the Worker's job. Checks can be scoped to the changed files (`paths`, `{changed}`) and may not rewrite code unless `writes`.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config.schema import Check

TIMEOUT = 600
OUTPUT_CAP = 4 * 1024 * 1024


@dataclass
class CheckResult:
    name: str
    command: str
    required: bool
    passed: bool
    exit_code: int
    duration_ms: int
    tail: list[str]
    fingerprint: str | None
    status: str = ""                     # pass | regression | pre-existing | skipped (set by classify; "" = unclassified fail)
    keys: list[str] = field(default_factory=list)          # failure keys found in the full output
    new_failures: list[str] = field(default_factory=list)  # keys not present on the base commit
    summary: str = ""                    # first meaningful error line, for humans
    wrote: list[str] = field(default_factory=list)         # files the check tried to modify (reverted unless writes: true)
    skip_reason: str = ""
    output: str = field(default="", repr=False)

    @property
    def blocking(self) -> bool:
        return self.required and not self.passed and self.status not in ("pre-existing", "skipped")


_NOISE = re.compile(r"(\d+(\.\d+)?\s*(ms|s|seconds)\b|0x[0-9a-f]+|\b\d{2}:\d{2}:\d{2}\b|/tmp/\S+|/var/\S+)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def fingerprint(name: str, tail: list[str]) -> str:
    """Stable id for 'the same failure': check name + normalised error lines (timings/paths stripped)."""
    errs = [_NOISE.sub("", l).strip() for l in tail if re.search(r"(?i)error|fail|assert|exception|✗|not ok", l)]
    return hashlib.sha1((name + "|" + "\n".join(errs[:12])).encode()).hexdigest()[:8]


# ---- failure keys: one per failing test / diagnostic, stable across runs --------------------------------------------
_KEY_PATTERNS = [
    re.compile(r"^\s*FAIL\s+(\S+\.(?:[cm]?[jt]sx?|py))"),                                   # jest/vitest: FAIL src/a.spec.ts
    re.compile(r"^\s*●\s+(.+?)\s*$"),                                                        # jest: ● Suite › test name
    re.compile(r"^FAILED\s+(\S+::\S+)"),                                                     # pytest: FAILED tests/x.py::test_y
    re.compile(r"^ERROR\s+(\S+\.py(?:::\S+)?)"),                                             # pytest collection error
    re.compile(r"^\s*--- FAIL: (\S+)"),                                                      # go test
    re.compile(r"^test (\S+) \.\.\. FAILED"),                                                # cargo test
    re.compile(r"^(\S+\.\w+)\((\d+),\d+\): error (TS\d+)"),                                  # tsc: a.ts(3,5): error TS2322
    re.compile(r"^(\S+\.\w+):(\d+):\d+:?\s+(?:error|warning)?\s*[:\s]*\[?([\w/@-]+(?:/[\w-]+)*)\]?"),  # eslint unix/ruff/flake8
    re.compile(r"^\s*(\S+\.\w+):(\d+):\d+\s+(lint/\S+|\S+/\S+)"),                            # biome: src/a.ts:12:5 lint/style/noX
]
_GENERIC_ERR = re.compile(r"(?i)\b(error|failed|failure|not found|no such|cannot|exception|×|✗)\b")


def _key(m: re.Match) -> str:
    g = [x for x in m.groups() if x]
    return ":".join(g) if len(g) > 1 else g[0]


def failure_keys(output: str) -> list[str]:
    keys: list[str] = []
    for line in _ANSI.sub("", output).splitlines():
        for p in _KEY_PATTERNS:
            m = p.search(line)
            if m:
                keys.append(_key(m).strip())
                break
    return list(dict.fromkeys(k for k in keys if k))


_SUMMARY = re.compile(r"^(Test Suites:.*failed.*|Tests:.*failed.*|=+ .*\bfailed\b.*=+|FAIL\s*$|\d+ (?:errors?|problems?) found.*|Found \d+ errors?.*)", re.I)


def first_error(output: str) -> str:
    """The line a human wants: the runner's own summary if there is one, else the first error-looking line."""
    clean = _ANSI.sub("", output).splitlines()
    summ = [l.strip() for l in clean if _SUMMARY.match(l.strip())]
    if summ:
        return " · ".join(dict.fromkeys(summ))[:200]
    for line in clean:
        s = line.strip()
        if s and _GENERIC_ERR.search(s):
            return s[:200]
    lines = [l.strip() for l in _ANSI.sub("", output).splitlines() if l.strip()]
    return (lines[-1] if lines else "")[:200]


# ---- scoping: paths globs and the {changed} placeholder -------------------------------------------------------------
def glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif c == "*":
            out, i = out + "[^/]*", i + 1
        elif c == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + r"\Z")


def matching(files: list[str], paths: list[str] | None) -> list[str]:
    if not paths:
        return list(files)
    pats = [glob_re(p) for p in paths]
    return [f for f in files if any(p.match(f) for p in pats)]


def expand(check: Check, changed: list[str] | None) -> tuple[str | None, str]:
    """(command to run, skip reason). changed=None means 'unknown': run unscoped (baseline/doctor without a file list)."""
    if changed is None:
        return (None, "needs the changed files") if "{changed}" in check.command else (check.command, "")
    files = matching(changed, check.paths)
    if check.paths and not files:
        return None, "no matching changes (" + ", ".join(check.paths) + ")"
    if "{changed}" in check.command:
        if not files:
            return None, "no changed files"
        return check.command.replace("{changed}", " ".join(shlex.quote(f) for f in files)), ""
    return check.command, ""


# ---- running ---------------------------------------------------------------------------------------------------------
async def _git(cwd: str, *args: str) -> str:
    p = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return out.decode(errors="replace")


async def _dirty(cwd: str) -> dict[str, bytes | None]:
    """Snapshot of files git sees as changed/untracked: path -> content (None for deleted/dirs)."""
    out = await _git(cwd, "status", "--porcelain", "--untracked-files=all", "-z")
    snap: dict[str, bytes | None] = {}
    for entry in filter(None, out.split("\0")):
        path = entry[3:]
        f = Path(cwd) / path
        snap[path] = f.read_bytes() if f.is_file() and not f.is_symlink() else None
    return snap


async def _guard_restore(cwd: str, before: dict[str, bytes | None]) -> list[str]:
    """Undo what a check wrote: tracked files back to their pre-check content, new untracked files removed."""
    after = await _dirty(cwd)
    touched = [p for p, v in after.items() if p not in before or before[p] != v]
    for p in touched:
        f = Path(cwd) / p
        if p in before and before[p] is not None:
            f.write_bytes(before[p])
        elif p in before:
            continue
        else:
            tracked = (await _git(cwd, "ls-files", "--", p)).strip()
            if tracked:
                await _git(cwd, "checkout", "--", p)
            elif f.is_file():
                f.unlink()
    return touched


async def run_check(name: str, check: Check, cwd: str, timeout: int = TIMEOUT, changed: list[str] | None = None,
                    guard: bool | None = None) -> CheckResult:
    cmd, why = expand(check, changed) if changed is not None or "{changed}" in check.command else (check.command, "")
    if cmd is None:
        return CheckResult(name, check.command, check.required, True, 0, 0, [], None, status="skipped", skip_reason=why)
    guard = (not check.writes) if guard is None else guard
    before = await _dirty(cwd) if guard else {}
    t0 = time.monotonic()
    p = await asyncio.create_subprocess_shell(cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                              stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        code = p.returncode
    except asyncio.TimeoutError:
        p.kill()
        out, code = b"timeout", 124
    text = out.decode(errors="replace")[-OUTPUT_CAP:]
    wrote = await _guard_restore(cwd, before) if guard else []
    tail = text.splitlines()[-60:]
    ok = code == 0
    keys = [] if ok else (failure_keys(text) or [f"{name}#{fingerprint(name, tail)}"])
    return CheckResult(name, cmd, check.required, ok, code, int((time.monotonic() - t0) * 1000), tail,
                       None if ok else fingerprint(name, keys), status="pass" if ok else "", keys=keys,
                       summary="" if ok else first_error(text), wrote=wrote, output=text)


async def run_all(checks: dict[str, Check], cwd: str, changed: list[str] | None = None) -> list[CheckResult]:
    return [await run_check(n, c, cwd, changed=changed) for n, c in checks.items()]


def classify(r: CheckResult, base: dict | None) -> CheckResult:
    """Compare with the same check on the base commit. base = {passed, keys} or None (unknown)."""
    if r.status in ("pass", "skipped"):
        return r
    from .decision.rules import environment_error
    if environment_error(r.tail):                    # broken machine (tool missing): nothing was verified, on base or here
        r.status, r.new_failures = "environment", list(r.keys)
        return r
    if base is None or base.get("passed"):
        r.status, r.new_failures = "regression", list(r.keys)
    else:
        new = [k for k in r.keys if k not in set(base.get("keys") or [])]
        r.status, r.new_failures = ("regression", new) if new else ("pre-existing", [])
    if r.status == "regression":
        r.fingerprint = fingerprint(r.name, sorted(r.new_failures))
        r.tail = new_failure_lines(r) or r.tail
    return r


def new_failure_lines(r: CheckResult, limit: int = 60) -> list[str]:
    """Only the output that belongs to the new failures: what the Worker should look at."""
    if not r.new_failures or not r.output:
        return []
    lines = _ANSI.sub("", r.output).splitlines()
    marks = [k.split(":")[0] if ":" in k and not k.startswith(r.name + "#") else k for k in r.new_failures]
    out: list[str] = []
    for i, line in enumerate(lines):
        if any(m and m in line for m in marks):
            out += lines[i:i + 6]
        if len(out) >= limit:
            break
    return list(dict.fromkeys(out))[:limit]


def required_failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.blocking]
