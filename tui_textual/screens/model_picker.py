"""
Model picker screen — browse providers and select a model.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class ModelPicker(ModalScreen[str | None]):
    """Modal screen for browsing providers and selecting a model."""

    CSS = """
    #dialog {
        width: 60;
        height: 80%;
        min-height: 20;
        border: thick $primary;
        background: $surface;
    }

    #header {
        padding: 0 1;
        background: $primary;
        color: $text;
        text-style: bold;
    }

    ListView {
        height: 1fr;
        margin: 0 1;
    }

    #footer {
        padding: 0 1;
        margin-top: 1;
    }
    """

    def __init__(self, gateway: "GatewayClient") -> None:
        from tui_textual.gateway_client import GatewayClient

        super().__init__()
        self._gateway: GatewayClient = gateway
        self._providers: list[dict[str, Any]] = []
        self._current_model: str = ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("[bold]Select a Model[/]", id="header"),
            ListView(id="model-list"),
            Horizontal(
                Label("", id="model-info"),
                Button("Cancel", variant="default", id="cancel"),
            ),
            id="dialog",
        )

    def on_mount(self) -> None:
        asyncio.create_task(self._load_models())

    async def _load_models(self) -> None:
        """Fetch providers and models from the gateway."""
        list_view = self.query_one("#model-list", ListView)
        info = self.query_one("#model-info", Label)

        try:
            result = await self._gateway.get_model_options()
            self._providers = result.get("providers", [])
            self._current_model = result.get("current", "")

            if not self._providers:
                list_view.append(ListItem(Label("[dim]No providers configured[/]")))
                return

            # Group by provider
            for provider in self._providers:
                slug = provider.get("slug", "")
                label = provider.get("label") or slug
                models: list[str] = provider.get("models", [])

                if not models:
                    continue

                # Provider header (non-selectable)
                header = ListItem(Label(f"[bold #FFD700]─ {label}[/]"))
                header.can_focus = False
                list_view.append(header)

                for model_id in models:
                    is_current = model_id == self._current_model
                    prefix = "[green]●[/] " if is_current else "  "
                    item = ListItem(
                        Label(
                            f"{prefix}[bold]{model_id}[/]"
                            if is_current
                            else f"{prefix}{model_id}"
                        )
                    )
                    item._model_id = model_id
                    item._provider_slug = slug
                    list_view.append(item)

            info.update(f"[dim]{len(self._providers)} providers[/]")

        except Exception as exc:
            list_view.clear()
            list_view.append(ListItem(Label(f"[bold red]Error: {exc}[/]")))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle model selection — dismiss with full model+provider string."""
        item = event.item
        model_id = getattr(item, "_model_id", None)
        provider_slug = getattr(item, "_provider_slug", None)
        if model_id and provider_slug:
            self.dismiss(f"{model_id} --provider {provider_slug}")
        elif model_id:
            self.dismiss(model_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
