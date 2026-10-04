"""Interactive widgets for the PLAN/RUN screens. They own focus and keys; all state still comes from the Projection."""
from __future__ import annotations

from typing import Callable

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from . import views


class ChatLog(VerticalScroll):
    """Full chat history, mounted message by message, plus one live bubble at the end. Follows the bottom unless you scrolled up."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.shown = 0
        self.bubble = Static("", classes="bubble")

    def on_mount(self) -> None:
        self.mount(self.bubble)

    def at_bottom(self) -> bool:
        return self.scroll_offset.y >= self.max_scroll_y - 1

    def sync(self, chat: list[dict], bubble: RenderableType | None) -> None:
        follow = self.at_bottom()
        new = chat[self.shown:]
        if len(chat) < self.shown:                    # projection was rebuilt: start over
            for w in list(self.query(".msg")):
                w.remove()
            new, self.shown = chat, 0
        for m in new:
            self.mount(Static(views.message_view(m), classes="msg"), before=self.bubble)
        self.shown = len(chat)
        self.bubble.update(bubble if bubble is not None else "")
        self.bubble.display = bubble is not None
        if follow and (new or bubble is not None):
            self.call_after_refresh(self.scroll_end, animate=False)


class KeyCard(Static, can_focus=True):
    """A boxed card answered with single keys (proposal, quota). Esc / Tab go back to the composer."""

    class Pressed(Message):
        def __init__(self, card: "KeyCard", key: str):
            super().__init__()
            self.card, self.key = card, key

    def __init__(self, keys: set[str], **kw):
        super().__init__("", **kw)
        self.keys = keys

    def on_key(self, event: events.Key) -> None:
        if event.key in self.keys:
            event.stop(); event.prevent_default()
            self.post_message(self.Pressed(self, event.key))
        elif event.key == "escape":
            event.stop(); event.prevent_default()
            self.app.focus_composer()


class QuestionCard(Static, can_focus=True):
    """Open questions one at a time: ▸ cursor on the recommended option, 1-9 picks, ↑↓ + Enter, any other key = free-text answer."""

    class Answered(Message):
        def __init__(self, qid: str, option: int | None, text: str | None):
            super().__init__()
            self.qid, self.option, self.text = qid, option, text

    class FreeText(Message):
        def __init__(self, qid: str, first: str):
            super().__init__()
            self.qid, self.first = qid, first

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.questions: list[dict] = []
        self.cursor = 0
        self.qid: str | None = None

    @property
    def current(self) -> dict | None:
        return self.questions[0] if self.questions else None

    def set_questions(self, qs: list[dict]) -> None:
        self.questions = qs
        q = self.current
        if q and q["id"] != self.qid:                     # a new question: start on the recommendation
            self.qid = q["id"]
            self.cursor = min(q.get("recommended") or 0, len(q["options"]) - 1)
        if not q:
            self.qid = None
        self.update(self._render_card())

    def _render_card(self) -> RenderableType:
        q = self.current
        if not q:
            return ""
        body = Text(q["text"] + "\n", style="bold")
        for i, o in enumerate(q["options"]):
            on = i == self.cursor
            body.append(("▸ " if on else "  ") + f"{i + 1}) {o}", style="bold yellow" if on else "")
            body.append("  (recommended)\n" if q.get("recommended") == i else "\n", style="green")
        if q.get("evidence"):
            body.append(f"evidence: {q['evidence']}\n", style="bright_black")
        body.append(f"1–{len(q['options'])} pick · ↑↓ + Enter · type to answer in your own words · Esc back to chat", style="bright_black")
        more = f"  ({len(self.questions)} open)" if len(self.questions) > 1 else ""
        return Panel(body, title=f"{q['id']}{more}", title_align="left", border_style="yellow" if self.has_focus else "bright_black", padding=(0, 1))

    def on_focus(self) -> None:
        self.update(self._render_card())

    def on_blur(self) -> None:
        self.update(self._render_card())

    def on_key(self, event: events.Key) -> None:
        q = self.current
        if not q:
            return
        k, n = event.key, len(q["options"])
        if k in ("up", "down"):
            self.cursor = (self.cursor + (1 if k == "down" else -1)) % n
        elif k == "enter":
            self.post_message(self.Answered(q["id"], self.cursor, None))
        elif k.isdigit() and 1 <= int(k) <= n:
            self.cursor = int(k) - 1
            self.post_message(self.Answered(q["id"], int(k) - 1, None))
        elif k == "escape":
            self.app.focus_composer()
        elif event.is_printable and event.character:
            self.post_message(self.FreeText(q["id"], event.character))
        else:
            return
        event.stop(); event.prevent_default()
        self.update(self._render_card())


class SelectList(Static, can_focus=True):
    """A focusable list rendered by a function of the selected index. ↑↓ move; other keys go to `on_action`."""

    class Action(Message):
        def __init__(self, lst: "SelectList", key: str):
            super().__init__()
            self.lst, self.key = lst, key

    class Typed(Message):
        def __init__(self, char: str):
            super().__init__()
            self.char = char

    def __init__(self, keys: set[str], forward: bool = False, **kw):
        super().__init__("", **kw)
        self.keys = keys
        self.forward = forward                    # other printable keys start a message in the composer
        self.sel = 0
        self.count = 0

    def on_key(self, event: events.Key) -> None:
        k = event.key
        if k in ("up", "down") and self.count:
            self.sel = (self.sel + (1 if k == "down" else -1)) % self.count
        elif k in self.keys:
            self.post_message(self.Action(self, k))
        elif k == "escape":
            self.app.focus_composer()
        elif self.forward and event.is_printable and event.character:
            self.post_message(self.Typed(event.character))
        else:
            return
        event.stop(); event.prevent_default()
        self.app.dirty = True
