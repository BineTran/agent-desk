"""Change config from the UI/CLI: preview -> validate -> write one layer file (comments kept) -> apply live.

Scopes: 'session' (in memory, layer "session"), 'repo' (<repo>/.agent-desk.yaml), 'global' (~/.agent-desk/config.yaml).
A change is a dict of dotted keys -> value (or DELETE). Nothing is written unless the whole merged config validates.
"""
from __future__ import annotations

import copy
import difflib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .loader import ConfigError, Loaded, _merge, load
from .schema import SECRET_RE, Config

SCOPES = ("session", "repo", "global")


class _Delete:
    def __repr__(self) -> str:
        return "DELETE"


DELETE = _Delete()


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def nest(changes: dict[str, Any]) -> dict:
    """{'a.b': 1} -> {'a': {'b': 1}} (DELETE kept as a marker)."""
    out: dict = {}
    for dotted, v in changes.items():
        d = out
        keys = dotted.split(".")
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = v
    return out


def _apply(doc: dict, changes: dict[str, Any], mapping=dict) -> None:
    for dotted, v in changes.items():
        keys = dotted.split(".")
        d, trail = doc, []
        for k in keys[:-1]:
            if not isinstance(d.get(k), dict):
                if v is DELETE:
                    break
                d[k] = mapping()
            trail.append((d, k))
            d = d[k]
        else:
            if v is DELETE:
                d.pop(keys[-1], None)
                for parent, k in reversed(trail):          # prune maps left empty by the delete
                    if not parent[k]:
                        del parent[k]
            else:
                d[keys[-1]] = v
                _drop_conflicting_selectors(d, keys)


def _drop_conflicting_selectors(d: dict, keys: list[str]) -> None:
    """Setting roles.<r>.model must remove tier/tiers/default_tier in the same layer (exactly one selector)."""
    sel = ("model", "tier", "tiers")
    if len(keys) >= 3 and keys[0] == "roles" and keys[-1] in sel:
        for s in sel + ("default_tier",):
            if s != keys[-1] and not (keys[-1] == "tiers" and s == "default_tier"):
                d.pop(s, None)


def layer_path(loaded: Loaded, scope: str) -> Path | None:
    if scope == "global":
        return loaded.global_path
    if scope == "repo":
        if loaded.repo is None:
            raise ConfigError("no repo: cannot save to .agent-desk.yaml")
        return loaded.repo / ".agent-desk.yaml"
    return None


@dataclass
class Preview:
    scope: str
    changes: dict[str, Any]
    path: Path | None
    before: str
    after: str
    new: Loaded
    keys: list[str] = field(default_factory=list)

    def diff(self) -> str:
        if self.path is None:
            return "\n".join(f"session: {k} = {'(removed)' if v is DELETE else v}" for k, v in self.changes.items())
        return "".join(difflib.unified_diff(self.before.splitlines(True), self.after.splitlines(True),
                                            str(self.path) if self.before else "/dev/null", str(self.path)))


def _with_session(new: Loaded, session: dict) -> Loaded:
    if not session:
        return new
    snap = new.snapshot()
    srcs = dict(new.sources)
    _merge_session(snap, session, srcs)
    cfg = Config.model_validate(snap)
    out = Loaded(cfg, srcs, new.repo, new.overrides, new.global_path, copy.deepcopy(session))
    return out


def _merge_session(snap: dict, session: dict, sources: dict) -> None:
    dels = {k: v for k, v in _flat(session).items() if v is DELETE}
    _apply(snap, dels)
    keep = _strip(session)
    if keep:
        _merge(snap, keep, "session", sources)
    for k in dels:
        sources[k] = "session"


