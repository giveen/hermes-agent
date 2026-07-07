"""
Async JSON-RPC transport over stdio for Textual TUI.

Protocol:
  - Request:  {"jsonrpc":"2.0","id":"<rid>","method":"<name>","params":{...}}
  - Response: {"jsonrpc":"2.0","id":"<rid>","result":{...}}
  - Error:    {"jsonrpc":"2.0","id":"<rid>","error":{"code":int,"message":str}}
  - Event:    {"jsonrpc":"2.0","method":"event","params":{"type":"<event>","session_id":"<sid>",...}}

All frames are newline-delimited JSON (one JSON object per line).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], None]


class JsonRpcError(Exception):
    """Non-zero exit from a JSON-RPC call."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class StdioTransport:
    """Async JSON-RPC transport over pipe streams.

    Can be bound to either the process's own stdin/stdout (via connect())
    or to arbitrary child-process pipe streams (via bind_pipes()).
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._reader: asyncio.StreamReader | None = None
        self._raw_write = None
        self._writer_lock = asyncio.Lock()
        self._closed = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def bind_pipes(self, write_stream, read_stream) -> None:
        """Bind to a child process's stdin (write) and stdout (read).

        Must be called before connect().  The streams should be
        subprocess.PIPE ends (proc.stdin for writing, proc.stdout for reading).
        """
        loop = asyncio.get_running_loop()
        self._raw_write = write_stream
        self._reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(lambda: protocol, read_stream)

    async def connect(self) -> None:
        """Bind to the process's own stdin/stdout streams."""
        loop = asyncio.get_running_loop()
        if self._reader is None:
            self._reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(self._reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


    # ── Event handlers ─────────────────────────────────────────────────

    def on(self, event_type: str, handler: EventHandler) -> Callable[[], None]:
        """Register a handler for *event_type*. Returns a deregister callable."""
        handlers = self._event_handlers.setdefault(event_type, [])
        handlers.append(handler)

        def deregister() -> None:
            try:
                handlers.remove(handler)
            except ValueError:
                pass

        return deregister

    # ── Request / response ─────────────────────────────────────────────

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        if self._closed:
            raise JsonRpcError(-32000, "transport closed")
        rid = uuid.uuid4().hex[:8]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            await self._write(payload)
            result = await asyncio.wait_for(future, timeout=timeout)
            if "error" in result:
                err = result["error"]
                raise JsonRpcError(err.get("code", -1), err.get("message", "unknown error"), err.get("data"))
            return result.get("result", {})
        except asyncio.TimeoutError:
            raise JsonRpcError(-32001, f"request timed out after {timeout}s") from None
        finally:
            self._pending.pop(rid, None)

    # ── Internal ───────────────────────────────────────────────────────

    async def _write(self, payload: dict[str, Any]) -> None:
        """Serialize *payload* as newline-delimited JSON and write to the pipe."""
        async with self._writer_lock:
            line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            target = self._raw_write if self._raw_write is not None else sys.stdout
            try:
                target.write(line)
                target.flush()
            except OSError as exc:
                logger.warning("transport write error: %s", exc)
                raise JsonRpcError(-32002, f"write error: {exc}") from exc

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON frames from stdin and dispatch them."""
        assert self._reader is not None
        while not self._closed:
            try:
                raw = await self._reader.readline()
            except (OSError, asyncio.IncompleteReadError) as exc:
                if not self._closed:
                    logger.warning("transport read error: %s", exc)
                    self._emit_event("transport.disconnected", {})
                break
            if not raw:
                # EOF — the gateway closed its stdout
                if not self._closed:
                    self._emit_event("transport.disconnected", {})
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("transport: invalid JSON: %s — %r", exc, raw[:200])
                continue
            if not isinstance(msg, dict):
                continue

            # Response to a pending request
            if "id" in msg and msg.get("method") is None:
                rid = msg["id"]
                future = self._pending.get(str(rid))
                if future is not None and not future.done():
                    future.set_result(msg)
                continue

            # Server-initiated event
            if msg.get("method") == "event":
                params = msg.get("params", {})
                event_type = str(params.get("type", ""))
                self._emit_event(event_type, params)
                continue

    def _emit_event(self, event_type: str, params: dict[str, Any]) -> None:
        """Fire registered handlers for *event_type*."""
        for handler in self._event_handlers.get(event_type, []):
            try:
                handler(event_type, params)
            except Exception:
                logger.exception("event handler for %s failed", event_type)
        # Also fire catch-all handlers
        for handler in self._event_handlers.get("*", []):
            try:
                handler(event_type, params)
            except Exception:
                logger.exception("catch-all handler failed")

