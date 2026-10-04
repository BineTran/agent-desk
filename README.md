# Agent Desk

**Local-first coding-agent harness with observable multi-agent orchestration.**

Agent Desk drives the coding-agent CLIs you already pay for — **Codex**, **Claude Code** and
(optionally) **Antigravity** — through one workflow: *plan → build → review → verify*. Every step,
decision and token count is visible in a terminal UI, and every component (runtimes, roles,
decision engines) is a plugin you choose in config.

> Status: early alpha. Expect breaking changes.

## How it works

- **Roles** run on top of the agent CLIs, each with its own model, effort, access and limits:
  `main` (plans and talks to you), `explorer`, `worker` (the only writer), `researcher`,
  `architect` (reviews the plan and the result), `deployer` (merge/push, every push asks you),
  `debug` (gatekeeper for secrets).
- Work happens in a **git worktree** on its own branch, so your checkout is never touched.
- **Verification** commands from `.agent-desk.yaml` are compared with a baseline on the base
  commit: only *new* failures block.
- **Subscriptions, not API keys**: each CLI logs in with your ChatGPT / Claude plan. Agent Desk keeps
  those logins in its own profile dirs and never touches `~/.codex` or `~/.claude`.
- Built with LangGraph (workflow + checkpoints), Textual (TUI), SQLite (events), Pydantic (config).

## Requirements

