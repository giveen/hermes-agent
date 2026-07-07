"""
Input dialog — collect text (plain or password) from the user.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class InputDialog(ModalScreen[str | None]):
    """Modal dialog for collecting user input."""

    CSS = """
    #dialog {
        padding: 1 2;
        min-width: 40;
        border: thick $primary;
        background: $surface;
    }

    Input {
        margin: 1 0;
    }
    """

    def __init__(self, prompt: str, *, password: bool = False) -> None:
        super().__init__()
        self._prompt = prompt
        self._password = password

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._prompt, id="prompt"),
            Input(password=self._password, id="response-input"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#response-input", Input))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
