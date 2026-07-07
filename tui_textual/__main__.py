"""
Entry point for the Hermes Textual TUI.

Spawns the tui_gateway subprocess and runs the Textual app.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _resolve_python() -> str:
    """Resolve the Python interpreter to use for the gateway subprocess."""
    return os.environ.get("HERMES_PYTHON") or sys.executable


def _resolve_root() -> Path:
    """Resolve the project root."""
    return Path(os.environ.get("HERMES_PYTHON_SRC_ROOT", Path(__file__).parent.parent.resolve()))


def main():
    """Spawn the gateway and run the Textual TUI."""
    from tui_textual.app import HermesTUIApp

    app = HermesTUIApp()
    app.run()


if __name__ == "__main__":
    main()
