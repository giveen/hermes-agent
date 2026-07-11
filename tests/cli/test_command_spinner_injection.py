"""Regression: _command_spinner_frame() must resolve _COMMAND_SPINNER_FRAMES.

The constant is defined in cli.py and only reaches the display mixin via
_inject_cli_globals(). Previously it was missing from the injected allow-list,
so bare-name LOAD_GLOBAL raised NameError on every input-line redraw while a
slow slash command (e.g. /compress) ran. See cli_display_mixin.py:1710.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_spinner_frames_injected():
    import hermes_cli.cli_display_mixin as mixin_mod
    from hermes_cli.cli_display_mixin import _inject_cli_globals

    _inject_cli_globals()

    assert "_COMMAND_SPINNER_FRAMES" in mixin_mod.__dict__
    frames = mixin_mod.__dict__["_COMMAND_SPINNER_FRAMES"]
    assert len(frames) > 0


def test_command_spinner_frame_returns_valid_frame():
    import hermes_cli.cli_display_mixin as mixin_mod
    from hermes_cli.cli_display_mixin import _inject_cli_globals

    _inject_cli_globals()

    cli = object.__new__(mixin_mod.CLIDisplayMixin)
    frame = cli._command_spinner_frame()
    assert frame in mixin_mod.__dict__["_COMMAND_SPINNER_FRAMES"]
