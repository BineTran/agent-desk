"""Deterministic command policy. Deny > approve > allow; Jev is advisory and never overrides this."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

DENY = [r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)", r"\bgit\s+push\b.*--force", r":\(\)\s*\{", r"\bmkfs\b", r"\bdd\s+.*of=/dev/",
        r"\bchmod\s+-R\s+777\s+/", r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh"]
APPROVE = [r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\b", r"\brm\s+-[a-z]*r", r"\bmigrate\b", r"\bprisma\s+db\s+push\b",
           r"\bkubectl\s+(apply|delete)\b", r"\bterraform\s+(apply|destroy)\b", r"\b(deploy|publish)\b", r"\bnpm\s+publish\b", r"\bsudo\b"]
# Reading env vars or secret files: not dangerous to run, but the output is a secret. Verdict "secret" -> approval.env_gate decides.
SECRET_CMD = [r"(^|[;&|]\s*)(printenv|env|export\s+-p|set)\s*($|[;&|])", r"(^|[;&|]\s*)printenv\s+\w", r"\becho\b[^;&|]*\$\{?[A-Za-z_]\w*",
              r"\b(security\s+find-(generic|internet)-password|gh\s+auth\s+token|aws\s+configure\s+get)\b"]
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[\w.-]+)?|\.envrc|\.npmrc|\.netrc|\.pgpass|credentials(\.json)?|secrets?\.[\w]+|id_(rsa|ed25519|ecdsa|dsa)\w*"
                         r"|[^/]*\.(pem|key|p12|pfx|keystore))$|(^|/)\.(aws|ssh|gnupg)(/|$)|(^|/)\.docker/config\.json$")
SAMPLE = re.compile(r"(^|/)\.env\.(example|sample|template|dist)$")


def secret_path(path: str) -> bool:
    p = path.strip().strip("'\"")
    return bool(p) and not SAMPLE.search(p) and bool(SECRET_FILE.search(p.rstrip("/")))


def classify_path(path: str) -> str:
    """A file read by a tool (Read/Grep/Glob): 'secret' for env/secret files, else 'allow' (the sandbox bounds reads)."""
    return "secret" if secret_path(path) else "allow"


ALLOW = [r"^(ls|pwd|cat|head|tail|wc|rg|grep|find|git\s+(status|diff|log|show|branch)|sed\s+-n)\b",
         r"^(pnpm|npm|yarn|bun)\s+(run\s+)?(test|lint|typecheck|build|tsc)\b", r"^(pytest|python\s+-m\s+pytest|uv\s+run\s+pytest|ruff|mypy|tsc|go\s+test|cargo\s+(test|check|build))\b"]


def _outside(command: str, cwd: str | None, root: Path) -> bool:
    if cwd and root.resolve() not in (Path(cwd).resolve(), *Path(cwd).resolve().parents):
        return True
    try:
        for tok in shlex.split(command):
            if tok.startswith(("/", "~")) and not tok.startswith(str(root)):
                if tok.startswith(("/tmp", "/dev/null", "/usr", "/bin", "/opt/homebrew")):
                    continue
                return True
            if ".." in Path(tok).parts:
                return True
    except ValueError:
        return True
    return False


def classify(command: str, cwd: str | None, root: Path, extra_approve: list[str] | None = None) -> str:
    c = command.strip()
    inner = re.sub(r"^(/bin/)?(ba|z)?sh\s+-l?c\s+['\"]?", "", c).rstrip("'\"")
    for text in (c, inner):
        if any(re.search(p, text) for p in DENY):
            return "deny"
    try:
        toks = shlex.split(inner)
    except ValueError:
        toks = inner.split()
    if any(re.search(p, inner) for p in SECRET_CMD) or any(secret_path(t) for t in toks if not t.startswith("-")):
        return "secret"
    if _outside(inner, cwd, root) or any(re.search(p, inner) for p in APPROVE + (extra_approve or [])):
        return "approve"
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", inner)
    if parts and all(any(re.search(p, x) for p in ALLOW) for x in parts if x):
        return "allow"
    return "approve"
