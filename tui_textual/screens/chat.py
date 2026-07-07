"""
Chat screen — main conversation interface with input and transcript.

Shows startup banner (version, model, session info) and routes all slash
commands to the gateway backend.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, RichLog

from tui_textual.gateway_client import GatewayClient

# Commands we handle locally (never forwarded to gateway)
_LOCAL_COMMANDS = frozenset({"quit", "exit", "clear", "help", "redraw"})


class ChatScreen(Screen[None]):
    """Main chat screen with transcript, composer, and slash command routing."""

    CSS = """
    #transcript {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }
    Input {
        dock: bottom;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "focus_input", "Input", show=False),
        Binding("ctrl+l", "clear_screen", "Clear", show=False),
        Binding("ctrl+s", "open_settings", "Settings", show=True),
    ]

    def __init__(
        self,
        gateway: GatewayClient,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._gateway: GatewayClient = gateway
        self._resume_session_id = resume_session_id
        self._session_id: str = ""
        self._current_response = ""
        self._info: dict[str, Any] = {}
        self._model: str = ""
        self._last_user_text: str = ""
        # Full command catalog — loaded at startup
        self._command_pairs: list[list[str]] = []
        self._command_categories: list[dict[str, Any]] = []

    def compose(self):
        yield Header(show_clock=True)
        yield RichLog(id="transcript", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="Type a message or /command...", id="composer-input")
        yield Footer()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        """Register event handlers and start session setup."""
        self._gateway.on("gateway.ready", self._on_gateway_ready)
        self._gateway.on("message.delta", self._on_message_delta)
        self._gateway.on("message.complete", self._on_message_complete)
        self._gateway.on("tool.start", self._on_tool_start)
        self._gateway.on("tool.complete", self._on_tool_complete)
        self._gateway.on("session.info", self._on_session_info)
        self._gateway.on("status.update", self._on_status_update)
        self._gateway.on("approval.request", self._on_approval_request)
        self._gateway.on("sudo.request", self._on_sudo_request)
        self._gateway.on("secret.request", self._on_secret_request)

        asyncio.create_task(self._initialize_session())
        self.set_focus(self.query_one("#composer-input"))

    # ── Session initialization ────────────────────────────────────────

    async def _initialize_session(self) -> None:
        """Create (or resume) the session and show the startup banner."""
        transcript = self.query_one("#transcript", RichLog)
        try:
            if self._resume_session_id:
                result = await self._gateway.resume_session(self._resume_session_id)
            else:
                result = await self._gateway.create_session()

            self._session_id = result.get("session_id", "")
            self._info = result.get("info", {}) or {}
            sid_short = self._session_id[:12] if self._session_id else "?"

            # ── Startup banner ─────────────────────────────────────
            model = self._info.get("model") or result.get("model") or "resolving..."
            provider = self._info.get("provider") or result.get("provider") or ""
            self._model = model
            self._provider = provider
            branch = result.get("branch") or self._info.get("branch") or ""
            repo = result.get("repo") or self._info.get("repo") or ""

            transcript.write(f"[bold #FFD700]═══ Hermes Agent [/] [dim]v0.18.0[/]")
            transcript.write(f"[dim]Session:[/] {sid_short}")
            if model and model != "resolving...":
                transcript.write(f"[dim]Model:[/]   {model}")
            if provider:
                transcript.write(f"[dim]Provider:[/] {provider}")
            if branch:
                transcript.write(f"[dim]Branch:[/]  {branch}")
            transcript.write("")

            # Load command catalog for /help display
            await self._load_commands()

            # Show session hint
            if self._resume_session_id:
                title = result.get("title") or ""
                transcript.write(f"[bold green]Resumed session[/] — {title}")
            else:
                transcript.write(f"[dim]Type /help for commands, or just type your message.[/]")
                transcript.write(f"[dim]Type /quit to exit.[/]")
            transcript.write("")

            # Load history for resumed sessions
            if self._resume_session_id:
                messages = await self._gateway.get_session_history()
                for msg in messages:
                    self._display_message(msg)

        except Exception as exc:
            transcript.write(f"[bold red]Error creating session:[/] {exc}")

    async def _load_commands(self) -> None:
        """Fetch the slash command catalog from the gateway."""
        try:
            catalog = await self._gateway.get_commands_catalog()
            # catalog is the dict from commands.catalog — pairs, categories, etc.
            # GatewayClient.get_commands_catalog returns result.get("commands", [])
            # but the actual key is "pairs" — let's read both
            self._command_pairs = catalog.get("pairs") or catalog.get("commands") or []
            self._command_categories = catalog.get("categories") or []
        except Exception:
            self._command_pairs = []

    # ── Input handling ──────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route user input — slash commands vs. prompt submit."""
        text = event.value.strip()
        if not text:
            return
        self.query_one("#composer-input", Input).value = ""

        self._display_message({"role": "user", "content": text})

        if text.startswith("/"):
            asyncio.create_task(self._route_slash(text))
        else:
            self._last_user_text = text
            asyncio.create_task(self._submit_prompt(text))

    async def _submit_prompt(self, text: str) -> None:
        """Submit a normal prompt to the gateway."""
        try:
            await self._gateway.submit_prompt(text)
        except Exception as exc:
            t = self.query_one("#transcript", RichLog)
            t.write(f"[bold red]Error:[/] {exc}")

    # ── Slash command routing ───────────────────────────────────────

    async def _route_slash(self, text: str) -> None:
        """Route a slash command: local handler, native RPC, or gateway fallback."""
        parts = text[1:].split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        transcript = self.query_one("#transcript", RichLog)

        # ── Local (client-side) commands ────────────────────────
        if cmd == "help":
            self._show_help(transcript)
            return
        if cmd == "clear":
            transcript.clear()
            return
        if cmd == "redraw":
            transcript.clear()
            transcript.write("[dim]Screen redrawn.[/]")
            return
        if cmd in ("quit", "exit"):
            self.app.exit()
            return

        # ── Model picker ───────────────────────────────────────
        if cmd == "model":
            await self._handle_model(arg, transcript)
            return

        # ── Session commands ───────────────────────────────────
        if cmd == "new" or cmd == "reset":
            await self._handle_new_session(arg, transcript)
            return

        if cmd == "undo":
            await self._handle_undo(transcript)
            return

        if cmd == "retry":
            await self._handle_retry(transcript)
            return

        if cmd == "status":
            await self._handle_status(transcript)
            return

        if cmd == "title":
            await self._handle_title(arg, transcript)
            return

        if cmd == "save":
            await self._handle_save(transcript)
            return

        if cmd == "history":
            await self._handle_history(arg, transcript)
            return

        if cmd in ("branch", "fork"):
            await self._handle_branch(arg, transcript)
            return

        if cmd == "compress" or cmd == "compact":
            await self._handle_compress(arg, transcript)
            return

        if cmd in ("background", "bg", "btw"):
            await self._handle_background(arg, transcript)
            return

        if cmd in ("queue", "q"):
            await self._handle_queue(arg, transcript)
            return

        if cmd == "steer":
            await self._handle_steer(arg, transcript)
            return

        # ── Info commands ──────────────────────────────────────
        if cmd == "usage":
            await self._handle_usage(transcript)
            return

        if cmd == "stop":
            await self._handle_stop(transcript)
            return

        if cmd == "sessions":
            await self._handle_sessions(arg, transcript)
            return

        if cmd == "copy":
            await self._handle_copy(arg, transcript)
            return

        if cmd == "fortune":
            self._handle_fortune(arg, transcript)
            return

        if cmd == "logs":
            await self._handle_logs(arg, transcript)
            return

        if cmd == "update":
            self._handle_update(transcript)
            return

        if cmd == "prompt" or cmd == "compose":
            await self._handle_prompt(arg, transcript)
            return

        # ── Migrated commands (native RPC, avoid slash.exec) ──────
        if cmd == "voice":
            await self._handle_voice(arg, transcript)
            return

        if cmd == "tools":
            await self._handle_tools(arg, transcript)
            return

        if cmd == "skills":
            await self._handle_skills(arg, transcript)
            return

        if cmd == "cron":
            await self._handle_cron(arg, transcript)
            return

        if cmd == "reload":
            await self._handle_reload(transcript)
            return

        if cmd in ("reload-mcp", "reload_mcp"):
            await self._handle_reload_mcp(arg, transcript)
            return

        if cmd in ("reload-skills", "reload_skills"):
            await self._handle_reload_skills(transcript)
            return

        if cmd == "browser":
            await self._handle_browser(arg, transcript)
            return

        if cmd == "plugins":
            await self._handle_plugins(transcript)
            return

        if cmd == "image":
            await self._handle_image(arg, transcript)
            return

        if cmd == "rollback":
            await self._handle_rollback(arg, transcript)
            return


        if cmd in ("agents", "tasks"):
            await self._handle_agents(arg, transcript)
            return

        if cmd == "credits":
            await self._handle_credits(transcript)
            return

        if cmd == "billing":
            await self._handle_billing(transcript)
            return

        if cmd == "insights":
            await self._handle_insights(arg, transcript)
            return

        if cmd in ("journey", "learning", "memory-graph"):
            await self._handle_journey(arg, transcript)
            return

        if cmd == "config":
            await self._handle_config(transcript)
            return

        if cmd == "toolsets":
            await self._handle_toolsets(transcript)
            return

        if cmd == "paste":
            await self._handle_paste(transcript)
            return

        if cmd == "handoff":
            await self._handle_handoff(arg, transcript)
            return

        if cmd in ("version", "v"):
            self._handle_version(transcript)
            return

        if cmd == "settings":
            await self._handle_settings(transcript)
            return

        if cmd == "pet":
            await self._handle_pet(arg, transcript)
            return

        if cmd == "hatch" or cmd == "generate-pet":
            await self._handle_hatch(arg, transcript)
            return

        if cmd == "resume":
            await self._handle_resume(arg, transcript)
            return
        output = ""
        try:
            result = await self._gateway.execute_slash(cmd, arg)
            output = (result.get("output") or result.get("text") or "").strip()
        except Exception:
            pass

        if not output or output == "(no output)":
            try:
                result = await self._gateway.dispatch_command(name=cmd, arg=arg)
                out_type = result.get("type", "")
                out_text = result.get("output", result.get("text", "")).strip()
                if out_text:
                    output = out_text
                elif out_type == "skill":
                    output = f"[dim]Skill command {cmd} dispatched to agent.[/]"
                elif out_type == "exec":
                    output = f"[dim]Quick command {cmd} executed.[/]"
                elif out_type == "alias":
                    target = result.get("target", "")
                    output = f"[dim]Alias /{cmd} → /{target}[/]"
            except Exception:
                pass
        if output:
            transcript.write(output)
            transcript.write("")

    # ── Command handlers ──────────────────────────────────────────

    async def _handle_model(self, arg: str, transcript: RichLog) -> None:
        """Switch model — show picker or forward arg."""
        if arg:
            try:
                result = await self._gateway.set_config("model", arg)
                new_val = result.get("value", "")
                warning = result.get("warning", "")
                if new_val:
                    self._model = new_val
                    transcript.write(f"[bold green]✓ Active model:[/] {new_val}")
                if warning:
                    transcript.write(f"[yellow]⚠ {warning}[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Model switch failed:[/] {exc}")
        else:
            from tui_textual.screens.model_picker import ModelPicker

            model_id = await self.app.push_screen_wait(ModelPicker(self._gateway))
            if model_id:
                transcript.write(f"[bold]Switching to:[/] {model_id}")
                try:
                    result = await self._gateway.set_config("model", model_id)
                    new_val = result.get("value", "")
                    warning = result.get("warning", "")
                    if new_val:
                        self._model = new_val
                        transcript.write(f"[bold green]✓ Active model:[/] {new_val}")
                    if warning:
                        transcript.write(f"[yellow]⚠ {warning}[/]")
                except Exception as exc:
                    transcript.write(f"[bold red]Model switch failed:[/] {exc}")
        transcript.write("")

    async def _handle_new_session(self, name: str, transcript: RichLog) -> None:
        """Start a new session."""
        transcript.clear()
        try:
            result = await self._gateway.create_session()
            self._session_id = result.get("session_id", "")
            self._info = result.get("info", {}) or {}
            sid_short = self._session_id[:12] if self._session_id else "?"
            model = self._info.get("model") or result.get("model") or "resolving..."
            provider = self._info.get("provider") or result.get("provider") or ""
            self._model = model
            self._provider = provider
            transcript.write(f"[bold #FFD700]═══ New Session — {sid_short}[/]")
            if model:
                transcript.write(f"[dim]Model:[/]   {model}")
            if provider:
                transcript.write(f"[dim]Provider:[/] {provider}")
            if name:
                try:
                    await self._gateway.set_session_title(name)
                    transcript.write(f"[dim]Title:[/]   {name}")
                except Exception:
                    pass
            transcript.write("")
        except Exception as exc:
            transcript.write(f"[bold red]Error creating session:[/] {exc}")

    async def _handle_undo(self, transcript: RichLog) -> None:
        """Undo last exchange — backend undo."""
        if not self._session_id:
            transcript.write("[dim]Nothing to undo.[/]")
            transcript.write("")
            return
        try:
            result = await self._gateway.undo_session()
            removed = int(result.get("removed", 0))
            if removed > 0:
                transcript.write(f"[dim]Undid {removed} messages. Your conversation has been rewound.[/]")
            else:
                transcript.write("[dim]Nothing to undo. No previous assistant turn to rewind.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Undo failed:[/] {exc}")
        transcript.write("")

    async def _handle_retry(self, transcript: RichLog) -> None:
        """Undo last exchange + resend the last user message."""
        if not self._session_id:
            transcript.write("[dim]Nothing to retry.[/]")
            transcript.write("")
            return
        if not self._last_user_text:
            transcript.write("[dim]No user message to retry. Submit a non-slash message first.[/]")
            transcript.write("")
            return
        text = self._last_user_text
        try:
            # Undo the last assistant turn first
            await self._gateway.undo_session()
            preview = text[:80] + "…" if len(text) > 80 else text
            transcript.write(f"[dim]Retrying: {preview}[/]")
            # Re-submit the user's last message as a new prompt
            await self._gateway.submit_prompt(text)
        except Exception as exc:
            transcript.write(f"[bold red]Retry failed:[/] {exc}")

    async def _handle_status(self, transcript: RichLog) -> None:
        """Show formatted session status."""
        if not self._session_id:
            transcript.write("[dim]No active session.[/]")
        else:
            transcript.write(f"[bold #FFD700]Session Status[/]")
            transcript.write(f"  [dim]Session ID:[/]  {self._session_id[:12]}")
            transcript.write(f"  [dim]Model:[/]       {self._model or '?'}")
            if self._provider:
                transcript.write(f"  [dim]Provider:[/]    {self._provider}")
            try:
                status = await self._gateway.get_session_status()
                output = status.get("output", "") or ""
                if output:
                    for line in output.split("\n"):
                        if line.strip():
                            transcript.write(f"  {line}")
            except Exception:
                pass
        transcript.write("")

    async def _handle_title(self, arg: str, transcript: RichLog) -> None:
        """Set or show the current session title."""
        if arg:
            try:
                await self._gateway.set_session_title(arg)
                transcript.write(f"[bold green]✓ Session title set:[/] {arg}")
            except Exception as exc:
                transcript.write(f"[bold red]Failed to set title:[/] {exc}")
        else:
            try:
                title = await self._gateway.get_session_title()
                if title:
                    transcript.write(f"[bold]Session title:[/] {title}")
                else:
                    transcript.write("[dim]No title set. Use /title <name> to set one.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Failed to get title:[/] {exc}")
        transcript.write("")

    async def _handle_save(self, transcript: RichLog) -> None:
        """Save the current conversation."""
        try:
            result = await self._gateway.save_session()
            file = result.get("file", "")
            if file:
                transcript.write(f"[bold green]✓ Conversation saved to:[/] {file}")
            else:
                transcript.write("[dim]Conversation saved.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Save failed:[/] {exc}")
        transcript.write("")

    async def _handle_history(self, arg: str, transcript: RichLog) -> None:
        """Show conversation history from backend."""
        try:
            messages = await self._gateway.get_session_history()
            if not messages:
                transcript.write("[dim]No conversation history yet.[/]")
            else:
                preview = max(80, int(arg) if arg.isdigit() else 400)
                transcript.write(f"[bold #FFD700]Conversation History ({len(messages)} messages)[/]")
                for i, msg in enumerate(messages):
                    role = msg.get("role", "?")
                    content = msg.get("content", "")
                    if not content:
                        continue
                    tag = "You" if role == "user" else "Hermes"
                    body = str(content)[:preview]
                    clipped = "…" if len(str(content)) > preview else ""
                    transcript.write(f"[bold]{tag} #{i + 1}:[/]")
                    transcript.write(f"  {body}{clipped}")
                transcript.write("")
        except Exception as exc:
            transcript.write(f"[bold red]History failed:[/] {exc}")
        transcript.write("")

    async def _handle_branch(self, name: str, transcript: RichLog) -> None:
        """Branch the current session."""
        try:
            result = await self._gateway.branch_session(name)
            new_sid = result.get("session_id", "")
            title = result.get("title", "")
            if new_sid:
                transcript.clear()
                self._session_id = new_sid
                transcript.write(f"[bold green]✓ Branched to new session[/]")
                if title:
                    transcript.write(f"[dim]Title:[/] {title}")
            else:
                transcript.write("[dim]Branch failed — no session returned.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Branch failed:[/] {exc}")
        transcript.write("")

    async def _handle_compress(self, arg: str, transcript: RichLog) -> None:
        """Compress conversation context."""
        try:
            result = await self._gateway.compress_session()
            removed = result.get("removed", 0)
            summary = result.get("summary", {}) or {}
            headline = summary.get("headline", "")
            if headline:
                transcript.write(f"[dim]{headline}[/]")
                token_line = summary.get("token_line", "")
                if token_line:
                    transcript.write(f"  [dim]{token_line}[/]")
            elif removed > 0:
                transcript.write(f"[dim]Compressed {removed} messages.[/]")
            else:
                transcript.write("[dim]Nothing to compress.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Compress failed:[/] {exc}")
        transcript.write("")

    async def _handle_background(self, arg: str, transcript: RichLog) -> None:
        """Run a prompt in the background."""
        if not arg:
            transcript.write("[dim]Usage: /background <prompt>[/]")
        else:
            try:
                result = await self._gateway.submit_background(arg)
                task_id = result.get("task_id", "") or result.get("id", "")
                if task_id:
                    transcript.write(f"[dim]Background task started:[/] {task_id}")
                else:
                    transcript.write("[dim]Background task submitted.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Background failed:[/] {exc}")
        transcript.write("")

    async def _handle_queue(self, arg: str, transcript: RichLog) -> None:
        """Queue a prompt for the next turn."""
        if arg:
            preview = arg[:50] + "…" if len(arg) > 50 else arg
            transcript.write(f"[dim]Queued:[/] {preview}")
            transcript.write(f"[dim]Your message will be sent when the current turn completes.[/]")
        else:
            transcript.write("[dim]Usage: /queue <prompt>[/]")
        transcript.write("")

    async def _handle_steer(self, arg: str, transcript: RichLog) -> None:
        """Inject a message after next tool call (no interrupt)."""
        if not arg:
            transcript.write("[dim]Usage: /steer <prompt>[/]")
        else:
            preview = arg[:50] + "…" if len(arg) > 50 else arg
            try:
                result = await self._gateway.steer_session(arg)
                status = result.get("status", "")
                if status == "queued":
                    transcript.write(f"[dim]Steer queued — arrives after next tool call: {preview}[/]")
                else:
                    # No agent running — submit as normal prompt (backend queues if busy)
                    transcript.write(f"[dim]Submitting as prompt: {preview}[/]")
                    await self._gateway.submit_prompt(arg)
            except Exception as exc:
                transcript.write(f"[bold red]Steer failed:[/] {exc}")
        transcript.write("")

    async def _handle_usage(self, transcript: RichLog) -> None:
        """Show token usage and session info."""
        try:
            result = await self._gateway.get_usage()
            if not result or not result.get("calls"):
                transcript.write("[dim]No API calls yet in this session.[/]")
            else:
                transcript.write(f"[bold #FFD700]Usage[/]")
                transcript.write(f"  [dim]Model:[/]        {result.get('model', '?')}")
                transcript.write(f"  [dim]Input tokens:[/]  {int(result.get('input', 0)):,}")
                transcript.write(f"  [dim]Output tokens:[/] {int(result.get('output', 0)):,}")
                transcript.write(f"  [dim]Total tokens:[/]  {int(result.get('total', 0)):,}")
                transcript.write(f"  [dim]API calls:[/]     {int(result.get('calls', 0)):,}")
        except Exception as exc:
            transcript.write(f"[bold red]Usage failed:[/] {exc}")
        transcript.write("")

    async def _handle_stop(self, transcript: RichLog) -> None:
        """Stop all background processes."""
        try:
            result = await self._gateway.stop_processes()
            killed = int(result.get("killed", 0))
            noun = "process" if killed == 1 else "processes"
            transcript.write(f"[dim]Stopped {killed} background {noun}.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Stop failed:[/] {exc}")
        transcript.write("")

    async def _handle_sessions(self, arg: str, transcript: RichLog) -> None:
        """Browse or resume sessions."""
        if arg and arg.lower() == "new":
            await self._handle_new_session("", transcript)
            return

        if arg:
            # Try to resume by session ID
            try:
                result = await self._gateway.resume_session(arg)
                if result.get("session_id"):
                    transcript.clear()
                    self._session_id = result.get("session_id", "")
                    self._info = result.get("info", {}) or {}
                    model = self._info.get("model") or result.get("model") or ""
                    provider = self._info.get("provider") or result.get("provider") or ""
                    if model:
                        self._model = model
                    if provider:
                        self._provider = provider
                    title = result.get("title", "")
                    if title:
                        transcript.write(f"[bold green]Resumed session:[/] {title}")
                    else:
                        transcript.write(f"[bold green]Resumed session[/]")
                    transcript.write(f"[dim]Session:[/] {self._session_id[:12]}")
                    if model:
                        transcript.write(f"[dim]Model:[/]   {model}")
                    transcript.write("")
                    return
            except Exception:
                pass
            transcript.write(f"[dim]Could not resume: {arg}. Use /sessions to browse.[/]")
        else:
            # List sessions
            try:
                all_sessions = await self._gateway.list_sessions()
                if not all_sessions:
                    transcript.write("[dim]No previous sessions.[/]")
                else:
                    transcript.write(f"[bold #FFD700]Sessions ({len(all_sessions)})[/]")
                    for s in all_sessions[:20]:
                        sid = (s.get("session_id") or s.get("id") or "?")[:12]
                        title = s.get("title", "") or ""
                        ts = s.get("created_at", "") or ""
                        if isinstance(ts, (int, float)):
                            from datetime import datetime as _dt
                            ts = _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                        label = f"[dim]{sid}[/]"
                        if title:
                            label += f" — {title}"
                        if ts:
                            label += f" [dim]({ts})[/]"
                        transcript.write(f"  {label}")
                    if len(all_sessions) > 20:
                        transcript.write(f"  [dim]... and {len(all_sessions) - 20} more[/]")
                transcript.write("")
            except Exception as exc:
                transcript.write(f"[bold red]Sessions list failed:[/] {exc}")

    async def _handle_copy(self, arg: str, transcript: RichLog) -> None:
        """Copy last assistant response to clipboard."""
        try:
            import pyperclip

            messages = await self._gateway.get_session_history()
            assistant_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]
            if not assistant_msgs:
                transcript.write("[dim]No assistant responses to copy.[/]")
            else:
                idx = int(arg) - 1 if arg and arg.lstrip("-").isdigit() else -1
                target = assistant_msgs[idx] if abs(idx) <= len(assistant_msgs) else assistant_msgs[-1]
                content = str(target.get("content", ""))
                pyperclip.copy(content)
                transcript.write(f"[dim]Copied {len(content)} characters to clipboard.[/]")
        except ImportError:
            # Fallback to OSC52 escape sequence
            try:
                messages = await self._gateway.get_session_history()
                assistant_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]
                if assistant_msgs:
                    content = str(assistant_msgs[-1].get("content", ""))
                    # Write OSC52 escape sequence
                    import sys
                    sys.stdout.write(f"\x1b]52;;{__import__('base64').standard_b64encode(content.encode()).decode()}\x07")
                    sys.stdout.flush()
                    transcript.write(f"[dim]Sent OSC52 clipboard sequence ({len(content)} chars).[/]")
                else:
                    transcript.write("[dim]No assistant responses to copy.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Clipboard copy failed:[/] {exc}")
        except Exception as exc:
            transcript.write(f"[bold red]Clipboard copy failed:[/] {exc}")
        transcript.write("")

    def _handle_fortune(self, arg: str, transcript: RichLog) -> None:
        """Show a local fortune."""
        import random

        fortunes = [
            "A clear conscience is usually the sign of a bad memory.",
            "A fresh start will put you on your way.",
            "A golden egg of opportunity falls into your lap this month.",
            "Advice is like snow — the softer it falls, the longer it dwells upon, and the deeper it sinks into the mind.",
            "All your hard work will soon pay off.",
            "An exciting opportunity lies ahead of you.",
            "Believe in yourself and others will too.",
            "Change is happening in your life, so go with the flow!",
            "Curiosity kills boredom. Nothing can kill curiosity.",
            "Don't be afraid of your fears. They're not there to scare you. They're there to let you know that something is worth it.",
            "Don't just think, act!",
            "Don't let your mind wander — it's too little to be let out alone.",
            "Every small step you take brings you closer to your goal.",
            "Fortune favors the bold.",
            "Good things come to those who wait, but only what's left by those who hustle.",
            "Happiness is an inside job.",
            "Keep your feet on the ground even though your friends look up to you.",
            "Now is the time to try something new.",
            "Someone will appreciate your effort today.",
            "The best time to start is now.",
            "The early bird gets the worm, but the second mouse gets the cheese.",
            "Your ability to juggle many tasks will help you today.",
            "Your heart is a place to draw true happiness.",
        ]
        key = arg.strip().lower()
        if not arg or key == "random":
            selected = random.choice(fortunes)
        elif key in ("daily", "today"):
            # Deterministic daily fortune based on session ID
            idx = hash(self._session_id or "default") % len(fortunes)
            selected = fortunes[idx]
        else:
            selected = random.choice(fortunes)
        transcript.write(f"[italic]{selected}[/]")
        transcript.write("")

    async def _handle_logs(self, arg: str, transcript: RichLog) -> None:
        """View gateway logs."""
        import os
        from hermes_constants import get_hermes_home

        log_dir = get_hermes_home() / "logs"
        n = max(1, min(80, int(arg) if arg and arg.isdigit() else 20))
        try:
            log_file = log_dir / "gateway.log"
            if log_file.exists():
                with open(log_file) as f:
                    lines = f.readlines()
                tail = lines[-n:]
                transcript.write(f"[bold #FFD700]Gateway Logs (last {len(tail)} lines)[/]")
                for line in tail:
                    transcript.write(f"  [dim]{line.rstrip()}[/]")
            else:
                transcript.write(f"[dim]No gateway log found at {log_file}.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Logs failed:[/] {exc}")
        transcript.write("")

    def _handle_update(self, transcript: RichLog) -> None:
        """Exit the TUI and trigger update."""
        transcript.write("[bold yellow]Exiting TUI to run update...[/]")
        # Exit code 42 signals the launcher to exec `hermes update`
        import sys

        sys.exit(42)

    async def _handle_prompt(self, arg: str, transcript: RichLog) -> None:
        """Open an editor to compose a prompt."""
        import os
        import subprocess
        import tempfile

        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))
        initial = arg or ""
        try:
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as f:
                if initial:
                    f.write(initial)
                    if not initial.endswith("\n"):
                        f.write("\n")
                f.flush()
                tmp_path = f.name

            subprocess.run([editor, tmp_path])

            with open(tmp_path) as f:
                content = f.read().strip()

            os.unlink(tmp_path)

            if content:
                # Submit as a normal prompt
                transcript.write(f"[dim]Composed prompt ({len(content)} chars).[/]")
                await self._gateway.submit_prompt(content)
            else:
                transcript.write("[dim]Empty prompt — cancelled.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Editor failed:[/] {exc}")
        transcript.write("")


    async def _handle_voice(self, arg: str, transcript: RichLog) -> None:
        """Toggle voice mode: [on|off|tts|status]."""
        action = arg.strip().lower() if arg.strip() in ("on", "off", "tts", "status") else "status"
        try:
            result = await self._gateway.voice_toggle(action)
            enabled = result.get("enabled", False)
            tts = result.get("tts", False)
            record_key = result.get("record_key", "ctrl+b")
            if action == "status":
                transcript.write(f"[bold #FFD700]Voice Mode Status[/]")
                transcript.write(f"  [dim]Mode:[/]       {'ON' if enabled else 'OFF'}")
                transcript.write(f"  [dim]TTS:[/]        {'ON' if tts else 'OFF'}")
                transcript.write(f"  [dim]Record key:[/] {record_key}")
            elif action == "tts":
                transcript.write(f"[dim]Voice TTS {'enabled' if tts else 'disabled'}.[/]")
            else:
                if enabled:
                    transcript.write(f"[dim]Voice mode enabled ({record_key} to start/stop recording).[/]")
                else:
                    transcript.write("[dim]Voice mode disabled.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Voice failed:[/] {exc}")
        transcript.write("")

    async def _handle_tools(self, arg: str, transcript: RichLog) -> None:
        """Enable/disable/list tools."""
        parts = arg.strip().split()
        sub = parts[0].lower() if parts else "list"
        if sub in ("enable", "disable") and len(parts) > 1:
            names = parts[1:]
            try:
                result = await self._gateway.tools_configure(sub, names)
                changed = result.get("changed", [])
                unknown = result.get("unknown", [])
                if changed:
                    transcript.write(f"[dim]{sub}d:[/] {', '.join(changed)}")
                if unknown:
                    transcript.write(f"[dim]Unknown:[/] {', '.join(unknown)}")
            except Exception as exc:
                transcript.write(f"[bold red]Tools failed:[/] {exc}")
        else:
            # List available tools via slash.exec (complex output)
            try:
                result = await self._gateway.execute_slash("tools", arg)
                output = (result.get("output") or result.get("text") or "").strip()
                if output:
                    transcript.write(output)
                else:
                    transcript.write("[dim]No tools output. Try /tools list.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Tools failed:[/] {exc}")
        transcript.write("")

    async def _handle_skills(self, arg: str, transcript: RichLog) -> None:
        """Search, install, inspect, or list skills."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        query = parts[1] if len(parts) > 1 else ""

        if sub in ("list", "inspect", "search", "install", "browse"):
            try:
                result = await self._gateway.skills_manage(sub, query)
                if sub == "list":
                    skills = result.get("skills", {})
                    cats = sorted(skills.items())
                    transcript.write(f"[bold #FFD700]Skills[/]")
                    for cat_name, items in cats:
                        transcript.write(f"  [bold]{cat_name}[/]: {', '.join(items[:10])}")
                        if len(items) > 10:
                            transcript.write(f"    [dim]... {len(items) - 10} more[/]")
                elif sub == "inspect":
                    info = result.get("info", {})
                    if info.get("name"):
                        transcript.write(f"[bold]Skill:[/] {info['name']}")
                        if info.get("category"):
                            transcript.write(f"  [dim]Category:[/] {info['category']}")
                        if info.get("description"):
                            transcript.write(f"  [dim]Description:[/] {info['description']}")
                    else:
                        transcript.write(f"[dim]Unknown skill: {query}[/]")
                elif sub == "search":
                    results = result.get("results", [])
                    if results:
                        transcript.write(f"[bold #FFD700]Search: {query}[/]")
                        for r in results[:10]:
                            transcript.write(f"  [bold]{r.get('name', '?')}[/] — {r.get('description', '')[:80]}")
                    else:
                        transcript.write(f"[dim]No results for: {query}[/]")
                elif sub == "install":
                    if result.get("installed"):
                        transcript.write(f"[bold green]✓ Installed: {result.get('name', query)}[/]")
                    else:
                        transcript.write("[dim]Install failed.[/]")
                elif sub == "browse":
                    items = result.get("items", [])
                    if items:
                        transcript.write(f"[bold #FFD700]Browse Skills[/]")
                        for s in items[:10]:
                            transcript.write(f"  {s.get('name', '?')} — {str(s.get('description', ''))[:80]}")
                    else:
                        transcript.write("[dim]No skills found.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Skills failed:[/] {exc}")
        else:
            # Fall through to slash.exec for unrecognized subcommands
            try:
                result = await self._gateway.execute_slash("skills", arg)
                output = (result.get("output") or "").strip()
                if output:
                    transcript.write(output)
            except Exception:
                pass
        transcript.write("")

    async def _handle_cron(self, arg: str, transcript: RichLog) -> None:
        """Manage scheduled tasks."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        name = parts[1] if len(parts) > 1 else ""
        try:
            result = await self._gateway.cron_manage(sub, name)
            output = result.get("output") or result.get("text") or ""
            if output:
                transcript.write(str(output))
            else:
                transcript.write(f"[dim]Cron {sub} completed.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Cron failed:[/] {exc}")
        transcript.write("")

    async def _handle_reload(self, transcript: RichLog) -> None:
        """Re-read .env into the running session."""
        try:
            result = await self._gateway.reload_env()
            updated = int(result.get("updated", 0))
            noun = "var" if updated == 1 else "vars"
            transcript.write(f"[dim]Reloaded .env ({updated} {noun} updated).[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Reload failed:[/] {exc}")
        transcript.write("")

    async def _handle_reload_mcp(self, arg: str, transcript: RichLog) -> None:
        """Reload MCP servers."""
        a = arg.strip().lower()
        now = a in ("now", "approve", "once", "yes")
        always = a == "always"
        try:
            result = await self._gateway.reload_mcp(confirm=now, always=always)
            status = result.get("status", "")
            if status == "confirm_required":
                transcript.write(f"[dim]{result.get('message', 'Reload MCP requires confirmation. Use /reload-mcp now to confirm.')}[/]")
            elif status == "reloaded":
                transcript.write("[dim]MCP servers reloaded.[/]")
            else:
                transcript.write("[dim]Reload complete.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]MCP reload failed:[/] {exc}")
        transcript.write("")

    async def _handle_reload_skills(self, transcript: RichLog) -> None:
        """Re-scan installed skills."""
        try:
            result = await self._gateway.reload_skills()
            output = result.get("output") or ""
            if output:
                transcript.write(str(output))
            else:
                transcript.write("[dim]Skills reloaded.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Skills reload failed:[/] {exc}")
        transcript.write("")

    async def _handle_browser(self, arg: str, transcript: RichLog) -> None:
        """Manage browser CDP connection."""
        parts = arg.strip().split(None, 1)
        action = parts[0].lower() if parts else "status"
        url = parts[1] if len(parts) > 1 else ""
        if action not in ("connect", "disconnect", "status"):
            transcript.write("[dim]Usage: /browser [connect|disconnect|status] [url][/]")
        else:
            try:
                if action == "connect":
                    url = url or "http://127.0.0.1:9222"
                    transcript.write(f"[dim]Checking browser at {url}...[/]")
                result = await self._gateway.browser_manage(action, url)
                if action == "status":
                    if result.get("connected"):
                        transcript.write(f"[dim]Browser connected: {result.get('url', '(url unavailable)')}[/]")
                    else:
                        transcript.write("[dim]Browser not connected. Use /browser connect <url>[/]")
                elif action == "disconnect":
                    transcript.write("[dim]Browser disconnected.[/]")
                elif result.get("connected"):
                    transcript.write(f"[dim]Browser connected to live Chromium at {url}[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Browser failed:[/] {exc}")
        transcript.write("")

    async def _handle_plugins(self, transcript: RichLog) -> None:
        """List installed plugins."""
        try:
            plugins = await self._gateway.plugins_list()
            if not plugins:
                transcript.write("[dim]No user plugins installed.[/]")
            else:
                transcript.write(f"[bold #FFD700]Plugins ({len(plugins)})[/]")
                for p in plugins:
                    name = p.get("name", "?")
                    state = p.get("state", "")
                    glyph = "✓" if state == "enabled" else "✗"
                    ver = p.get("version", "")
                    detail = f" v{ver}" if ver else ""
                    transcript.write(f"  {glyph} {name}{detail} [{state}]")
        except Exception as exc:
            transcript.write(f"[bold red]Plugins failed:[/] {exc}")
        transcript.write("")

    async def _handle_image(self, arg: str, transcript: RichLog) -> None:
        """Attach a local image file."""
        if not arg:
            transcript.write("[dim]Usage: /image <path>[/]")
        else:
            try:
                result = await self._gateway.attach_image(arg)
                if result.get("ok") or result.get("remainder") is not None:
                    transcript.write(f"[dim]Image attached: {arg}[/]")
                else:
                    transcript.write(f"[dim]Image queued: {arg}[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Image attach failed:[/] {exc}")
        transcript.write("")

    async def _handle_rollback(self, arg: str, transcript: RichLog) -> None:
        """List, restore, or diff checkpoints."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        target = parts[1] if len(parts) > 1 else ""
        if sub in ("list", "ls") or not sub:
            try:
                result = await self._gateway.rollback_list()
                checkpoints = result.get("checkpoints", [])
                if not result.get("enabled"):
                    transcript.write("[dim]Checkpoints are not enabled.[/]")
                elif not checkpoints:
                    transcript.write("[dim]No checkpoints found.[/]")
                else:
                    transcript.write(f"[bold #FFD700]Checkpoints ({len(checkpoints)})[/]")
                    for i, cp in enumerate(checkpoints[:10], 1):
                        h = cp.get("hash", "?")[:10]
                        ts = cp.get("timestamp", "")
                        msg = cp.get("message", "")
                        label = f"{ts} — {msg}" if ts and msg else ts or msg or "(no metadata)"
                        transcript.write(f"  {i}. {h}  {label}")
            except Exception as exc:
                transcript.write(f"[bold red]Rollback failed:[/] {exc}")
        elif sub == "diff":
            try:
                result = await self._gateway.rollback_diff(target)
                diff = result.get("rendered") or result.get("diff") or ""
                stat = result.get("stat", "")
                if stat:
                    transcript.write(f"[dim]{stat}[/]")
                if diff:
                    for line in diff.split("\n")[:30]:
                        transcript.write(f"  [dim]{line}[/]")
                else:
                    transcript.write("[dim]No changes since this checkpoint.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Rollback diff failed:[/] {exc}")
        else:
            # hash — restore
            try:
                result = await self._gateway.rollback_restore(sub)
                if result.get("success"):
                    detail = result.get("reason") or result.get("message") or "restored"
                    transcript.write(f"[dim]Checkpoint restored: {detail}[/]")
                else:
                    transcript.write(f"[dim]Rollback failed: {result.get('error', 'unknown error')}[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Rollback restore failed:[/] {exc}")
        transcript.write("")


    async def _handle_agents(self, arg: str, transcript: RichLog) -> None:
        """Show active agents and running tasks."""
        try:
            result = await self._gateway.get_agents()
            if not result:
                transcript.write("[dim]No active agents or running tasks.[/]")
            else:
                transcript.write(f"[bold #FFD700]Active Agents ({len(result)})[/]")
                for a in result[:10]:
                    name = a.get("name") or a.get("task_id") or "?"
                    status = a.get("status", "running")
                    model = a.get("model", "")
                    detail = f" ({model})" if model else ""
                    transcript.write(f"  {name:<20s} [{status}]{detail}")
        except Exception as exc:
            transcript.write(f"[bold red]Agents failed:[/] {exc}")
        transcript.write("")

    async def _handle_credits(self, transcript: RichLog) -> None:
        """Show Nous credit balance and top up."""
        try:
            result = await self._gateway.get_credits()
            if result.get("logged_in") or result.get("balance_lines"):
                transcript.write(f"[bold #FFD700]Nous Credits[/]")
                for line in (result.get("balance_lines") or []):
                    transcript.write(f"  {line}")
                identity = result.get("identity_line", "")
                if identity:
                    transcript.write(f"  [dim]{identity}[/]")
                topup = result.get("topup_url", "")
                if topup:
                    transcript.write(f"  Top up: {topup}")
            else:
                transcript.write("[dim]Not logged into Nous credits. Balance info unavailable.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Credits failed:[/] {exc}")
        transcript.write("")

    async def _handle_billing(self, transcript: RichLog) -> None:
        """Show billing information."""
        try:
            result = await self._gateway.get_usage()
            if result:
                transcript.write(f"[bold #FFD700]Billing[/]")
                credits_lines = result.get("credits_lines", [])
                for line in credits_lines:
                    transcript.write(f"  {line}")
                if not credits_lines:
                    transcript.write("[dim]No billing information available.[/]")
            else:
                transcript.write("[dim]Billing unavailable.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Billing failed:[/] {exc}")
        transcript.write("")

    async def _handle_insights(self, arg: str, transcript: RichLog) -> None:
        """Show usage insights and analytics."""
        try:
            days = int(arg) if arg and arg.isdigit() else 30
            result = await self._gateway.get_insights(days)
            output = result.get("output") or result.get("text") or ""
            if output:
                transcript.write(str(output))
            else:
                transcript.write("[dim]No insights available for the selected period.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Insights failed:[/] {exc}")
        transcript.write("")

    async def _handle_journey(self, arg: str, transcript: RichLog) -> None:
        """Show learning journey timeline."""
        parts = arg.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        target = parts[1] if len(parts) > 1 else ""
        try:
            if sub in ("list", "ls") or not sub:
                frames = await self._gateway.get_learning_frames()
                nodes = frames.get("nodes", []) or frames.get("frames", [])
                if nodes:
                    transcript.write(f"[bold #FFD700]Learning Journey ({len(nodes)})[/]")
                    for node in nodes[:10]:
                        title = node.get("title") or node.get("name") or "?"
                        kind = node.get("type", node.get("kind", ""))
                        date = node.get("date", node.get("created_at", ""))[:10] if node.get("date", node.get("created_at", "")) else ""
                        tag = f"[{kind}]" if kind else ""
                        transcript.write(f"  {tag:<10s} {title:<35s} {date}")
                else:
                    transcript.write("[dim]No journey entries yet.[/]")
            elif sub == "delete":
                if not target:
                    transcript.write("[dim]Usage: /journey delete <id>[/]")
                else:
                    await self._gateway.learning_delete(target)
                    transcript.write(f"[dim]Journey entry {target} deleted.[/]")
            elif sub == "detail" or sub == "edit":
                if not target:
                    transcript.write(f"[dim]Usage: /journey {sub} <id>[/]")
                elif sub == "detail":
                    detail = await self._gateway.get_learning_detail(target)
                    content = detail.get("content", "")
                    if content:
                        transcript.write(str(content))
                else:
                    transcript.write(f"[dim]Edit not supported in TUI. Use slash.exec.[/]")
            else:
                transcript.write(f"[dim]Usage: /journey [list|delete <id>|detail <id>][/]")
        except Exception as exc:
            transcript.write(f"[bold red]Journey failed:[/] {exc}")
        transcript.write("")

    async def _handle_config(self, transcript: RichLog) -> None:
        """Show current configuration."""
        try:
            result = await self._gateway.show_config()
            output = result.get("output") or result.get("text") or ""
            if output:
                transcript.write(str(output))
            else:
                transcript.write("[dim]Configuration unavailable.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Config failed:[/] {exc}")
        transcript.write("")

    async def _handle_toolsets(self, transcript: RichLog) -> None:
        """List available toolsets."""
        try:
            result = await self._gateway.get_toolsets()
            toolsets = result.get("toolsets", [])
            if not toolsets:
                transcript.write("[dim]No toolsets available.[/]")
            else:
                transcript.write(f"[bold #FFD700]Toolsets ({len(toolsets)})[/]")
                for ts in toolsets:
                    name = ts.get("name", ts.get("key", "?")) if isinstance(ts, dict) else str(ts)
                    desc = ts.get("description", "") if isinstance(ts, dict) else ""
                    line = f"  {name}"
                    if desc:
                        line += f" — {desc}"
                    transcript.write(line)
        except Exception as exc:
            transcript.write(f"[bold red]Toolsets failed:[/] {exc}")
        transcript.write("")

    async def _handle_paste(self, transcript: RichLog) -> None:
        """Attach clipboard image."""
        try:
            result = await self._gateway.paste_clipboard()
            if result.get("ok") or result.get("status") == "ok":
                transcript.write("[dim]Clipboard image attached.[/]")
            else:
                transcript.write("[dim]No image found in clipboard.[/]")
        except Exception as exc:
            transcript.write(f"[bold red]Paste failed:[/] {exc}")
        transcript.write("")

    async def _handle_handoff(self, arg: str, transcript: RichLog) -> None:
        """Hand off session to a messaging platform."""
        if not arg:
            transcript.write("[dim]Usage: /handoff <platform> (e.g. telegram, discord)[/]")
        else:
            try:
                result = await self._gateway.handoff_request(arg)
                status = result.get("status", result.get("state", ""))
                if status:
                    transcript.write(f"[dim]Handoff initiated: {status}[/]")
                else:
                    transcript.write(f"[dim]Handoff requested to {arg}.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Handoff failed:[/] {exc}")
        transcript.write("")

    def _handle_version(self, transcript: RichLog) -> None:
        """Show Hermes Agent version."""
        try:
            from hermes_cli.main import _print_version_info
        except ImportError:
            transcript.write("[dim]Hermes Agent[/]")
        else:
            try:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _print_version_info(check_updates=False)
                version_text = buf.getvalue().strip()
                if version_text:
                    transcript.write(version_text)
                else:
                    transcript.write("[dim]Hermes Agent[/]")
            except Exception:
                transcript.write("[dim]Hermes Agent[/]")
        transcript.write("")



    async def _handle_pet(self, arg: str, transcript: RichLog) -> None:
        """Toggle or list pets."""
        sub = arg.strip().lower()
        if sub == "list":
            try:
                result = await self._gateway.pet_gallery()
                pets = result.get("pets", [])
                if pets:
                    transcript.write(f"[bold #FFD700]Available Pets ({len(pets)})[/]")
                    for p in pets[:20]:
                        name = p.get("name", "?")
                        slug = p.get("slug", "")
                        active = " ✓" if p.get("active") else ""
                        transcript.write(f"  {slug:<20s} {name}{active}")
                else:
                    transcript.write("[dim]No pets available.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Pet list failed:[/] {exc}")
        elif sub:
            # Adopt a specific pet by slug
            try:
                result = await self._gateway.pet_select(sub)
                if result.get("ok") or result.get("active") == sub:
                    transcript.write(f"[bold green]✓ Pet activated: {sub}[/]")
                else:
                    transcript.write(f"[dim]Could not adopt pet: {sub}[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Pet select failed:[/] {exc}")
        else:
            # Toggle via config.set
            try:
                info = await self._gateway.pet_info()
                enabled = info.get("enabled", False)
                if enabled:
                    await self._gateway.set_config("pet.enabled", "false")
                    transcript.write("[dim]Pet disabled.[/]")
                else:
                    await self._gateway.set_config("pet.enabled", "true")
                    transcript.write("[dim]Pet enabled.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Pet toggle failed:[/] {exc}")
        transcript.write("")

    async def _handle_hatch(self, arg: str, transcript: RichLog) -> None:
        """Generate a new pet from a description."""
        if not arg:
            transcript.write("[dim]Usage: /hatch <description>[/]")
            transcript.write("[dim]Example: /hatch a tiny dragon with crystal wings[/]")
        else:
            try:
                result = await self._gateway.pet_hatch(arg)
                if result.get("ok") or result.get("slug"):
                    slug = result.get("slug", "?")
                    transcript.write(f"[bold green]✓ Pet hatched! Adopt with /pet {slug}[/]")
                else:
                    transcript.write("[dim]Hatch failed.[/]")
            except Exception as exc:
                transcript.write(f"[bold red]Hatch failed:[/] {exc}")
        transcript.write("")

    async def _handle_resume(self, arg: str, transcript: RichLog) -> None:
        """Resume a previously-named session."""
        if not arg:
            transcript.write("[dim]Usage: /resume <session-id>[/]")
            transcript.write("[dim]Tip: Use /sessions to list available sessions.[/]")
        else:
            await self._handle_sessions(arg, transcript)

    async def _handle_settings(self, transcript: RichLog) -> None:
        """Open the settings screen."""
        from tui_textual.screens.settings import SettingsScreen
        await self.app.push_screen_wait(SettingsScreen(self._gateway))
        transcript.write("[dim]Settings updated.[/]")
        transcript.write("")

    def _show_help(self, transcript: RichLog) -> None:
        """Display categorized help with all known commands."""
        transcript.write("[bold underline #FFD700]Available Commands[/]")

        # Show from our loaded catalog
        if self._command_categories:
            for cat in self._command_categories:
                name = cat.get("name", "")
                pairs = cat.get("pairs", [])
                if not pairs:
                    continue
                transcript.write(f"")
                transcript.write(f"[bold]{name}[/]")
                for p in pairs[:8]:
                    cmd_name = p[0] if isinstance(p, list) else p.get("name", "")
                    desc = p[1] if isinstance(p, list) else p.get("description", "")
                    transcript.write(f"  {cmd_name:<25s} {desc[:60]}")
                if len(pairs) > 8:
                    transcript.write(f"  [dim]... {len(pairs) - 8} more[/]")
        else:
            # Fallback: hardcoded essentials
            transcript.write("")
            transcript.write("[bold]Session[/]")
            transcript.write("  /help                  Show this help")
            transcript.write("  /new                   Start a new session")
            transcript.write("  /clear                 Clear screen + new session")
            transcript.write("  /undo                  Undo last turn")
            transcript.write("  /retry                 Retry last prompt")
            transcript.write("  /save                  Save conversation")
            transcript.write("  /history               Show history")
            transcript.write("  /model                 Switch model")
            transcript.write("")
            transcript.write("[bold]Tools & Skills[/]")
            transcript.write("  /toolsets              List available toolsets")
            transcript.write("  /tools                 List available tools")
            transcript.write("  /skills                List installed skills")
            transcript.write("")
            transcript.write("[bold]Exit[/]")
            transcript.write("  /quit                  Exit the TUI")
        transcript.write("")
        transcript.write("[dim]Type a message to chat with the agent.[/]")
        transcript.write("")

    # ── Event handlers ────────────────────────────────────────────

    def _on_gateway_ready(self, _event: str, params: dict[str, Any]) -> None:
        """Backend gateway has fully initialized."""
        pass

    def _on_message_delta(self, _event: str, params: dict[str, Any]) -> None:
        """Accumulate streaming response delta."""
        delta = params.get("delta", "")
        if delta:
            self._current_response += delta

    def _on_message_complete(self, _event: str, params: dict[str, Any]) -> None:
        """Full response received — display it."""
        content = self._current_response or params.get("content", "")
        if content:
            self._display_message({"role": "assistant", "content": content})
        self._current_response = ""

    def _on_tool_start(self, _event: str, params: dict[str, Any]) -> None:
        """Tool execution started."""
        name = params.get("name", "tool")
        t = self.query_one("#transcript", RichLog)
        t.write(f"[yellow]⚙ {name}...[/]")

    def _on_tool_complete(self, _event: str, params: dict[str, Any]) -> None:
        """Tool execution completed."""
        name = params.get("name", "tool")
        summary = params.get("summary") or str(params.get("result", ""))[:80]
        if summary:
            t = self.query_one("#transcript", RichLog)
            t.write(f"[dim]┊ {name}: {summary}[/]")

    def _on_session_info(self, _event: str, params: dict[str, Any]) -> None:
        """Session info updated by the backend (model switch, etc.)."""
        if "session_id" in params:
            self._session_id = params["session_id"]
        payload = params.get("payload") or {}
        info = payload if isinstance(payload, dict) else {}
        if info.get("model"):
            self._model = info["model"]
        if info.get("provider"):
            self._provider = info["provider"]

    def _on_status_update(self, _event: str, params: dict[str, Any]) -> None:
        """Status update from gateway (thinking, activity)."""
        kind = params.get("kind", "")
        text = params.get("text", "")
        if kind == "thinking" and text:
            t = self.query_one("#transcript", RichLog)
            t.write(f"[dim]{text}[/]")

    def _on_approval_request(self, _event: str, params: dict[str, Any]) -> None:
        """Dangerous command approval request."""
        from tui_textual.screens.approval import ApprovalDialog

        self.app.push_screen(
            ApprovalDialog(
                request_id=params.get("request_id", ""),
                command=params.get("command", ""),
                preview=params.get("preview", ""),
            ),
            callback=lambda action: self._handle_approval(
                params.get("request_id", ""), action or "deny"
            ),
        )

    def _on_sudo_request(self, _event: str, params: dict[str, Any]) -> None:
        """Sudo password request."""
        from tui_textual.screens.input import InputDialog

        self.app.push_screen(
            InputDialog("Sudo password required:", password=True),
            callback=lambda pw: self._handle_sudo(
                params.get("request_id", ""), pw
            ),
        )

    def _on_secret_request(self, _event: str, params: dict[str, Any]) -> None:
        """Secret value request."""
        from tui_textual.screens.input import InputDialog

        key = params.get("key", "secret")
        self.app.push_screen(
            InputDialog(f"Enter {key}:", password=True),
            callback=lambda val: self._handle_secret(
                params.get("request_id", ""), val
            ),
        )

    async def _handle_approval(self, request_id: str, action: str) -> None:
        await self._gateway.respond_approval(request_id, action)

    async def _handle_sudo(self, request_id: str, password: str) -> None:
        if password:
            await self._gateway.respond_sudo(request_id, password)

    async def _handle_secret(self, request_id: str, value: str) -> None:
        if value:
            await self._gateway.respond_secret(request_id, value)

    # ── Display helpers ───────────────────────────────────────────

    def _display_message(self, msg: dict[str, Any]) -> None:
        """Format and write a message to the transcript."""
        transcript = self.query_one("#transcript", RichLog)
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if not content:
            return

        if role == "user":
            transcript.write(f"[bold blue]You:[/] {content}")
        elif role == "assistant":
            transcript.write(f"[bold green]Hermes:[/] {content}")
        elif role == "system":
            transcript.write(f"[dim]{content}[/]")
        else:
            transcript.write(f"[dim]{role}: {content}[/]")
        transcript.write("")

    # ── Actions ───────────────────────────────────────────────────

    def action_clear_screen(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def action_focus_input(self) -> None:
        self.set_focus(self.query_one("#composer-input"))

    async def action_open_settings(self) -> None:
        """Open settings via Ctrl+S binding."""
        from tui_textual.screens.settings import SettingsScreen
        await self.app.push_screen_wait(SettingsScreen(self._gateway))
        t = self.query_one("#transcript", RichLog)
        t.write("[dim]Settings updated.[/]")
        t.write("")
