from __future__ import annotations

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class TextScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("q", "dismiss", "close")]
    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > VerticalScroll { width: 92%; height: 90%; border: round #4a5a7a; background: #0c1018; padding: 0 1; }
    """

    def __init__(self, body: RenderableType, title: str = ""):
        super().__init__()
        self.body, self.ttl = body, title

    def compose(self) -> ComposeResult:
        with VerticalScroll() as v:
            v.border_title = self.ttl + "   (esc to close)"
            yield Static(self.body)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ModalScreen[str]):
    """A boxed prompt answered with single keys. Dismisses with the key pressed."""
    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen > Static { width: 84%; border: round #b8860b; background: #111827; padding: 1 2; }
    """

    def __init__(self, body: RenderableType, keys: dict[str, str], title: str = ""):
        super().__init__()
        self.body, self.keys, self.ttl = body, keys, title

    def compose(self) -> ComposeResult:
        s = Static(self.body)
        s.border_title = self.ttl
        yield s

    def on_key(self, event) -> None:
        if event.key in self.keys:
            event.stop()
            self.dismiss(self.keys[event.key])
