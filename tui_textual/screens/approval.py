"""
Approval dialog — confirm or deny dangerous commands.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ApprovalDialog(ModalScreen[str]):
    """Modal dialog for approving or denying a command."""

    CSS = """
    #dialog {
        padding: 1 2;
        min-width: 40;
        max-width: 80;
        border: thick $primary;
        background: $surface;
    }

    #prompt {
        margin: 1 0;
    }

    #command-box {
        border: solid $secondary;
        padding: 1;
        margin: 1 0;
    }

    Buttons {
        align: center middle;
        margin-top: 1;
    }
    """

    def __init__(self, request_id: str, command: str, preview: str = "") -> None:
        super().__init__()
        self._request_id = request_id
        self._command = command
        self._preview = preview

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("[bold yellow]⚠ Approval Required[/]", id="title"),
            Label("The agent wants to run:", id="prompt"),
            Static(f"[bold]{self._command}[/]" if not self._preview else f"[bold]{self._command}[/]\n[dim]{self._preview}[/]", id="command-box"),
            Horizontal(
                Button("Approve", variant="primary", id="approve"),
                Button("Always", variant="default", id="always"),
                Button("Deny", variant="error", id="deny"),
                classes="buttons",
            ),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id or "deny"
        self.dismiss(action)
