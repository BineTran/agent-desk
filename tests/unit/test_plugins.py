import ast
import sys
import types
from pathlib import Path

import pytest

from agent_desk.plugins import Plugin, PluginError, Registry, default_registry

ROOT = Path(__file__).resolve().parents[2] / "agent_desk"
CONCRETE = {"agent_desk.runtime.codex_app_server", "agent_desk.runtime.agy_cli", "agent_desk.runtime.claude_cli", "agent_desk.runtime.mock"}
ALLOWED = {"builtin.py"}                                    # the only core module allowed to import concrete plugins


def test_builtin_plugins_registered():
    r = default_registry()
    assert r.names("runtime") == ["agy-cli", "claude-cli", "codex", "mock"]
    assert r.names("decision") == ["jev", "llm", "none", "replay"]
    assert "sandbox_write" not in r.get("runtime", "agy-cli").capabilities
    assert "sandbox_write" in r.get("runtime", "codex").capabilities and "sandbox_write" in r.get("runtime", "claude-cli").capabilities


def test_duplicate_and_unknown():
    r = Registry()
    r.register(Plugin("runtime", "x", lambda: 1))
    with pytest.raises(PluginError):
        r.register(Plugin("runtime", "x", lambda: 2))
    with pytest.raises(PluginError, match="available"):
        r.get("runtime", "nope")
    with pytest.raises(PluginError):
        r.register(Plugin("weird", "x", lambda: 1))


def test_external_plugin_loaded_by_name(monkeypatch):
    mod = types.ModuleType("my_plugin")
    mod.register = lambda reg: reg.register(Plugin("decision", "mine", lambda: None, frozenset({"provider:llm"})))
    monkeypatch.setitem(sys.modules, "my_plugin", mod)
    r = default_registry(["my_plugin"])
    assert r.get("decision", "mine").source == "external:my_plugin" and r.get("decision", "none").source == "builtin"
    with pytest.raises(PluginError, match="cannot load"):
        default_registry(["no_such_module_xyz"])


def test_core_never_imports_concrete_plugins():
    """Architecture rule: only builtin.py (and the plugin packages themselves) may import concrete runtimes."""
    offenders = []
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT)
        if rel.name in ALLOWED or rel.parts[0] == "runtime":
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                base = "agent_desk." + ".".join(rel.parts[:-1] + (node.module or "",)).strip(".") if node.level else (node.module or "")
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n in CONCRETE or n.endswith(("codex_app_server", "claude_cli")) for n in names):
                offenders.append(f"{rel}: {names}")
    assert not offenders, offenders
