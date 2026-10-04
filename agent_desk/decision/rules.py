"""Deterministic decisions. Rules run before any model and can only be tightened by config."""
from __future__ import annotations

import re
from pathlib import Path

from ..contracts import TaskSpec

ENV_PATTERNS = [r"No module named ['\"]?([\w.]+)", r"ModuleNotFoundError: No module named ['\"]?([\w.]+)", r"command not found", r"not found: \w+",
                r"ENOENT", r"Cannot find module ['\"]?([\w@/.-]+)", r"\bEACCES\b", r"Permission denied", r"ECONNREFUSED", r"Could not resolve host",
                r"[Nn]etwork is unreachable", r"No such file or directory: ['\"]?(python|node|pnpm|npm|pytest|uv|go|cargo)\b",
                r"executable file not found", r"is not recognized as an internal or external command",
                # natural-language reports from agents ("pytest is not installed", "no network access")
                r"(?i)\b(is|are|was|were) not installed\b", r"(?i)\bnot installed in (the|this) (environment|sandbox)", r"(?i)\bno network access\b",
                r"(?i)\b(cannot|can't|unable to) (install|download|reach)\b", r"(?i)\b(unavailable|missing) in (the|this) (environment|sandbox)\b",
                r"(?i)read-only file ?system", r"(?i)\bunwritable\b", r"(?i)(temporary|temp|tmp) dir(ectory)? .{0,40}(unavailable|unwritable|not writable|denied)",
                r"(?i)cannot (create|write|open) .{0,40}(temp|tmp|cache)", r"(?i)\boperation not permitted\b",
                r"\.husky/_/husky\.sh: No such file"]                     # husky never installed in the checkout


def environment_error(lines: list[str], repo: Path | None = None) -> str | None:
    """Failure caused by the machine (missing tool/dependency/permission/network), not by the code. Returns the matching line."""
    for line in lines:
        for pat in ENV_PATTERNS:
            m = re.search(pat, line)
            if not m:
                continue
            mod = m.group(1) if m.groups() else None
            if mod and repo and "No module named" in line:      # a project-local module that is missing is a code bug, not the env
                top = mod.split(".")[0]
                if (repo / f"{top}.py").exists() or (repo / top).is_dir():
                    continue
            return line.strip()[:200]
    return None


def route_options(task: TaskSpec, roles: dict | None = None) -> list[str]:
    if task.kind == "deployment" and (roles is None or ("deployer" in roles and roles["deployer"].enabled)):
        return ["deployer"]
    if task.requires_write:
        return ["worker"]
    return {"investigation": ["explorer"], "review": ["explorer"], "verification": ["explorer"], "research": ["researcher", "explorer"]}.get(task.kind, ["explorer"])


def route_rule(task: TaskSpec) -> str:
    return route_options(task)[0]


def tier_options(role_tiers: list[str] | None) -> list[str]:
    return list(role_tiers or [])


def tier_rule(task: TaskSpec, options: list[str]) -> str:
    """Writers get the strongest tier, readers the cheapest."""
    order = ["fast", "strong", "max"]
    ranked = sorted(options, key=lambda t: order.index(t) if t in order else 1)
    return ranked[-1] if task.requires_write else ranked[0]
