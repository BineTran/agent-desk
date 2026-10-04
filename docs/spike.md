# Spike results (2026-10-03, codex-cli 0.159.0)

Verified against the real `codex app-server`:

| Question | Result |
|---|---|
| Handshake | `initialize {clientInfo}` then notify `initialized`; works with `--disable multi_agent` |
| Models | `model/list` → `data[].{id, supportedReasoningEfforts[].reasoningEffort}`. Offered: gpt-6.1-sol, gpt-6-astra, gpt-6-sol, gpt-6-luna, gpt-5.6-{sol,terra,luna}, gpt-5.5. Efforts include `max`/`ultra` → `effort` is a free string validated by `config check` |
| Capabilities | `modelProvider/capabilities/read` → `{webSearch: true, ...}` |
| `thread/start` | accepts `model, cwd, sandbox:"read-only", approvalPolicy:"never", developerInstructions`; returns `thread.id` |
| `turn/start` + `outputSchema` | A Pydantic `model_json_schema()` (nested `$defs`, nullable) was accepted; final `agentMessage` text validated against `ArchitectReview` |
| Completion | `turn/completed` with `turn.status` (`completed` only = success) |
| Final message | `item/completed` with `item.type == "agentMessage"`, `item.text` |
| Usage | `thread/tokenUsage/updated` → `tokenUsage.{total,last}.{inputTokens,cachedInputTokens,outputTokens}` |
| Overhead | first turn ≈ 18k input tokens (14k cached on repeat) — matches the ~20k figure measured by the feature-spec-to-code skill |

Not yet verified: Jev (no `TYPESAFE_API_KEY` in this shell), approval round-trip on `workspace-write`, `thread/fork` ephemeral caching, multiple providers per app-server, `fuzzyFileSearch`.

# Spike C — Claude Code CLI 2.1.236, claude.ai subscription (2026-10-03)

All measured with `claude -p ... --model haiku` in scratch dirs; effects verified on disk, not from model text.

| Question | Result |
|---|---|
| Overhead | Default: ~9.6k tokens, loads repo `CLAUDE.md` (it obeyed a planted instruction), 15 MCP connectors, 52 slash commands, 21 skills. With `--system-prompt <card> --strict-mcp-config --disable-slash-commands --setting-sources "" --tools <list>`: **330 input tokens**, 5× cheaper, CLAUDE.md ignored |
| User hooks | Run in `-p` by default; `--setting-sources ""` removes them |
| Structured output | `--json-schema` → `result.structured_output` (via a `StructuredOutput` tool, 2 turns) |
| stream-json | needs `--verbose`. Events: `system/{init,hook_*,thinking_tokens(very noisy),task_summary,post_turn_summary}`, `assistant`, `user` (tool_result), `rate_limit_event`, `result`. `result` has `usage`, `total_cost_usd`, `session_id`, `is_error`, `terminal_reason`, `permission_denials[]` |
| Stdin | must pass `< /dev/null` (otherwise waits 3 s) |
| **Sandbox** | OS-level and effective via `--settings` JSON `sandbox:{enabled, failIfUnavailable, filesystem.allowWrite:["."], network.allowedDomains:[]}`: write inside cwd OK; write outside → `operation not permitted`, nothing on disk; network blocked (curl 000/56) |
| `dontAsk` | non-allowlisted tools auto-denied; shows up in `permission_denials` |
| PreToolUse hook | stdin keys: `cwd, hook_event_name, permission_mode, session_id, tool_input, tool_name, tool_use_id, transcript_path…`; stdout `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|deny","permissionDecisionReason":…}}` works under `dontAsk` |
| **Hook crash = FAIL-OPEN** | exit 1 → tool still ran (sandbox auto-allows Bash). Fixed by `autoAllowBashIfSandboxed:false`: crash → `dontAsk` denies. Our hook wrapper also catches every exception and prints `deny` |
| `--resume <session_id>` | continues the same session (remembered "MANGO") |
| SIGINT | inconclusive (Haiku may have finished); design: Agent Desk decides `interrupted` itself from its own cancel flag, SIGINT then SIGTERM after 3 s |
| Haiku decision quality | picked `retry` for "No module named pytest" in 3/3 trials → env-error rule must run before the model |

Conclusion: `claude-cli` gets `sandbox_write` (needs the settings above); Worker on Claude is allowed.

## Spike M7 — Codex token overhead & cache (2026-10-03, codex 0.159.0, gpt-5.6-luna, effort low)
Trivial prompt ("Reply OK"), cwd = a repo with a 25k-char AGENTS.md and 75 skills:

| thread/start `config` | 1st-turn input tokens |
|---|---|
| none (before M7) | 23,366 |
| `project_doc_max_bytes=0` | 17,361 (AGENTS.md gone) |
| + `skills.config=[{name, enabled:false}...]` (from `skills/list`) | **12,959** (skills block 25k -> 6k chars) |
`include_*_instructions=false` keys: no measurable effect.

Cache across turns of one thread (turn 2 cachedInputTokens):
- same outputSchema: 16,128 · different outputSchema: **0** · no schema: 17,152.
=> the output schema is part of the cached prefix; Main must keep ONE schema per thread.
- `thread/resume {threadId}` in a NEW app-server process works and keeps the cache (16,128).
- Per-thread effort falls back to ~/.codex/config.toml (`high`) unless turn/start sends `effort`.

# Spike D — Antigravity CLI `agy` 1.2.16, Google subscription (2026-10-03)

Measured with `agy -p ... --output-format stream-json --model gemini-3.8-flash-low` in scratch dirs; effects checked on disk.

| Question | Result |
|---|---|
| Models | `agy models` → `id<TAB>label` lines after a "Fetching…" header (Gemini 3.x flash/pro, Claude 5.5, GPT-OSS; effort baked into the id, e.g. `-high`) |
| stream-json | NDJSON `{event: init}` (conversation_id, tools, `permission_mode: request-review`), `{event: step_update, step_update: {step_type: user_input|agent_response|tool|finish|system_message, state: ACTIVE|DONE|ERROR, text_delta, tool_name, tool_info.{parameters,output,error}, usage}}`, final `{event: result, result: {status: SUCCESS|ERROR, response, conversation_id, usage{input,output,thinking,cache_read}_tokens, structured_output, denied_actions[], error}}` |
| Structured output | `--json-schema` → `result.structured_output` (via a `finish` tool, 2 turns) |
| Resume | `--conversation <id>` keeps context |
| Bad model | `status: ERROR`, `error: "invalid model selection…"` |
| **Permissions (headless)** | Default mode cannot prompt → `write_file` / non-allow-listed commands are **auto-denied**; a denial ends the turn with an empty `response` + `denied_actions`. Nothing written on disk. Note: the user's own `~/.gemini/antigravity-cli/settings.json` `permissions.allow` rules still apply (e.g. `command(npm test)`) — there is no flag to ignore them |
| `--mode plan` | NOT read-only: wrote a plan artifact under `~/.gemini/…/brain/` and attempted the edits → not used |
| No system-prompt flag | role card is prepended to the prompt, with an explicit read-only instruction |

Decision: runtime `agy-cli` registered with `structured_output, resume` only (no `sandbox_write`, no approval bridge) → usable for read roles (explorer, researcher, architect, main) and `llm` decision engine; `config check` refuses write roles on it.
