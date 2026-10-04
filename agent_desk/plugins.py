"""Plugin registry: every component is a plugin selected by name from config."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

KINDS = ("runtime", "decision", "check", "input", "sink", "ui")


class PluginError(Exception):
    pass


@dataclass
class Plugin:
    kind: str
    name: str
    factory: Callable[..., Any]
    capabilities: frozenset[str] = frozenset()
    config_model: type | None = None             # strict pydantic model for plugin-specific config
    source: str = "builtin"                      # builtin | external:<module>
    describe: Callable[[], dict] = field(default=lambda: {})


class Registry:
    def __init__(self):
        self._p: dict[tuple[str, str], Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        if plugin.kind not in KINDS:
            raise PluginError(f"unknown plugin kind {plugin.kind!r}")
        key = (plugin.kind, plugin.name)
        if key in self._p:
            raise PluginError(f"plugin {plugin.kind}:{plugin.name} already registered")
        self._p[key] = plugin

    def get(self, kind: str, name: str) -> Plugin:
        try:
            return self._p[(kind, name)]
        except KeyError:
            raise PluginError(f"no {kind} plugin named {name!r}; available: {', '.join(self.names(kind)) or 'none'}") from None

    def has(self, kind: str, name: str) -> bool:
        return (kind, name) in self._p

    def names(self, kind: str) -> list[str]:
        return sorted(n for k, n in self._p if k == kind)

    def all(self) -> list[Plugin]:
        return [self._p[k] for k in sorted(self._p)]

    def load_external(self, modules: list[str]) -> None:
        """An external plugin module exposes register(registry)."""
        for m in modules:
            try:
                mod = importlib.import_module(m)
                before = set(self._p)
                mod.register(self)
            except Exception as e:
                raise PluginError(f"cannot load plugin module {m!r}: {e}") from e
            for k in set(self._p) - before:
                self._p[k].source = f"external:{m}"


def default_registry(external: list[str] | None = None) -> Registry:
    from . import builtin                     # the only module that imports concrete plugins
    reg = Registry()
    builtin.register(reg)
    reg.load_external(external or [])
    return reg
