"""Layered config loading with per-value provenance and secret rejection."""
from __future__ import annotations

import hashlib
import re
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .schema import SECRET_RE, Config

GLOBAL_PATH = Path.home() / ".agent-desk" / "config.yaml"


class ConfigError(Exception):
    pass


@dataclass
class Loaded:
    config: Config
    sources: dict[str, str] = field(default_factory=dict)  # dotted path -> layer name
    repo: Path | None = None                               # what produced it, so the settings writer can reload
    overrides: list[str] = field(default_factory=list)
    global_path: Path = GLOBAL_PATH
    session: dict = field(default_factory=dict)            # nested in-memory changes (layer "session"), re-applied on reload

    def snapshot(self) -> dict[str, Any]:
        return self.config.model_dump(mode="json")

    def snapshot_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True).encode()).hexdigest()[:12]


def _read(path: Path, text: str | None = None) -> dict:
    text = path.read_text() if text is None else text
    for i, line in enumerate(text.splitlines(), 1):
        if SECRET_RE.search(line):
            raise ConfigError(f"{path}:{i}: looks like a secret value; use env_key with the variable NAME")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


SELECTORS = ("model", "tier", "tiers")


def _merge(dst: dict, src: dict, layer: str, sources: dict, prefix: str = "") -> None:
    # a role override that picks a new selector replaces the old one (and default_tier)
    if re.fullmatch(r"roles\.\w+\.(fallback\.)?", prefix) and any(s in src for s in SELECTORS):
        for s in SELECTORS + ("default_tier",):
            if s not in src:
                dst.pop(s, None)
    # ...and one that moves it to another provider drops the account picked for the old one
    if re.fullmatch(r"roles\.\w+\.(fallback\.)?", prefix) and "provider" in src and "account" not in src and src["provider"] != dst.get("provider"):
        dst.pop("account", None)
    for k, v in src.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and isinstance(dst.get(k), dict) and v:
            _merge(dst[k], v, layer, sources, key + ".")
        else:
            dst[k] = v
            sources[key] = layer
            if isinstance(v, dict):
                for sub in v:
                    sources[f"{key}.{sub}"] = layer


def _set_dotted(d: dict, dotted: str, raw: str) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = yaml.safe_load(raw)


def _locate(path: Path, err: Exception) -> str:
    return f"{path}: {err}"


def load(repo: Path | None = None, overrides: list[str] | None = None,
         global_path: Path = GLOBAL_PATH, texts: dict[str, str] | None = None) -> Loaded:
    """texts: layer name ('global' | 'repo') -> content to use instead of the file (validating a change before writing it)."""
    texts = texts or {}
    merged: dict = {}
    sources: dict[str, str] = {}
    defaults = yaml.safe_load((resources.files("agent_desk.config") / "defaults.yaml").read_text())
    _merge(merged, defaults, "default", sources)
    layers: list[tuple[str, Path]] = []
    if global_path.exists() or "global" in texts:
        layers.append(("global", global_path))
    if repo and ((repo / ".agent-desk.yaml").exists() or "repo" in texts):
        layers.append(("repo", repo / ".agent-desk.yaml"))
    for name, path in layers:
        _merge(merged, _read(path, texts.get(name)), name, sources)
    if overrides:
        ov: dict = {}
        for item in overrides:
            if "=" not in item:
                raise ConfigError(f"--set expects key=value, got {item!r}")
            k, v = item.split("=", 1)
            _set_dotted(ov, k, v)
        _merge(merged, ov, "--set", sources)
    try:
        cfg = Config.model_validate(merged)
    except Exception as e:  # pydantic ValidationError -> point at the highest layer that touched the key
        raise ConfigError(_explain(e, layers, texts)) from e
    return Loaded(cfg, sources, repo, list(overrides or []), global_path)


def _explain(e: Exception, layers: list[tuple[str, Path]], texts: dict[str, str] | None = None) -> str:
    errs = getattr(e, "errors", lambda: [])()
    lines = []
    for er in errs:
        loc = ".".join(str(x) for x in er["loc"])
        where = _find_line(loc, [(n, p) for n, p in layers if n not in (texts or {})])
        lines.append(f"{where or 'config'}: {loc}: {er['msg']}")
    return "\n".join(lines) or str(e)


def _find_line(loc: str, layers: list[tuple[str, Path]]) -> str | None:
    key = loc.split(".")[-1]
    for _, path in reversed(layers):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith(f"{key}:") or f" {key}:" in line:
                return f"{path}:{i}"
    return None
