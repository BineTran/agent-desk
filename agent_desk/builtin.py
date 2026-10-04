"""The ONLY module that imports concrete plugins. Core code asks the registry by name."""
from __future__ import annotations

from .plugins import Plugin, Registry


def register(reg: Registry) -> None:
    from .runtime.agy_cli import AgyCliRuntime
    from .runtime.claude_cli import ClaudeCliRuntime
    from .runtime.codex_app_server import CodexAppServerRuntime
    from .runtime.mock import MockRuntime

    reg.register(Plugin("runtime", "codex", CodexAppServerRuntime, frozenset({"sandbox_write", "structured_output", "resume", "approvals_rpc", "web_search", "profiles", "mcp"})))
    reg.register(Plugin("runtime", "claude-cli", ClaudeCliRuntime, frozenset({"sandbox_write", "structured_output", "resume", "approvals_hook", "web_search", "profiles", "mcp"})))  # Spike C: OS sandbox verified
    reg.register(Plugin("runtime", "agy-cli", AgyCliRuntime, frozenset({"structured_output", "resume"})))  # headless = read-only (permissions auto-denied)
    reg.register(Plugin("runtime", "mock", MockRuntime, frozenset({"sandbox_write", "structured_output", "resume"})))
    from .decision.engines import JevEngine, LlmEngine, NoneEngine, ReplayEngine

    # factory(ctx, provider_name, model, provider_cfg) -> engine
    reg.register(Plugin("decision", "none", lambda ctx, p, m, pc: NoneEngine(), frozenset()))
    reg.register(Plugin("decision", "llm", lambda ctx, p, m, pc: LlmEngine(ctx, p, m), frozenset({"provider:llm"})))
    reg.register(Plugin("decision", "jev", lambda ctx, p, m, pc: JevEngine(ctx, pc.env_key if pc else None), frozenset({"provider:decision", "calibrated"})))
    reg.register(Plugin("decision", "replay", lambda ctx, p, m, pc: ReplayEngine(ctx), frozenset()))
