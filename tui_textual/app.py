"""
Hermes Textual TUI — main application.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from tui_textual.gateway_client import GatewayClient
from tui_textual.transport import StdioTransport

logger = logging.getLogger(__name__)

_D: list[str] = []
def _debug(msg):
    _D.append(msg)
    with open("/tmp/tui.log", "a") as f:
        f.write(f"{msg}\n")


def _make_gateway_env() -> dict[str, str]:
    env = os.environ.copy()
    root = Path(os.environ.get("HERMES_PYTHON_SRC_ROOT", Path(__file__).parent.parent.resolve()))
    env["HERMES_PYTHON_SRC_ROOT"] = str(root)
    env.setdefault("HERMES_PYTHON", sys.executable)
    env.setdefault("HERMES_CWD", os.getcwd())
    for key in (
        "HERMES_TUI_RESUME", "HERMES_TUI_PROVIDER", "HERMES_TUI_TOOLSETS",
        "HERMES_TUI_SKILLS", "HERMES_TUI_QUERY", "HERMES_TUI_TOOL_PROGRESS",
        "HERMES_TUI_CHECKPOINTS", "HERMES_TUI_PASS_SESSION_ID", "HERMES_TUI_MAX_TURNS",
        "HERMES_TUI_ACTIVE_SESSION_FILE",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val
    for key in ("HERMES_MODEL", "HERMES_INFERENCE_MODEL", "TERMINAL_CWD", "HERMES_CWD"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


class HermesTUIApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+c", "interrupt", "Interrupt", show=False),
        Binding("ctrl+p", "toggle_dark", "Toggle dark", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        _debug("HermesTUIApp.__init__")
        self._gateway_proc: subprocess.Popen[str] | None = None
        self._gateway_stderr_task: asyncio.Task[None] | None = None
        self._transport = StdioTransport()
        self.gateway = GatewayClient(self._transport)
        self._resume_session_id: str | None = None

    def run(self) -> None:
        _debug("run() START")
        self._spawn_gateway()
        try:
            super().run()
        finally:
            _debug("run() FINALLY")
            self._cleanup()
        _debug("run() DONE")

    def _spawn_gateway(self) -> None:
        python = os.environ.get("HERMES_PYTHON") or sys.executable
        env = _make_gateway_env()
        cwd = env.get("HERMES_CWD") or os.getcwd()
        _debug(f"spawn gateway: {python}")
        self._gateway_proc = subprocess.Popen(
            [python, "-m", "tui_gateway.entry"],
            cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _cleanup(self) -> None:
        if self._gateway_stderr_task:
            self._gateway_stderr_task.cancel()
        self._kill_gateway()
        asyncio.run(self._transport.close())

    def _kill_gateway(self) -> None:
        proc = self._gateway_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except Exception:
            proc.kill()
            proc.wait(timeout=2.0)

    async def on_mount(self) -> None:
        _debug("on_mount START")
        resume = (os.environ.get("HERMES_TUI_RESUME") or "").strip()
        if resume:
            self._resume_session_id = resume

        proc = self._gateway_proc
        assert proc is not None
        assert proc.stdout is not None
        assert proc.stdin is not None
        assert proc.stderr is not None

        _debug("bind_pipes")
        await self._transport.bind_pipes(proc.stdin, proc.stdout)
        _debug("connect")
        await self.gateway.connect()
        _debug("forward stderr")
        self._gateway_stderr_task = asyncio.create_task(self._forward_stderr(proc.stderr))
        _debug("push ChatScreen")
        from tui_textual.screens.chat import ChatScreen
        await self.push_screen(ChatScreen(self.gateway, self._resume_session_id))
        _debug("on_mount DONE")

    async def _forward_stderr(self, stderr_stream) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, stderr_stream)
        while True:
            try:
                line = await reader.readline()
            except (OSError, EOFError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("[gateway] %s", text)

    def action_interrupt(self) -> None:
        asyncio.create_task(self.gateway.interrupt_session())

    def action_toggle_dark(self) -> None:
        self.theme = "dark" if self.theme == "light" else "light"
