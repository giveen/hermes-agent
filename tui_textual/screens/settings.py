"""
Settings screen — modal form for configuring session toggles.

Groups all the config.set toggle commands into a single form with:
- Checkbox for boolean toggles (yolo)
- Select dropdowns for multi-value options
- Input fields for text values (personality, skin)

Each value loads on mount and writes via config.set on change.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from tui_textual.gateway_client import GatewayClient


class SettingsScreen(ModalScreen[None]):
    """Modal settings screen for all config toggles."""

    CSS = """
    #settings-dialog {
        padding: 1 2;
        min-width: 50;
        max-width: 90;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
    }

    #settings-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .section {
        margin: 1 0;
        border: solid $secondary;
        padding: 1;
    }

    .section-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .row {
        height: 3;
        margin: 0 1;
    }

    Label {
        width: 20;
        padding: 0 1;
    }

    Select, Checkbox {
        width: 30;
    }

    Input {
        width: 30;
    }

    #buttons {
        align: center middle;
        margin-top: 1;
    }
    """

    # Config key → (label, options, type)
    # type: "bool" (Checkbox), "select" (Select), "text" (Input)
    TOGGLES: dict[str, tuple[str, Any, str]] = {
        "yolo": ("YOLO mode", None, "bool"),

        "reasoning": ("Reasoning effort", [
            ("None", "none"),
            ("Minimal", "minimal"),
            ("Low", "low"),
            ("Medium", "medium"),
            ("High", "high"),
            ("Extra high", "xhigh"),
            ("Show reasoning", "show"),
            ("Hide reasoning", "hide"),
            ("Full", "full"),
            ("Clamp", "clamp"),
        ], "select"),

        "fast": ("Fast mode", [
            ("Normal", "normal"),
            ("Fast", "fast"),
        ], "select"),

        "footer": ("Reply footer", [
            ("On", "on"),
            ("Off", "off"),
            ("Status", "status"),
        ], "select"),

        "codex-runtime": ("Codex runtime", [
            ("Auto", "auto"),
            ("Codex app server", "codex_app_server"),
        ], "select"),

        "verbose": ("Tool progress", [
            ("Off", "off"),
            ("New only", "new"),
            ("All", "all"),
            ("Verbose", "verbose"),
            ("Log", "log"),
        ], "select"),

        "timestamps": ("Timestamps", [
            ("On", "on"),
            ("Off", "off"),
        ], "select"),

        "statusbar": ("Status bar", [
            ("Off", "off"),
            ("Top", "top"),
            ("Bottom", "bottom"),
        ], "select"),

        "indicator": ("Busy indicator", [
            ("Kaomoji", "kaomoji"),
            ("Emoji", "emoji"),
            ("Unicode", "unicode"),
            ("ASCII", "ascii"),
        ], "select"),

        "busy": ("Busy Enter mode", [
            ("Queue", "queue"),
            ("Steer", "steer"),
            ("Interrupt", "interrupt"),
        ], "select"),

        "personality": ("Personality", None, "text"),
        "skin": ("Skin/theme", None, "text"),
    }

    SESSION_TOGGLES = ["yolo", "reasoning", "fast", "footer", "codex-runtime"]
    DISPLAY_TOGGLES = ["verbose", "timestamps", "statusbar", "indicator", "busy"]
    TEXT_TOGGLES = ["personality", "skin"]

    def __init__(self, gateway: GatewayClient) -> None:
        super().__init__()
        self._gateway: GatewayClient = gateway
        self._values: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Static("[bold #FFD700]⚙ Settings[/]", id="settings-title")

            with ScrollableContainer():
                # Session section
                with Vertical(classes="section"):
                    yield Static("Session", classes="section-title")
                    for key in self.SESSION_TOGGLES:
                        label, options, kind = self.TOGGLES[key]
                        with Horizontal(classes="row"):
                            yield Label(label)
                            if kind == "bool":
                                yield Checkbox(label, id=f"cfg-{key}")
                            elif kind == "select":
                                yield Select(
                                    options or [],
                                    id=f"cfg-{key}",
                                    prompt="(loading...)",
                                )

                # Display section
                with Vertical(classes="section"):
                    yield Static("Display", classes="section-title")
                    for key in self.DISPLAY_TOGGLES:
                        label, options, kind = self.TOGGLES[key]
                        with Horizontal(classes="row"):
                            yield Label(label)
                            if kind == "select":
                                yield Select(
                                    options or [],
                                    id=f"cfg-{key}",
                                    prompt="(loading...)",
                                )

                # Text toggles
                with Vertical(classes="section"):
                    yield Static("Text Settings", classes="section-title")
                    for key in self.TEXT_TOGGLES:
                        label, options, kind = self.TOGGLES[key]
                        with Horizontal(classes="row"):
                            yield Label(label)
                            yield Input(placeholder=f"Set {label.lower()}", id=f"cfg-{key}")

            with Horizontal(id="buttons"):
                yield Button("Close", variant="primary", id="close")
                yield Button("Reset all", variant="default", id="reset")

    async def on_mount(self) -> None:
        """Load current config values from backend."""
        for key in self.SESSION_TOGGLES + self.DISPLAY_TOGGLES + self.TEXT_TOGGLES:
            try:
                result = await self._gateway.get_config(key)
                val = result.get("value") if isinstance(result, dict) else result
                self._values[key] = val
            except Exception:
                self._values[key] = None

        self._update_widgets()

    def _update_widgets(self) -> None:
        """Update all widgets with loaded config values."""
        for key, val in self._values.items():
            label, _options, kind = self.TOGGLES[key]
            if kind == "bool" and val is not None:
                widget = self.query_one(f"#cfg-{key}", Checkbox)
                widget.value = str(val) in ("1", "true", "True", "yes", "on")
            elif kind == "select":
                try:
                    widget = self.query_one(f"#cfg-{key}", Select)
                    if val is not None:
                        # Find matching option value
                        str_val = str(val)
                        for opt_label, opt_value in (widget._options or []):
                            if str(opt_value) == str_val:
                                widget.value = opt_value
                                break
                        else:
                            # Set value directly if not in options
                            widget.value = str_val
                except Exception:
                    pass
            elif kind == "text":
                try:
                    widget = self.query_one(f"#cfg-{key}", Input)
                    if val is not None:
                        widget.value = str(val)
                except Exception:
                    pass

    async def _save_config(self, key: str, value: Any) -> None:
        """Persist a config change to the backend."""
        try:
            await self._gateway.set_config(key, value)
            self._values[key] = value
        except Exception as exc:
            # Show error in the UI
            close_btn = self.query_one("#close", Button)
            close_btn.label = f"⚠ {exc}"
            self.set_timer(3.0, lambda: setattr(close_btn, "label", "Close"))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Handle checkbox toggle."""
        key = event.checkbox.id or ""
        key = key.removeprefix("cfg-")
        if key in self.TOGGLES:
            value = "1" if event.value else "0"
            self.set_timer(0, lambda: self._save_config(key, value))

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle select dropdown change."""
        key = event.select.id or ""
        key = key.removeprefix("cfg-")
        if key in self.TOGGLES and event.value is not None and event.value != Select.BLANK:
            self.set_timer(0, lambda: self._save_config(key, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle text input submission."""
        key = event.input.id or ""
        key = key.removeprefix("cfg-")
        if key in self.TOGGLES and event.value:
            self.set_timer(0, lambda: self._save_config(key, event.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "reset":
            self._reset_all()

    def _reset_all(self) -> None:
        """Reset all config toggles to default (empty = unset)."""
        for key in self.SESSION_TOGGLES + self.DISPLAY_TOGGLES:
            self.set_timer(0, lambda k=key: self._save_config(k, ""))
        self._values = {k: None for k in self._values}
        self._update_widgets()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
