"""
Gateway client — high-level interface between Textual TUI and the tui_gateway backend.

Wraps the JSON-RPC transport with typed methods for session management,
prompt submission, and event handling.
"""

from __future__ import annotations

from typing import Any


class GatewayClient:
    """Manages a session on the tui_gateway via JSON-RPC over stdio.

    Usage:
        1. ``await client.connect()`` — starts the read loop
        2. ``await client.create_session(...)`` — returns a session_id
        3. ``await client.submit_prompt(session_id, text)`` — sends user input
        4. Register handlers via ``client.on("message.delta", handler)``
        5. ``await client.close()`` — when done
    """

    def __init__(self, transport: "StdioTransport") -> None:  # type: ignore[name-defined]
        from tui_textual.transport import StdioTransport

        self._transport: StdioTransport = transport
        self._session_id: str = ""

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the transport read loop."""
        await self._transport.connect()

    async def close(self) -> None:
        """Close the transport."""
        await self._transport.close()

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── Event subscriptions ────────────────────────────────────────────

    def on(self, event_type: str, handler):
        """Register a handler for a server event.

        Returns a deregister callable.
        """
        return self._transport.on(event_type, handler)

    # ── Session management ────────────────────────────────────────────

    async def create_session(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        toolsets: list[str] | None = None,
        resume: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Create (or resume) a new gateway session.

        Returns session info dict with ``session_id``.
        """
        params: dict[str, Any] = {}
        if model:
            params["model"] = model
        if provider:
            params["provider"] = provider
        if toolsets:
            params["toolsets"] = toolsets
        if resume:
            params["resume"] = resume
        if cwd:
            params["cwd"] = cwd
        result = await self._transport.request("session.create", params, timeout=60.0)
        if "session_id" in result:
            self._session_id = str(result["session_id"])
        return result

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List stored sessions on the backend."""
        result = await self._transport.request("session.list")
        return result.get("sessions", [])

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        """Resume an existing session by ID."""
        result = await self._transport.request("session.resume", {"session_id": session_id}, timeout=60.0)
        if "session_id" in result:
            self._session_id = str(result["session_id"])
        return result

    async def close_session(self) -> dict[str, Any]:
        """Close the current session."""
        if not self._session_id:
            return {}
        result = await self._transport.request("session.close", {"session_id": self._session_id})
        self._session_id = ""
        return result

    async def get_session_history(self) -> list[dict[str, Any]]:
        """Get the message history for the current session."""
        if not self._session_id:
            return []
        result = await self._transport.request("session.history", {"session_id": self._session_id})
        return result.get("messages", [])

    async def get_session_status(self) -> dict[str, Any]:
        """Get live session status."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.status", {"session_id": self._session_id})

    async def get_config(self, key: str) -> Any:
        """Read a config.yaml value from the backend."""
        result = await self._transport.request("config.get", {"key": key})
        return result.get("value")

    async def set_config(self, key: str, value: Any) -> dict[str, Any]:
        """Write a config.yaml value on the backend.

        For the ``model`` key, this triggers a live model switch via
        ``_apply_model_switch``.  Returns the result dict.
        """
        params: dict[str, Any] = {"key": key, "value": value}
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("config.set", params, timeout=30.0)

    async def get_commands_catalog(self) -> dict[str, Any]:
        """Get the slash-command catalog.
        Returns the full catalog dict with keys: pairs, categories, sub, canon, skill_count.
        """
        return await self._transport.request("commands.catalog")

    # ── Prompt / conversation ─────────────────────────────────────────

    async def submit_prompt(self, text: str, *, images: list[str] | None = None) -> dict[str, Any]:
        """Submit a user prompt to the current session.

        Returns immediately with ``{"status": "streaming"}``.
        The response arrives as ``message.delta`` / ``message.complete`` events.
        """
        if not self._session_id:
            return {"status": "error", "error": "no active session"}
        params: dict[str, Any] = {"session_id": self._session_id, "text": text}
        if images:
            params["images"] = images
        return await self._transport.request("prompt.submit", params, timeout=10.0)

    async def interrupt_session(self) -> dict[str, Any]:
        """Interrupt the currently running turn."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.interrupt", {"session_id": self._session_id})

    async def compress_session(self) -> dict[str, Any]:
        """Trigger context compression for the current session."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.compress", {"session_id": self._session_id}, timeout=120.0)

    # ── Approval / interactive prompts ────────────────────────────────

    async def respond_approval(self, request_id: str, action: str, scope: str = "once") -> dict[str, Any]:
        """Respond to an approval request."""
        return await self._transport.request(
            "approval.respond",
            {"request_id": request_id, "action": action, "scope": scope},
        )

    async def respond_clarify(self, request_id: str, answer: str) -> dict[str, Any]:
        """Answer a clarification request."""
        return await self._transport.request("clarify.respond", {"request_id": request_id, "answer": answer})

    async def respond_sudo(self, request_id: str, password: str) -> dict[str, Any]:
        """Provide a sudo password."""
        return await self._transport.request("sudo.respond", {"request_id": request_id, "password": password})

    async def respond_secret(self, request_id: str, value: str) -> dict[str, Any]:
        """Provide a secret value."""
        return await self._transport.request("secret.respond", {"request_id": request_id, "value": value})

    # ── Slash commands ────────────────────────────────────────────────

    async def execute_slash(self, command: str, arg: str = "") -> dict[str, Any]:
        """Execute a slash command via the slash worker subprocess."""
        if not self._session_id:
            return {}
        full_text = f"/{command} {arg}".strip()
        return await self._transport.request(
            "slash.exec",
            {"session_id": self._session_id, "command": full_text},
            timeout=120.0,
        )

    async def dispatch_command(self, name: str, arg: str = "") -> dict[str, Any]:
        """Dispatch a command via command.dispatch (for skill/quick commands)."""
        params: dict[str, Any] = {"name": name}
        if arg:
            params["arg"] = arg
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("command.dispatch", params, timeout=60.0)

    async def get_slash_completions(self, text: str) -> list[dict[str, Any]]:
        """Get slash-command completions."""
        result = await self._transport.request("complete.slash", {"text": text})
        return result.get("completions", [])

    # ── Model management ────────────────────────────────────────────

    async def get_model_options(self) -> dict[str, Any]:
        """Get available providers and models."""
        params: dict[str, Any] = {}
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("model.options", params, timeout=30.0)

    async def save_model_key(self, slug: str, api_key: str) -> dict[str, Any]:
        """Save an API key for a provider and return refreshed model list."""
        params: dict[str, Any] = {"slug": slug, "api_key": api_key}
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("model.save_key", params, timeout=30.0)

    async def disconnect_model(self, slug: str) -> dict[str, Any]:
        """Remove credentials for a provider."""
        params: dict[str, Any] = {"slug": slug}
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("model.disconnect", params, timeout=30.0)

    # ── Session operations ───────────────────────────────────────────

    async def set_session_title(self, title: str) -> dict[str, Any]:
        """Set the session title."""
        if not self._session_id:
            return {}
        return await self._transport.request(
            "session.title", {"session_id": self._session_id, "title": title}
        )

    async def get_session_title(self) -> str:
        """Get the current session title."""
        if not self._session_id:
            return ""
        result = await self._transport.request("session.title", {"session_id": self._session_id})
        return str(result.get("title", ""))

    async def save_session(self) -> dict[str, Any]:
        """Save the current conversation."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.save", {"session_id": self._session_id}, timeout=30.0)

    async def undo_session(self) -> dict[str, Any]:
        """Undo last exchange."""
        if not self._session_id:
            return {"removed": 0}
        return await self._transport.request("session.undo", {"session_id": self._session_id})

    async def steer_session(self, text: str) -> dict[str, Any]:
        """Inject a message after next tool call (no interrupt)."""
        if not self._session_id:
            return {"status": "rejected"}
        return await self._transport.request(
            "session.steer", {"session_id": self._session_id, "text": text}
        )

    async def branch_session(self, name: str = "") -> dict[str, Any]:
        """Branch the current session."""
        if not self._session_id:
            return {}
        params: dict[str, Any] = {"session_id": self._session_id}
        if name:
            params["name"] = name
        return await self._transport.request("session.branch", params, timeout=30.0)

    async def get_session_status(self) -> dict[str, Any]:
        """Get live session status."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.status", {"session_id": self._session_id})

    # ── Process / tools / skills ────────────────────────────────────

    async def stop_processes(self) -> dict[str, Any]:
        """Kill all running background processes."""
        return await self._transport.request("process.stop")

    async def submit_background(self, text: str) -> dict[str, Any]:
        """Submit a prompt to run in the background."""
        if not self._session_id:
            return {}
        return await self._transport.request(
            "prompt.background", {"session_id": self._session_id, "text": text}
        )

    async def attach_image(self, path: str) -> dict[str, Any]:
        """Attach a local image file to the session."""
        if not self._session_id:
            return {}
        return await self._transport.request(
            "image.attach", {"session_id": self._session_id, "path": path}
        )

    async def get_usage(self) -> dict[str, Any]:
        """Get token usage and rate limits for the current session."""
        if not self._session_id:
            return {}
        return await self._transport.request("session.usage", {"session_id": self._session_id})

    async def voice_toggle(self, action: str = "status") -> dict[str, Any]:
        """Toggle voice mode. Actions: on|off|tts|status."""
        return await self._transport.request("voice.toggle", {"action": action})

    async def browser_manage(self, action: str = "status", url: str = "") -> dict[str, Any]:
        """Manage browser CDP connection."""
        params: dict[str, Any] = {"action": action}
        if url:
            params["url"] = url
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("browser.manage", params)

    async def rollback_list(self) -> dict[str, Any]:
        """List filesystem checkpoints."""
        if not self._session_id:
            return {"checkpoints": []}
        return await self._transport.request("rollback.list", {"session_id": self._session_id})

    async def rollback_restore(self, hash: str, file_path: str = "") -> dict[str, Any]:
        """Restore a checkpoint."""
        params: dict[str, Any] = {"hash": hash, "session_id": self._session_id}
        if file_path:
            params["file_path"] = file_path
        return await self._transport.request("rollback.restore", params)

    async def rollback_diff(self, hash: str) -> dict[str, Any]:
        """Show diff for a checkpoint."""
        if not self._session_id:
            return {}
        return await self._transport.request("rollback.diff", {"hash": hash, "session_id": self._session_id})

    async def skills_manage(self, action: str, query: str = "") -> dict[str, Any]:
        """Manage skills: list, inspect, search, install, browse."""
        params: dict[str, Any] = {"action": action}
        if query:
            params["query"] = query
        return await self._transport.request("skills.manage", params)

    async def tools_configure(self, action: str, names: list[str]) -> dict[str, Any]:
        """Enable or disable tools."""
        params: dict[str, Any] = {"action": action, "names": names}
        if self._session_id:
            params["session_id"] = self._session_id
        return await self._transport.request("tools.configure", params)

    async def plugins_list(self) -> list[dict[str, Any]]:
        """List installed plugins."""
        result = await self._transport.request("plugins.list")
        return result.get("plugins", [])

    async def cron_manage(self, action: str, name: str = "") -> dict[str, Any]:
        """Manage scheduled tasks."""
        params: dict[str, Any] = {"action": action}
        if name:
            params["name"] = name
        return await self._transport.request("cron.manage", params)

    async def reload_env(self) -> dict[str, Any]:
        """Re-read .env into the running session."""
        return await self._transport.request("reload.env")

    async def reload_mcp(self, *, confirm: bool = False, always: bool = False) -> dict[str, Any]:
        """Reload MCP servers."""
        params: dict[str, Any] = {"session_id": self._session_id}
        if confirm:
            params["confirm"] = True
        if always:
            params["always"] = True
        return await self._transport.request("reload.mcp", params)

    async def reload_skills(self) -> dict[str, Any]:
        """Re-scan installed skills."""
        result = await self._transport.request("skills.reload")
        return result

    async def get_agents(self) -> list[dict[str, Any]]:
        """List active agents and running tasks."""
        result = await self._transport.request("agents.list")
        return result.get("agents", [])

    async def get_delegation_pause(self, paused: bool) -> dict[str, Any]:
        """Pause or resume delegation."""
        return await self._transport.request("delegation.pause", {"paused": paused})

    async def get_delegation_status(self) -> dict[str, Any]:
        """Get delegation status."""
        return await self._transport.request("delegation.status")

    async def get_credits(self) -> dict[str, Any]:
        """Show Nous credit balance and top up info."""
        return await self._transport.request("credits.view")

    async def get_insights(self, days: int = 30) -> dict[str, Any]:
        """Show usage insights and analytics."""
        return await self._transport.request("insights.get", {"days": days})

    async def get_learning_frames(self) -> dict[str, Any]:
        """Get learning journey timeline frames."""
        return await self._transport.request("learning.frames")

    async def get_learning_detail(self, node_id: str) -> dict[str, Any]:
        """Get detail for a journey node."""
        return await self._transport.request("learning.detail", {"node_id": node_id})

    async def learning_delete(self, node_id: str) -> dict[str, Any]:
        """Delete a journey node."""
        return await self._transport.request("learning.delete", {"node_id": node_id})

    async def learning_edit(self, node_id: str, content: str) -> dict[str, Any]:
        """Edit a journey node."""
        return await self._transport.request("learning.edit", {"node_id": node_id, "content": content})

    async def show_config(self) -> dict[str, Any]:
        """Show current configuration."""
        return await self._transport.request("config.show")

    async def get_toolsets(self) -> dict[str, Any]:
        """List available toolsets."""
        return await self._transport.request("toolsets.list")

    async def paste_clipboard(self) -> dict[str, Any]:
        """Attach clipboard image."""
        if not self._session_id:
            return {}
        return await self._transport.request("clipboard.paste", {"session_id": self._session_id})

    async def handoff_request(self, platform: str) -> dict[str, Any]:
        """Hand off session to a messaging platform."""
        if not self._session_id:
            return {}
        return await self._transport.request(
            "handoff.request", {"session_id": self._session_id, "platform": platform}
        )

    async def handoff_state(self) -> dict[str, Any]:
        """Poll the handoff state."""
        if not self._session_id:
            return {}
        return await self._transport.request("handoff.state", {"session_id": self._session_id})

    async def pet_info(self) -> dict[str, Any]:
        """Get active pet info."""
        return await self._transport.request("pet.info")

    async def pet_gallery(self) -> dict[str, Any]:
        """List adoptable pets."""
        return await self._transport.request("pet.gallery")

    async def pet_select(self, slug: str) -> dict[str, Any]:
        """Adopt a pet."""
        return await self._transport.request("pet.select", {"slug": slug})

    async def pet_disable(self) -> dict[str, Any]:
        """Disable the active pet."""
        return await self._transport.request("pet.disable")

    async def pet_hatch(self, slug: str) -> dict[str, Any]:
        """Hatch a new pet."""
        return await self._transport.request("pet.hatch", {"slug": slug})
