"""The big input box: multi-line, @file completion (repo fuzzy + paths outside), / commands, mention chips."""
from __future__ import annotations

import os
from pathlib import Path

from textual import events
from textual.message import Message
from textual.widgets import TextArea

from ..context.inputs import parse_mentions

COMMANDS = {"/ask": "force an answer, no changes: /ask why does X do Y?", "/quick": "force a small direct change (still verified): /quick rename foo to bar",
            "/plan": "force the full plan flow: /plan add feature X", "/approve": "lock the plan and run (/approve --skip-review to skip the architect)", "/questions": "list open questions",
            "/review": "ask the architect to review the plan again", "/diff-plan": "compare plan versions: /diff-plan 1 2",
            "/model": "switch a role: /model worker claude[@account] sonnet", "/account": "accounts: /account · add <name> [runtime] · login <name> · use <provider|role> <account> · remove <name>", "/engine": "switch a decision engine: /engine retry_or_stop none",
            "/config": "show components and configuration (F2)",
            "/settings": "change providers, models, decision engines, Jev (F3); saves to session, repo or global", "/context": "show session.md", "/diff": "everything the session changed: its commits + what is not committed yet",
            "/stop": "stop the session (keeps all work)", "/checks": "run the verification commands on a clean checkout and show which are broken (/checks --refresh)",
            "/baseline": "re-run the checks on the base commit (what already failed before this session)",
            "/setup": "prepare the worktree (link node_modules/.venv or run workspace.setup) and resume",
            "/resume": "continue unfinished work (after a quota pause, a stop or a failure)",
            "/home": "back to the home screen to start a new task or open another session (alias /new, Ctrl+N)", "/new": "start a new task (same as /home)",
            "/open": "reopen an earlier session: /open s-1a2b (see the list on the home screen)", "/pin": "pin Main's last answer as a note",
            "/help": "list commands", "/quit": "leave Agent Desk (Ctrl+Q)"}


def _subseq(q: str, s: str) -> int | None:
    """Fuzzy score: lower is better; None if q is not a subsequence of s."""
    q, s = q.lower(), s.lower()
    i = 0
    for ch in s:
        if i < len(q) and ch == q[i]:
            i += 1
    if i < len(q):
        return None
    base = os.path.basename(s)
    if base.startswith(q): return 0
    if q in base: return 1
    if q in s: return 2
    return 3 + len(s) // 20


def complete(token: str, first_token: bool, repo_files: list[str], repo: Path) -> list[tuple[str, str]]:
    """Returns [(replacement, label)]. `token` is the word under the cursor."""
    if token.startswith("@"):
        q = token[1:]
        if q.startswith(("~", "/", "./")):
            p = Path(os.path.expanduser(q))
            base, prefix = (p, "") if (q.endswith("/") and p.is_dir()) else (p.parent, p.name)
            try:
                names = sorted(os.listdir(base))
            except OSError:
                return []
            shown = q if q.endswith("/") else q[: len(q) - len(prefix)]
            out = []
            for n in names:
                if n.startswith(prefix) and not n.startswith("."):
                    d = (base / n).is_dir()
                    out.append((f"@{shown}{n}{'/' if d else ''}", "dir" if d else "file"))
            return out[:8]
        scored = []
        for f in repo_files:
            sc = _subseq(q, f)
            if sc is not None:
                scored.append((sc, len(f), f))
        scored.sort()
        return [(f"@{f}", "dir" if f.endswith("/") else "file") for _, _, f in scored[:8]]
    if first_token and token.startswith("/"):
        return [(c, d) for c, d in COMMANDS.items() if c.startswith(token)][:8]
    return []


class Composer(TextArea):
    """Enter sends. Ctrl+J / Alt+Enter inserts a newline. Tab accepts the highlighted completion. Esc leaves the box."""

    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class Suggestions(Message):
        def __init__(self, items: list[tuple[str, str]], index: int):
            super().__init__()
            self.items, self.index = items, index

    class Left(Message):
        pass

    def __init__(self, repo: Path, repo_files: list[str] | None = None, **kw):
        super().__init__("", soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kw)
        self.repo, self.repo_files = repo, repo_files or []
        self.items: list[tuple[str, str]] = []
        self.index = 0

    def _token(self) -> tuple[str, int, int, bool]:
        row, col = self.cursor_location
        line = self.document.get_line(row)[:col]
        start = max(line.rfind(" "), line.rfind("\t")) + 1
        first = self.text.lstrip().startswith("/") and row == 0 and line[:start].strip() == ""
        return line[start:], start, col, first

    def _refresh_suggestions(self) -> None:
        tok, _, _, first = self._token()
        self.items = complete(tok, first, self.repo_files, self.repo) if tok else []
        self.index = min(self.index, max(len(self.items) - 1, 0))
        self.post_message(self.Suggestions(self.items, self.index))

    def on_text_area_changed(self, _) -> None:
        self._refresh_suggestions()

    def on_text_area_selection_changed(self, _) -> None:
        self._refresh_suggestions()

    def mentions(self) -> tuple[list[str], list[str]]:
        return parse_mentions(self.text)

    def accept(self) -> bool:
        if not self.items:
            return False
        rep, _ = self.items[self.index]
        tok, start, col, _ = self._token()
        row = self.cursor_location[0]
        self.replace(rep + ("" if rep.endswith("/") else " "), (row, start), (row, col))
        self.items = []
        self.post_message(self.Suggestions([], 0))
        return True

    def _delete_left(self, n: int = 1) -> None:
        row, col = self.cursor_location
        for _ in range(n):
            if col == 0 and row == 0:
                return
            self.action_delete_left()
            row, col = self.cursor_location

    def _ime_insert(self, text: str) -> None:
        """Vietnamese IMEs (Telex/VNI via OpenKey, EVKey, Unikey) rewrite a word as backspaces + new letters,
        sometimes as one burst: apply \x08/\x7f as delete-left instead of inserting them."""
        buf = ""
        for ch in text:
            if ch in "\x08\x7f":
                if buf:
                    self.insert(buf); buf = ""
                self._delete_left()
            elif ch == "\r":
                buf += "\n"
            else:
                buf += ch
        if buf:
            self.insert(buf)

    async def _on_paste(self, event: events.Paste) -> None:
        if any(c in event.text for c in "\x08\x7f"):
            event.stop(); event.prevent_default()
            self._ime_insert(event.text)
            return
        await super()._on_paste(event)

    async def _on_key(self, event: events.Key) -> None:
        k = event.key
        if k == "ctrl+h":                                   # IME backspace sent as BS (0x08) instead of DEL
            event.stop(); event.prevent_default()
            self._delete_left()
            return
        if event.character and len(event.character) > 1 and not k.startswith(("ctrl+", "alt+")):
            event.stop(); event.prevent_default()           # multi-char burst delivered as one key
            self._ime_insert(event.character)
            return
        if k == "enter":
            event.stop(); event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if k in ("ctrl+j", "alt+enter", "shift+enter"):
            event.stop(); event.prevent_default()
            self.insert("\n")
            return
        if k == "tab" and self.items:
            event.stop(); event.prevent_default()
            self.accept()
            return
        if k in ("up", "down") and self.items:
            event.stop(); event.prevent_default()
            self.index = (self.index + (1 if k == "down" else -1)) % len(self.items)
            self.post_message(self.Suggestions(self.items, self.index))
            return
        if k == "escape":
            event.stop(); event.prevent_default()
            if self.items:
                self.items = []
                self.post_message(self.Suggestions([], 0))
            else:
                self.post_message(self.Left())
            return
        await super()._on_key(event)