| What | Why |
|---|---|
| macOS or Linux, `git` | worktrees, the CLIs |
| [uv](https://docs.astral.sh/uv/) | installs Python ≥ 3.12 and the dependencies |
| Node.js ≥ 18 + npm | to install the agent CLIs |
| At least one agent CLI + a subscription | Codex CLI (ChatGPT plan) and/or Claude Code (Claude plan) |

The default config uses **Codex** for most roles and **Claude (Haiku)** for a few small decisions;
you can point every role at either one (see [Configuration](#configuration)).

## Setup on a new machine

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(or `brew install uv`). Open a new terminal afterwards. uv downloads Python 3.12 by itself when
needed; to do it now: `uv python install 3.12`.

### 2. Get the code

```bash
git clone https://github.com/BineTran/agent-desk.git
cd agent-desk
uv sync --extra dev
```

### 3. Put `agent-desk` on your PATH

```bash
uv tool install --editable .
```

`--editable` means a later `git pull` is enough to update. If `pyproject.toml` dependencies change,
run `uv tool install --editable . --reinstall`. If the shell says `command not found`, run
`uv tool update-shell` and open a new terminal. (Without this step use `uv run agent-desk …` from
the repo folder.)

### 4. Install the agent CLIs

```bash
npm install -g @openai/codex
npm install -g @anthropic-ai/claude-code
codex --version && claude --version
```

Install at least one. Antigravity is optional — see [step 5b](#5b-optional-antigravity-cli-agy).

### 5. Log in Agent Desk's own accounts

```bash
agent-desk account login codex
agent-desk account login claude
agent-desk account ls
```

- Each login runs the official CLI's login in a separate profile dir
  (`~/.agent-desk/accounts/<name>`); the token stays with the CLI, Agent Desk never sees it.
- The browser opens in a **private window** so you can pick which account to use; add `--normal`
  to use your normal browser session instead.
- After login the identity (email · org) is **pinned**: runs refuse to start if that account later
  turns out to be someone else.
- `account ls` should show `ok` for each account you use.

Several subscriptions of the same CLI (e.g. personal + team):

```bash
agent-desk account add team claude      # adds the account, then logs it in
agent-desk account use architect team   # one role runs on it
agent-desk account use claude team      # or every role on the claude provider
```

### 5b. (Optional) Antigravity CLI (`agy`)

Antigravity runs Gemini (and other) models on a Google subscription. In Agent Desk it can only
take **read-only roles** (`main`, `explorer`, `researcher`, `architect`) and the `llm` decision
engine: in headless mode `agy` auto-denies writes and has no write sandbox, so `config check`
refuses it for `worker` / `deployer`.

1. Install the Antigravity CLI from Google's official Antigravity download page, then let it set
   up your PATH:
   ```bash
   agy install
   agy --version
   ```
2. Sign in once by starting it interactively and following the Google sign-in prompt, then quit:
   ```bash
   agy
   ```
   Unlike Codex/Claude, `agy` keeps a single login in its own folder (`~/.gemini/antigravity-cli`);
   Agent Desk uses that login as is (no `agent-desk account` profiles for `agy`).
3. Check that it works and see the model ids:
   ```bash
   agy models
   ```
4. Point read-only roles at it (the `antigravity` provider is already defined in the defaults,
   with `fast: gemini-3.8-flash-medium`, `strong: gemini-3.1-pro-high`):
   ```bash
   agent-desk config set roles.explorer.provider=antigravity roles.researcher.provider=antigravity --scope global
   agent-desk config check      # the rows should say "ok (google subscription agy …)"
   ```

Note: your own `agy` permission rules (`~/.gemini/antigravity-cli/settings.json` →
`permissions.allow`) still apply to runs started by Agent Desk; keep them read-only.

### 6. Check the install

```bash
uv run pytest -q
```

Runs the unit, workflow and TUI tests with fake CLIs — no login needed.

## Use it in a project

```bash
cd ~/code/your-project            # must be a git repo
agent-desk init                   # writes .agent-desk.yaml (detects test/lint commands)
agent-desk config check --checks  # validates config + runs the checks once on a clean checkout
agent-desk                        # opens the TUI
```

Edit `verification` in `.agent-desk.yaml` so it says what "done" means in that repo, e.g.:

```yaml
verification:
  unit: { command: "pytest -q", required: true }
  lint: { command: "ruff check {changed}", paths: ["**/*.py"] }
```

Other ways to run:

```bash
agent-desk -f brief.md                 # open the TUI with a brief pre-filled
agent-desk headless "add a --json flag to the export command"
agent-desk ls                          # past sessions
agent-desk resume <session-id>         # continue one (same worktree, branch, memory)
agent-desk show <session-id>           # read-only replay
```

### In the TUI

| Key / command | What it does |
|---|---|
| `F2` | components: which plugin each role / decision point uses |
| `F3`, `Ctrl+S`, `/settings` | settings: providers, roles, accounts, decision engines |
| `Tab`, `Esc` | move focus / navigate |
| `Ctrl+D`, `/diff` | diff of the worktree |
| `Ctrl+N`, `/new` | new task |
| `/model`, `/engine` | change a role's model or a decision engine |
| `/account` | add / login / use / pin accounts |
| `/stop`, `/resume`, `/open` | stop the run, resume, reopen an earlier session |
| `/setup`, `/checks`, `/baseline` | worktree setup and verification checks |
| `/help` | everything else |
| `Ctrl+Q`, `/quit` | quit |

## Configuration

Config is layered; later layers win:

1. built-in defaults — [`agent_desk/config/defaults.yaml`](agent_desk/config/defaults.yaml)
2. global — `~/.agent-desk/config.yaml`
3. repo — `.agent-desk.yaml`
4. command line — `--set key=value`

Change it from the TUI (`F3`) or the CLI:

```bash
agent-desk config show
agent-desk config set roles.architect.provider=claude roles.architect.model=opus --scope repo
agent-desk config check
```

API keys are off by default (`policy.subscription_only: true`). If you enable one, config only
stores the **name** of the environment variable, never the key.

## Data & safety

- Everything is local: sessions, events database, worktree cache and account profiles live in
  `~/.agent-desk` (change with `--home`).
- Every `git push` asks you; force-push is always denied; destructive filesystem commands,
  DB migrations and deployments need approval (`approval.require_for`).
- Reading env vars or secret files goes through a gatekeeper role.

## Development

```bash
uv run pytest -q              # default suite (fake CLIs)
uv run pytest -m codex        # real Codex CLI (needs `agent-desk account login codex`)
uv run pytest -m claude       # real Claude Code CLI
uv run pytest -m agy          # real Antigravity CLI
```

- `agent_desk/` — package: `runtime/` (CLI adapters), `decision/`, `context/`, `config/`, `tui/`
- `tests/` — unit, workflow, TUI and integration tests
- `docs/spike.md` — measurements against the real CLIs (overhead, caching, sandbox)
- `prototype/` — HTML UI prototypes

## Troubleshooting

| Symptom | Fix |
|---|---|
| `account ls` shows `not logged in` | `agent-desk account login <name>` |
| `MISMATCH` | the CLI is logged in as someone else than the pinned identity — log in again with the right account, or `agent-desk account pin <name>` to accept the new one |
| `DUPLICATE` | two accounts are the same login — log one in with a different account |
| `agent-desk: command not found` | `uv tool update-shell`, then open a new terminal |
| `agy not found` / antigravity rows `FAIL` | install `agy`, run `agy install`, sign in with `agy`, check `agy models` |
| `runtime agy-cli cannot enforce a write sandbox` | a write role (`worker`, `deployer`) points at `antigravity` — use codex or claude for it |
| wrong Python version | `uv python install 3.12` |
| checks fail before the agent changed anything | `agent-desk config check --checks` shows which ones; fix them or mark them not `required` |

## License

[MIT](LICENSE)