def _flat(d: dict, prefix: str = "") -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _strip(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if v is DELETE:
            continue
        if isinstance(v, dict) and v:
            s = _strip(v)
            if s:
                out[k] = s
        else:
            out[k] = v
    return out


def _drop_keys(session: dict, keys: list[str]) -> dict:
    """Session entries overridden by a file save are dropped (the saved value wins)."""
    flat = _flat(session)
    keep = {k: v for k, v in flat.items() if not any(k == x or k.startswith(x + ".") or x.startswith(k + ".") for x in keys)}
    return nest(keep)


def plan_change(loaded: Loaded, scope: str, changes: dict[str, Any], texts: dict[str, str] | None = None) -> Preview:
    """Validate the change against the whole layered config. Raises ConfigError with a readable reason.
    texts: other layers' not-yet-written content (a review saving to several files validates them together)."""
    texts = dict(texts or {})
    if scope not in SCOPES:
        raise ConfigError(f"unknown scope {scope!r}; scopes: {', '.join(SCOPES)}")
    for k, v in changes.items():
        if isinstance(v, str) and SECRET_RE.search(v):
            raise ConfigError(f"{k}: looks like a secret value; store the key in an environment variable and reference its NAME")
    keys = list(changes)
    if scope == "session":
        session = copy.deepcopy(loaded.session)
        for k, v in _flat(nest(changes)).items():
            _apply(session, {k: v}) if v is not DELETE else _set_marker(session, k)
        base = load(loaded.repo, loaded.overrides, loaded.global_path, texts=texts)
        try:
            new = _with_session(base, session)
        except Exception as e:
            raise ConfigError(_short(e)) from e
        return Preview(scope, changes, None, "", "", new, keys)
    path = layer_path(loaded, scope)
    before = texts.get(scope, path.read_text() if path.exists() else "")
    y = _yaml()
    doc = y.load(before) if before.strip() else None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    _apply(doc, changes, CommentedMap)
    buf = io.StringIO()
    if doc:
        y.dump(doc, buf)
    after = buf.getvalue()
    try:
        new = load(loaded.repo, loaded.overrides, loaded.global_path, texts={**texts, scope: after})
        new = _with_session(new, _drop_keys(loaded.session, keys))
    except Exception as e:
        raise ConfigError(_short(e)) from e
    return Preview(scope, changes, path, before, after, new, keys)


def _set_marker(session: dict, dotted: str) -> None:
    d = session
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = DELETE


def _short(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        return "\n".join(f"{'.'.join(map(str, x['loc'])) or 'config'}: {x['msg']}" for x in errs())
    return str(e)


def save(p: Preview) -> None:
    """Write the layer file atomically (keeps <file>.bak). Session scope writes nothing."""
    if p.path is None:
        return
    p.path.parent.mkdir(parents=True, exist_ok=True)
    if p.path.exists():
        p.path.with_name(p.path.name + ".bak").write_text(p.before)
    tmp = p.path.with_name(p.path.name + ".tmp")
    tmp.write_text(p.after)
    os.replace(tmp, p.path)


def apply_live(loaded: Loaded, new: Loaded, session=None, keys: list[str] | None = None) -> None:
    """Swap values into the SAME Config object: router, decision pipeline and engine ctx hold it by reference."""
    for name in Config.model_fields:
        setattr(loaded.config, name, getattr(new.config, name))
    loaded.sources.clear()
    loaded.sources.update(new.sources)
    loaded.session = new.session
    if session is not None:
        for k in keys or []:                        # a saved role replaces the /model override for it
            parts = k.split(".")
            if parts[0] == "roles" and len(parts) > 1:
                session.overrides.pop(parts[1], None)


def commit(loaded: Loaded, p: Preview, session=None) -> None:
    save(p)
    apply_live(loaded, p.new, session, p.keys)


ORDER = ("global", "repo", "session")


def plan_many(loaded: Loaded, by_scope: dict[str, dict[str, Any]]) -> list[Preview]:
    """One preview per scope, validated together: global first, then repo on top of it, then session on top of both."""
    out: list[Preview] = []
    texts: dict[str, str] = {}
    for scope in ORDER:
        if by_scope.get(scope):
            p = plan_change(loaded, scope, by_scope[scope], texts)
            if p.path is not None:
                texts[scope] = p.after
            out.append(p)
    return out


def commit_many(loaded: Loaded, previews: list[Preview], session=None) -> None:
    """Write every file, then apply the last (most complete) result live once."""
    for p in previews:
        save(p)
    if previews:
        apply_live(loaded, previews[-1].new, session, [k for p in previews for k in p.keys])
