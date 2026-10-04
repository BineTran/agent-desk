import re
from typing import Any

PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.I),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)([^\s\"',]{6,})"),
]
SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")
SECRET_ENV = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_EXTRA: set[str] = set()                      # values entered at runtime (SETTINGS key input): redacted everywhere


def register_secret(value: str) -> None:
    if value and len(value) >= 8:
        _EXTRA.add(value)


def redact_text(s: str, env: dict[str, str] | None = None) -> str:
    for name in SECRET_ENV:
        v = (env or {}).get(name)
        if v and len(v) >= 8:
            s = s.replace(v, "[REDACTED]")
    for v in _EXTRA:
        s = s.replace(v, "[REDACTED]")
    s = PATTERNS[4].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", s)
    for p in PATTERNS[:4]:
        s = p.sub("[REDACTED]", s)
    return s


def redact(obj: Any, env: dict[str, str] | None = None) -> Any:
    if isinstance(obj, str):
        return redact_text(obj, env)
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if isinstance(v, str) and SENSITIVE_KEY.search(str(k)) else redact(v, env)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, env) for v in obj]
    return obj
