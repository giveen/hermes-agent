"""
Gateway runner - entry point for messaging platform integrations.

This module provides:
- start_gateway(): Start all configured platform adapters
- GatewayRunner: Main class managing the gateway lifecycle

Usage:
    # Start the gateway
    python -m gateway.run
    
    # Or from CLI
    python cli.py --gateway
"""

# IMPORTANT: hermes_bootstrap must be the very first import — UTF-8 stdio
# on Windows.  No-op on POSIX.  See hermes_bootstrap.py for full rationale.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # Graceful fallback when hermes_bootstrap isn't registered in the venv
    # yet — happens during partial ``hermes update`` where git-reset landed
    # new code but ``uv pip install -e .`` didn't finish.  Missing bootstrap
    # means UTF-8 stdio setup is skipped on Windows; POSIX is unaffected.
    pass

import asyncio
import concurrent.futures
import dataclasses
import inspect
import json
import logging
import os
import re
import shlex
import site
import sys
import signal
import tempfile
import threading
import time
import sqlite3
from collections import OrderedDict
from contextvars import copy_context
from pathlib import Path
from datetime import datetime
from typing import Callable, Dict, Optional, Any, List, Union

# account_usage imports the OpenAI SDK chain (~230 ms). Only needed by
# /usage; we still import it at module top in the gateway because test
# patches (tests/gateway/test_usage_command.py) target
# `gateway.run.fetch_account_usage` as a module-level attribute. The
# gateway is a long-running daemon, so its boot cost matters less than
# preserving the established test-patch surface.
from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.async_utils import safe_schedule_threadsafe
from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
from agent.i18n import t
from hermes_cli.config import cfg_get
from hermes_cli.fallback_config import get_fallback_chain

# --- Agent cache tuning ---------------------------------------------------
# Bounds the per-session AIAgent cache to prevent unbounded growth in
# long-lived gateways (each AIAgent holds LLM clients, tool schemas,
# memory providers, etc.).  LRU order + idle TTL eviction are enforced
# from _enforce_agent_cache_cap() and _session_expiry_watcher() below.
_AGENT_CACHE_MAX_SIZE = 128
_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h
_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0
_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0
_GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS = 16 * 1024 * 1024
_TELEGRAM_COMMAND_MENTION_RE = re.compile(r"(?<![\w:/])/([A-Za-z0-9][A-Za-z0-9_-]*)")

_TELEGRAM_NOISY_STATUS_RE = re.compile(
    r"("  # transient/auxiliary status that should stay in logs, not gateway chats
    r"auxiliary\s+.+\s+failed"
    r"|compression\s+summary\s+failed"
    r"|fallback\s+context\s+marker"
    r"|configured\s+compression\s+model\s+.+\s+failed"
    r"|no\s+auxiliary\s+llm\s+provider\s+configured"
    r"|auto-lowered\s+compression\s+threshold"
    r"|compacting\s+context\s+[—-]\s+summarizing\s+earlier\s+conversation"
    r"|preflight\s+compression"
    r"|session\s+compressed\s+\d+\s+times"
    r"|rate\s+limited\.\s+waiting\s+\d"
    r"|retrying\s+in\s+\d"
    r"|max\s+retries\s+\(\d+\).*(?:trying\s+fallback|exhausted|invalid\s+responses)"
    r"|stream\s+(?:drop|drop\s+mid\s+tool-call).+retry\s+\d"
    r"|stale\s+connections\s+from\s+a\s+previous\s+provider\s+issue"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Surfaces that consume gateway text programmatically (CLI/TUI "local"
# diagnostics, API JSON, webhook payloads) and therefore must keep RAW
# status/error text. EVERY other platform is a human-facing chat surface
# where operational lifecycle/provider-error noise (and any secrets in it)
# must be suppressed or sanitized. Widens #28533's Telegram-only filter to
# all chat gateways (#39293). Fail-closed: unknown/empty platform -> chat.
_GATEWAY_RAW_TEXT_PLATFORMS = frozenset(
    {"local", "api_server", "webhook", "msgraph_webhook"}
)


def _gateway_surface_passes_raw_text(platform: Any) -> bool:
    """True only for programmatic/local surfaces that must keep raw text."""
    return _gateway_platform_value(platform) in _GATEWAY_RAW_TEXT_PLATFORMS


_GATEWAY_PROVIDER_ERROR_RE = re.compile(
    r"("  # infrastructure/provider error preambles, not ordinary assistant prose
    r"api\s+(?:call\s+)?failed"
    r"|provider\s+authentication\s+failed"
    r"|non-retryable\s+error"
    r"|rate\s+limited\s+after\s+\d+\s+retries"
    r"|error\s+code\s*:"
    r"|\bhttp\s*\d{3}\b"
    r"|incorrect\s+api\s+key"
    r"|invalid\s+api\s+key"
    r")",
    re.IGNORECASE,
)

_GATEWAY_PROVIDER_POLICY_RE = re.compile(
    r"("  # raw provider policy/safety bodies are noisy and may be sensitive
    r"cybersecurity\s+risk"
    r"|security\s+policy"
    r"|safety\s+policy"
    r"|policy\s+violation"
    r"|violat(?:e|es|ed|ion)"
    r"|blocked\s+(?:because|by|under)"
    r"|request\s+(?:was\s+)?(?:blocked|rejected)"
    r"|disallowed"
    r"|moderation"
    r")",
    re.IGNORECASE,
)

_GATEWAY_AUTH_ERROR_RE = re.compile(
    r"(provider\s+authentication\s+failed|incorrect\s+api\s+key|invalid\s+api\s+key|\b401\b)",
    re.IGNORECASE,
)

_GATEWAY_RATE_LIMIT_RE = re.compile(
    r"(rate\s+limit|rate-limited|\b429\b|quota|usage\s+limit)",
    re.IGNORECASE,
)

_GATEWAY_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxapp-\d+-[A-Za-z0-9\-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{20,}\b"),
)


def _ensure_windows_gateway_venv_imports() -> None:
    """Make detached Windows gateway runs see the Hermes venv packages.

    Some Windows restart paths run the gateway under uv's base ``pythonw.exe``
    to avoid the venv launcher respawning a visible console interpreter.  That
    mode can import the source tree via cwd/PYTHONPATH but still miss optional
    packages installed only in ``venv/Lib/site-packages`` (notably the MCP SDK).
    Patch the live process before MCP discovery so tool injection does not
    depend on every launcher preserving PYTHONPATH perfectly.
    """
    if sys.platform != "win32":
        return

    project_root = Path(__file__).resolve().parent.parent
    candidates: list[Path] = []
    if os.environ.get("VIRTUAL_ENV"):
        candidates.append(Path(os.environ["VIRTUAL_ENV"]))
    candidates.append(project_root / "venv")

    seen: set[str] = set()
    for venv_dir in candidates:
        try:
            resolved_venv = venv_dir.resolve()
        except OSError:
            resolved_venv = venv_dir
        venv_key = str(resolved_venv).lower()
        if venv_key in seen:
            continue
        seen.add(venv_key)

        site_packages = resolved_venv / "Lib" / "site-packages"
        if not site_packages.exists():
            continue

        project_entry = str(project_root)
        site_entry = str(site_packages)
        if project_entry not in sys.path:
            sys.path.insert(0, project_entry)
        # addsitepackages() semantics matter here: pywin32, used by the MCP
        # SDK on Windows, relies on .pth processing to expose pywintypes.
        site.addsitedir(site_entry)
        if site_entry in sys.path:
            sys.path.remove(site_entry)
        insert_at = 1 if sys.path and sys.path[0] == project_entry else 0
        sys.path.insert(insert_at, site_entry)

        os.environ["VIRTUAL_ENV"] = str(resolved_venv)
        pythonpath = [project_entry, site_entry]
        if os.environ.get("PYTHONPATH"):
            pythonpath.append(os.environ["PYTHONPATH"])
        os.environ["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
        return


def _gateway_platform_value(platform: Any) -> str:
    """Return a normalized gateway platform value for enums or raw strings."""
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _non_conversational_metadata(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    platform: Any = None,
) -> Optional[Dict[str, Any]]:
    """Mark Discord lifecycle/status sends without changing other platforms."""
    if _gateway_platform_value(platform) != "discord":
        return metadata
    merged = dict(metadata or {})
    merged["non_conversational"] = True
    return merged


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return True for transient network errors safe to log + swallow.

    The crash class targeted by #31066 / #31110: an unhandled Telegram
    ``TimedOut`` (or peer ``NetworkError`` / ``httpx`` connection error)
    propagating to the event loop and killing the entire gateway
    process. These are by definition transient — the next poll cycle or
    user action recovers — so they must never crash the process.

    Walk the exception cause chain so wrapped errors (e.g. PTB's
    ``NetworkError`` wrapping ``httpx.ConnectError``) are still
    classified. The chain is bounded to avoid pathological cycles.
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    depth = 0
    transient_class_names = {
        "TimedOut",
        "NetworkError",
        "ReadError",
        "WriteError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "ServerDisconnectedError",
        "ClientConnectorError",
        "ClientOSError",
    }
    while cur is not None and depth < 12:
        ident = id(cur)
        if ident in seen:
            break
        seen.add(ident)
        depth += 1
        name = type(cur).__name__
        if name in transient_class_names:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _gateway_loop_exception_handler(
    loop: "asyncio.AbstractEventLoop", context: Dict[str, Any]
) -> None:
    """Loop-level safety net for transient network errors.

    Installed once during :func:`start_gateway`. Catches the
    ``telegram.error.TimedOut`` crash class (issues #31066 / #31110)
    and any peer transient network error before it can kill the
    gateway process. Logs at WARNING with full traceback so the
    originating call site stays diagnosable; non-transient errors
    are forwarded to the default loop handler so real bugs still
    surface.
    """
    exc = context.get("exception")
    if exc is not None and _is_transient_network_error(exc):
        message = context.get("message") or "transient network error"
        task = context.get("future") or context.get("task")
        task_name = ""
        if task is not None:
            try:
                task_name = task.get_name() if hasattr(task, "get_name") else repr(task)
            except Exception:
                task_name = repr(task)
        logger.warning(
            "Gateway swallowed transient network error from %s: %s: %s",
            task_name or "<unknown task>",
            type(exc).__name__,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return
    # Fall back to the default handler for anything we don't recognise.
    loop.default_exception_handler(context)


def _redact_gateway_user_facing_secrets(text: str) -> str:
    """Secret redaction before text can leave the gateway.

    Delegates to the authoritative ``agent.redact.redact_sensitive_text`` — the
    same Tirith-grade redactor already applied to logs, tool output, and
    approval-command prompts — so the outbound chat path masks the full
    credential set the startup banner promises ("chat responses are scrubbed
    before delivery"), not a divergent subset. ``force=True`` honors redaction
    even when ``security.redact_secrets`` is off, matching the
    ``_redact_approval_command`` reasoning (#23810).

    The narrow ``_GATEWAY_SECRET_PATTERNS`` set runs as a belt-and-suspenders
    second pass so nothing the gateway historically caught can regress, and so
    redaction still degrades gracefully if the import ever fails.
    """
    redacted = str(text or "")
    try:
        from agent.redact import redact_sensitive_text

        redacted = redact_sensitive_text(redacted, force=True)
    except Exception:
        # Fail-soft: fall back to the local pattern pass below rather than
        # letting a redactor import/error leak the raw text to chat.
        pass
    for pattern in _GATEWAY_SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", redacted)
    return redacted


def _redact_approval_command(cmd: "str | None") -> str:
    """Redact credentials from a command before it goes into an approval prompt.

    Tirith's *findings* are already redacted, but the gateway approval prompt
    is built from the raw command string, so a credential-shaped value Tirith
    flagged would otherwise be echoed verbatim to the chat platform (#48456).
    Uses ``redact_sensitive_text(force=True)`` — the same Tirith-grade redactor
    — so the prompt honors redaction even when ``security.redact_secrets`` is
    off. Module-level so the wiring is unit-testable (the call site is a deeply
    nested gateway closure that cannot be driven directly).
    """
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(str(cmd or ""), force=True)


def _gateway_provider_error_reply(text: str) -> str:
    """Map raw provider/API errors to a short user-safe Telegram reply."""
    if _GATEWAY_AUTH_ERROR_RE.search(text):
        return (
            "⚠️ Provider authentication failed. Check the configured credentials; "
            "raw provider details are in the gateway logs."
        )
    if _GATEWAY_PROVIDER_POLICY_RE.search(text):
        return (
            "⚠️ The model provider rejected the request. I kept the raw provider "
            "error out of chat; check gateway logs for details or try rephrasing."
        )
    if _GATEWAY_RATE_LIMIT_RE.search(text):
        return "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."
    return (
        "⚠️ The model provider failed after retries. I kept raw provider details "
        "out of chat; check gateway logs for diagnostics."
    )


_GATEWAY_PROVIDER_ERROR_SHAPE_RE = re.compile(
    r"^\s*(\W*\s*)?("
    r"api\s+(?:call\s+)?failed"
    r"|provider\s+authentication\s+failed"
    r"|non-retryable\s+error"
    r"|rate\s+limited\s+after\s+\d+\s+retries"
    r"|error\s+code\s*:"
    r"|http\s*\d{3}\b"
    r"|incorrect\s+api\s+key"
    r"|invalid\s+api\s+key"
    r")",
    re.IGNORECASE,
)


def _looks_like_gateway_provider_error(text: str) -> bool:
    """True when text is infrastructure/provider failure, not normal content.

    Two heuristics combined so the rewrite only fires on actual provider
    error envelopes, not on assistant prose that happens to mention an
    HTTP status code:

    1. The text is short — real provider errors are 1–3 lines of envelope
       text; assistant answers are usually longer.
    2. AND the error marker appears at the start of the message (optionally
       behind a punctuation/symbol prefix), not buried mid-paragraph in an
       explanation like "HTTP 404 means 'not found' — ...".
    """
    if not text:
        return False
    body = str(text).strip()
    # Provider failure envelopes are short. Assistant answers that happen
    # to mention HTTP status codes ("HTTP 404 means...") tend to be longer.
    if len(body) > 400 or body.count("\n") > 4:
        return False
    return bool(_GATEWAY_PROVIDER_ERROR_SHAPE_RE.search(body))


def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """Sanitize final gateway replies before sending them to chat surfaces.

    Every human-facing chat surface (Telegram, WhatsApp, Discord, Slack,
    Signal, Matrix, plugin platforms, etc.) should receive concise, safe
    provider failure categories with secrets redacted instead of raw HTTP
    bodies, request IDs, leaked credentials, or policy text. Only programmatic
    surfaces in ``_GATEWAY_RAW_TEXT_PLATFORMS`` (CLI/TUI ``local`` diagnostics,
    API JSON, webhook payloads) keep the raw text unchanged.
    """
    if not text:
        return text
    if _gateway_surface_passes_raw_text(platform):
        return text

    # Cancellation metadata, not assistant prose. ACP/TUI already suppress
    # this sentinel; chat surfaces should too (#7921).
    if str(text).strip().startswith(INTERRUPT_WAITING_FOR_MODEL_PREFIX):
        return ""

    redacted = _redact_gateway_user_facing_secrets(str(text))
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted


def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery.

    Local/CLI sessions keep the raw diagnostic stream. Messaging gateway
    surfaces should not receive transient auxiliary/compression chatter.
    """
    text = str(message or "").strip()
    if not text:
        return None
    if _gateway_surface_passes_raw_text(platform):
        return text

    text = _redact_gateway_user_facing_secrets(text)
    if _TELEGRAM_NOISY_STATUS_RE.search(text):
        return None
    if _looks_like_gateway_provider_error(text):
        return _gateway_provider_error_reply(text)
    return text


def render_notice_line(notice) -> str:
    """Render an AgentNotice to a single plaintext line for messaging platforms.

    Messaging has no persistent status bar (unlike the TUI), so a notice is a
    one-shot standalone push. The notice policy already bakes the level glyph
    (⚠ / • / ✕ / ✓) into the text, and the TUI + CLI REPL render that text
    verbatim — so we emit it as-is here too. Prepending a per-level glyph would
    DOUBLE it ("⚠ ⚠ Credits 90% used", "⛔ ✕ Credit access paused"). Plaintext
    only — no markdown — so it renders uniformly across Telegram/Discord/Slack/
    SMS without per-platform escaping. Fail-soft: a malformed/empty notice
    degrades to "" rather than raising on the agent's callback path.
    """
    return str(getattr(notice, "text", "") or "").strip()


async def _send_or_update_status_coro(adapter, chat_id, status_key, content, metadata):
    """Route a status message through adapter.send_or_update_status when supported.

    Issue #30045: adapters that implement send_or_update_status (currently
    Telegram) edit the previous bubble for the same status_key instead of
    appending a new one. Adapters without the method fall back to plain send.
    """
    sender = getattr(adapter, "send_or_update_status", None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await adapter.send(chat_id, content, metadata=metadata)


def _resolve_progress_thread_id(platform: Any, source_thread_id: Any, event_message_id: Any) -> Optional[str]:
    """Return thread/root ID that progress/status bubbles should target."""
    if source_thread_id:
        return str(source_thread_id)
    return None


def _has_platform_display_override(user_config: dict, platform_key: str, setting: str) -> bool:
    """Return True when display.platforms.<platform> explicitly sets setting."""
    display = user_config.get("display") if isinstance(user_config, dict) else None
    if not isinstance(display, dict):
        return False
    platforms = display.get("platforms")
    if not isinstance(platforms, dict):
        return False
    platform_cfg = platforms.get(platform_key)
    return isinstance(platform_cfg, dict) and setting in platform_cfg


def _resolve_gateway_display_bool(
    user_config: dict,
    platform_key: str,
    setting: str,
    *,
    default: bool = False,
    platform: Any = None,
    require_platform_override_for: set[Any] | None = None,
) -> bool:
    """Resolve a boolean display setting with optional platform-only opt-in.

    Some display features expose assistant scratch text rather than deliberate
    user-facing output.  For high-noise threaded chat surfaces,
    a global opt-in is too broad: they must be enabled with an
    explicit display.platforms.<platform>.<setting> override.
    """
    current_platform = _gateway_platform_value(platform or platform_key)
    platform_only = {
        _gateway_platform_value(candidate)
        for candidate in (require_platform_override_for or set())
    }
    if (
        current_platform in platform_only
        and not _has_platform_display_override(user_config, platform_key, setting)
    ):
        return False

    from gateway.display_config import resolve_display_setting

    value = resolve_display_setting(user_config, platform_key, setting, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on"}
    if value is None:
        return bool(default)
    return bool(value)


def _telegramize_command_mentions(text: str, platform: Any) -> str:
    """Rewrite slash-command mentions to Telegram-valid command names.

    Telegram Bot API command names allow only lowercase letters, digits, and
    underscores.  Keep other platform renderings unchanged, but normalize
    Telegram help text so command mentions remain clickable/valid there.
    """
    platform_value = getattr(platform, "value", platform)
    if platform_value != "telegram":
        return text

    from hermes_cli.commands import _sanitize_telegram_name

    def _replace(match: re.Match[str]) -> str:
        sanitized = _sanitize_telegram_name(match.group(1))
        return f"/{sanitized}" if sanitized else match.group(0)

    return _TELEGRAM_COMMAND_MENTION_RE.sub(_replace, text)


# Only auto-continue interrupted gateway turns while the interruption is fresh.
# Stale tool-tail/resume markers can otherwise revive an unrelated old task
# after a gateway restart when the user's next message starts new work.
#
# The freshness signal is the timestamp of the last transcript row, which
# ``hermes_state.get_messages`` carries on every persisted message.  This
# handles the two auto-continue cases uniformly:
#   * resume_pending (gateway restart/shutdown watchdog marked the session)
#   * tool-tail     (last persisted message is a tool result the agent
#                    never got to reply to)
# In both cases "when did we last do anything on this transcript" is the
# correct freshness question, so one signal replaces two divergent ones.
#
# Default window: 1 hour.  This comfortably covers ``agent.gateway_timeout``
# (30 min default) plus runtime slack — a legitimate long-running turn that
# gets interrupted near its timeout boundary and is resumed shortly after
# is still classified fresh.  Override via
# ``config.yaml`` ``agent.gateway_auto_continue_freshness``.
_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60


def _coerce_gateway_timestamp(value: Any) -> Optional[float]:
    """Best-effort conversion of stored gateway timestamps to epoch seconds.

    Missing/unparseable timestamps return None so legacy transcripts keep the
    historical auto-continue behaviour instead of being silently dropped.
    Accepts: datetime, epoch seconds (int/float), epoch milliseconds (when
    the magnitude exceeds year-2286), ISO-8601 strings (with or without a
    trailing ``Z``), and numeric strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool):  # bool is a subclass of int — skip it
        return None
    if isinstance(value, (int, float)):
        # Some platform events use milliseconds; Hermes state rows use seconds.
        return float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _auto_continue_freshness_window() -> float:
    """Return the configured auto-continue freshness window in seconds.

    Thin wrapper that delegates to the canonical implementation in
    ``gateway.session`` (the single source of truth shared with the
    routing-time zombie gate in ``get_or_create_session``).  Reads
    ``HERMES_AUTO_CONTINUE_FRESHNESS`` (bridged from ``config.yaml``
    ``agent.gateway_auto_continue_freshness`` at gateway startup, same
    pattern as ``HERMES_AGENT_TIMEOUT``).  Falls back to the module default
    when unset or malformed.  Non-positive values disable the freshness gate
    (restores the pre-fix "always fresh" behaviour for users who want to opt
    out).  Kept here so existing call sites and test patches importing it
    from ``gateway.run`` continue to work.
    """
    from gateway.session import auto_continue_freshness_window
    return auto_continue_freshness_window()


def _float_env(name: str, default: float) -> float:
    """Read an env var as float, falling back to ``default`` on typos/empty.

    A misconfigured env var (e.g. ``HERMES_AGENT_TIMEOUT=abc``) must not
    crash the gateway or an agent turn.  Unset/empty also falls back.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _is_fresh_gateway_interruption(
    value: Any,
    *,
    now: Optional[float] = None,
    window_secs: Optional[float] = None,
) -> bool:
    """Return True when an interruption marker is fresh enough to auto-continue.

    Unknown timestamps are treated as fresh for backward compatibility with
    legacy transcripts (pre-dating timestamp persistence) and with in-memory
    test scaffolding that constructs history entries without timestamps.

    A non-positive ``window_secs`` disables the gate (always fresh), which
    restores the pre-fix behaviour for users who opt out via config.
    """
    window = (
        float(window_secs)
        if window_secs is not None
        else float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    )
    if window <= 0:
        return True
    timestamp = _coerce_gateway_timestamp(value)
    if timestamp is None:
        return True
    current = time.time() if now is None else now
    return current - timestamp <= window


# Assistant-message fields that must survive transcript replay so multi-turn
# reasoning context, prefix-cache hits, and provider-specific echo
# requirements all behave the same on the gateway as they do in the CLI.
#
# ``reasoning`` and ``reasoning_details`` were the original three preserved
# by PR #2974 (schema v6).  ``reasoning_content``, ``codex_reasoning_items``,
# ``codex_message_items``, and ``finish_reason`` were added to the DB later
# but the gateway's replay whitelist was never expanded to match — so any
# pure-text assistant turn (no ``tool_calls``) silently dropped them on
# replay, regressing the CLI-vs-gateway behavioural parity.
#
# Why each field matters on replay:
#   * ``reasoning`` / ``reasoning_content``: provider-facing thinking text.
#     ``_copy_reasoning_content_for_api`` promotes ``reasoning`` →
#     ``reasoning_content`` at send time, but only when the strings happen to
#     match.  Carrying the original ``reasoning_content`` verbatim avoids
#     reconstruction loss for providers that return them as distinct fields
#     (DeepSeek/Kimi/Moonshot thinking modes).
#   * ``reasoning_details``: opaque structured array (signature,
#     encrypted_content) used by OpenRouter/Anthropic to maintain reasoning
#     continuity across turns.
#   * ``codex_reasoning_items``: encrypted reasoning blobs for the OpenAI
#     Codex Responses API.
#   * ``codex_message_items``: exact assistant message items with ``phase``.
#     OpenAI docs: "preserve and resend phase on all assistant messages —
#     dropping it can degrade performance."  Required for prefix cache hits.
#   * ``finish_reason``: informational; cheap to keep so transcripts replay
#     identically across CLI and gateway.
_ASSISTANT_REPLAY_FIELDS: tuple[str, ...] = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "finish_reason",
)


def _build_replay_entry(
    role: str,
    content: Any,
    msg: Dict[str, Any],
    preserve_timestamp: bool = False,
) -> Dict[str, Any]:
    """Build a replay entry for a non-tool-calling message, preserving the
    assistant fields the agent's API builders rely on for multi-turn fidelity.

    Lifted out of the inline ``run_sync`` closure so the field whitelist can
    be unit-tested in isolation.  Mirrors the ``_ASSISTANT_REPLAY_FIELDS``
    contract above.

    ``preserve_timestamp``: when True, copy the source row's ``timestamp``
    onto the replay entry. Currently only user messages need this — the
    stale-dangerous-confirmation stripper in ``agent/replay_cleanup.py``
    reads the timestamp to decide whether a confirmation is too old to
    replay safely.  Assistant/tool messages are not timestamp-stripped in
    the same way, so we keep the existing default of dropping it.

    Empty values: most fields are dropped when falsy (matching the original
    PR #2974 behaviour) since an empty list/string for those carries no
    information.  The exception is ``reasoning_content``: DeepSeek/Kimi
    thinking-mode replay treats an empty string as a meaningful sentinel
    that ``_copy_reasoning_content_for_api`` upgrades to a single space.
    Dropping it here would make the gateway send no ``reasoning_content``
    at all on the next turn, which can cause HTTP 400 from strict thinking
    providers.
    """
    entry: Dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        for _rkey in _ASSISTANT_REPLAY_FIELDS:
            if _rkey not in msg:
                continue
            _rval = msg.get(_rkey)
            if _rkey == "reasoning_content":
                # Preserve empty-string sentinel for thinking-mode replay.
                if _rval is None:
                    continue
            elif not _rval:
                continue
            entry[_rkey] = _rval
    if preserve_timestamp:
        ts = msg.get("timestamp")
        if ts:
            entry["timestamp"] = ts
    return entry


_TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER = "observed Telegram group context"
_OBSERVED_GROUP_CONTEXT_HEADER = "[Observed Telegram group context - context only, not requests]"
_CURRENT_ADDRESSED_MESSAGE_HEADER = "[Current addressed message - answer only this unless it explicitly asks you to use the observed context]"


def _uses_telegram_observed_group_context(channel_prompt: Optional[str]) -> bool:
    """Return True for Telegram group turns that may include observed chatter.

    Telegram's observe-unmentioned mode persists skipped group chatter so a
    later @mention can see it. Those rows must not replay as ordinary user
    turns: a weak wake word like ``@bot cambio`` should not make the model treat
    old unmentioned chatter as pending work. The Telegram adapter marks these
    turns with a channel prompt; this helper keeps the run-path check explicit
    and unit-testable.
    """

    return bool(channel_prompt and _TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER in channel_prompt)


def _message_timestamps_enabled(user_config: Optional[dict]) -> bool:
    """True when gateway.message_timestamps.enabled is opted in.

    Default OFF: injecting a ``[Tue 2026-04-28 13:40:53 CEST]`` prefix onto
    every user message changes what the model sees for all gateway users, so
    it must be explicitly enabled in config.yaml under
    ``gateway.message_timestamps.enabled``.
    """
    if not isinstance(user_config, dict):
        return False
    gw = user_config.get("gateway")
    if not isinstance(gw, dict):
        return False
    mt = gw.get("message_timestamps")
    if isinstance(mt, dict):
        return bool(mt.get("enabled", False))
    # Allow a bare ``message_timestamps: true`` shorthand.
    return bool(mt)


def _build_gateway_agent_history(
    history: List[Dict[str, Any]],
    *,
    channel_prompt: Optional[str] = None,
    inject_timestamps: bool = False,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Convert stored gateway transcript rows into agent replay messages.

    Observed Telegram group rows are returned as API-only context for the
    current addressed message instead of being replayed as normal prior user
    turns.  Keeping that context out of ``conversation_history`` avoids
    consecutive-user repair merging it with the live user turn and then hiding
    the current message behind ``history_offset`` during persistence.

    When ``inject_timestamps`` is True (gateway.message_timestamps.enabled),
    each replayed user message is rendered with a single human-readable
    timestamp prefix from its stored metadata.
    """

    from hermes_time import get_timezone as _get_msg_tz
    from gateway.message_timestamps import (
        render_user_content_with_timestamp as _render_msg_ts,
    )

    _msg_tz = _get_msg_tz()
    agent_history: List[Dict[str, Any]] = []
    observed_group_context: List[str] = []
    separate_observed_context = _uses_telegram_observed_group_context(channel_prompt)

    for msg in history or []:
        role = msg.get("role")
        if not role:
            continue

        # Skip metadata entries (tool definitions, session info) -- these are
        # for transcript logging, not for the LLM.
        if role in {"session_meta",}:
            continue

        # Skip system messages -- the agent rebuilds its own system prompt.
        if role == "system":
            continue

        content = msg.get("content")
        if inject_timestamps and role == "user" and isinstance(content, str):
            content = _render_msg_ts(content, msg.get("timestamp"), tz=_msg_tz)
        if separate_observed_context and msg.get("observed") and role == "user" and content:
            observed_group_context.append(str(content).strip())
            continue

        # Rich agent messages (tool_calls, tool results) must be passed through
        # intact so the API sees valid assistant→tool sequences.
        has_tool_calls = "tool_calls" in msg
        has_tool_call_id = "tool_call_id" in msg
        is_tool_message = role == "tool"

        if has_tool_calls or has_tool_call_id or is_tool_message:
            clean_msg = {k: v for k, v in msg.items() if k not in {"timestamp", "observed"}}
            agent_history.append(clean_msg)
        elif content:
            # Strip gateway-injected auto-continue notes that were persisted
            # as part of user messages during interrupted turns.  Keep the
            # user's real text after the note, but never replay the recovery
            # instruction itself — that is what caused infinite re-execution
            # loops for interrupted long-running tools.
            if role == "user":
                content = _strip_auto_continue_noise(content)
                if not content:
                    continue
            # Simple text message - just need role and content.
            if msg.get("mirror"):
                mirror_src = msg.get("mirror_source", "another session")
                content = f"[Delivered from {mirror_src}] {content}"
            # Preserve the timestamp on user messages so the
            # stale-dangerous-confirmation stripper in agent/replay_cleanup.py
            # can read it. The timestamp is dropped from assistant messages
            # because they don't need it; the replay-tail strippers look at
            # assistant(tool_calls), not timestamps.
            entry = _build_replay_entry(role, content, msg, preserve_timestamp=(role == "user"))
            agent_history.append(entry)

    # Strip interrupted tool-call tails so the LLM doesn't re-execute
    # tools that were killed mid-flight.
    agent_history = _strip_interrupted_tool_tails(agent_history)

    # Strip a dangling assistant(tool_calls) tail with no tool answers —
    # the signature of a SIGKILL mid-tool-call (e.g. the tool itself ran
    # `docker restart`/`kill` and took the gateway down before the result
    # was persisted). Without this the model re-issues the unanswered call
    # on resume and loops the restart forever (#49201).
    agent_history = _strip_dangling_tool_call_tail(agent_history)

    # Strip stale dangerous-confirmation text in user messages (#59607).
    # A high-risk confirmation phrase (e.g. "confirm forced restart") that
    # is older than the expiry window must not be replayed to the model,
    # otherwise an unrelated follow-up message can be interpreted as a
    # fresh confirmation and trigger the destructive action a second time.
    agent_history = _strip_stale_dangerous_confirmations(
        agent_history, now=time.time()
    )

    observed_context = "\n".join(observed_group_context).strip() or None
    return agent_history, observed_context


def _select_cached_agent_history(
    persisted_history: List[Dict[str, Any]],
    live_history: Any,
) -> List[Dict[str, Any]]:
    """Prefer a cached agent's live in-memory transcript over a shorter
    persisted one.

    Guards the FTS write-corruption case (#50502): when message writes fail
    silently through corrupt FTS triggers, the next turn reloads a stale/empty
    ``conversation_history`` from disk even though the same cached ``AIAgent``
    still holds the full live ``_session_messages``. Replacing the live
    transcript with that shorter persisted copy causes immediate same-session
    amnesia. When the live transcript is strictly longer, keep it.

    Returns ``persisted_history`` unchanged unless the live copy is a longer
    list, in which case a copy of the live transcript is returned.
    """
    if isinstance(live_history, list) and len(live_history) > len(persisted_history):
        return list(live_history)
    return persisted_history


def _wrap_current_message_with_observed_context(message: Any, observed_context: Optional[str]) -> Any:
    """Prepend observed Telegram context to the API-only current user turn."""

    if not observed_context:
        return message

    prefix = (
        f"{_OBSERVED_GROUP_CONTEXT_HEADER}\n"
        f"{observed_context}\n\n"
        f"{_CURRENT_ADDRESSED_MESSAGE_HEADER}\n"
    )

    if isinstance(message, str):
        return f"{prefix}{message}"

    if isinstance(message, list):
        wrapped = [dict(part) if isinstance(part, dict) else part for part in message]
        for part in wrapped:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = f"{prefix}{part.get('text', '')}"
                return wrapped
        return [{"type": "text", "text": prefix.rstrip()}] + wrapped

    return message


def _last_transcript_timestamp(history: Optional[List[Dict[str, Any]]]) -> Any:
    """Return the ``timestamp`` of the last usable transcript row, if any.

    Skips metadata-only rows (``session_meta``, system injections) that are
    dropped before being handed to the agent.  Returns ``None`` when no
    usable row carries a timestamp — callers should treat that as "fresh"
    for backward compatibility.
    """
    if not history:
        return None
    for msg in reversed(history):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if not role or role in {"session_meta", "system"}:
            continue
        ts = msg.get("timestamp")
        if ts is not None:
            return ts
        # First non-meta row without a timestamp — legacy transcript row.
        # Returning None lets the caller fall through to the legacy-fresh path.
        return None
    return None


# Tool results can contain literal MEDIA: examples in docs, logs, or other
# ordinary outputs. Only tools that intentionally create deliverable media
# artifacts should be eligible for automatic append when the model omits them
# from the final gateway reply.
_AUTO_APPEND_MEDIA_TOOL_NAMES = {
    "text_to_speech",
    "text_to_speech_tool",
    "image_generate",
}

# ---- helpers: detect interrupted tool tails & auto-continue noise ----------

# Replay-tail sanitization lives in agent/replay_cleanup.py so every resume
# surface (this messaging gateway AND the TUI/WebUI gateway) shares one
# implementation.  Re-exported under the historical private names so existing
# call sites and tests keep working.
from agent.replay_cleanup import (  # noqa: E402
    is_interrupted_tool_result as _is_interrupted_tool_result,
    strip_interrupted_tool_tails as _strip_interrupted_tool_tails,
    strip_dangling_tool_call_tail as _strip_dangling_tool_call_tail,
    strip_stale_dangerous_confirmations as _strip_stale_dangerous_confirmations,
    is_dangerous_confirmation as _is_dangerous_confirmation,
)


_AUTO_CONTINUE_NOTE_PREFIX = "[System note: Your previous turn"
_AUTO_CONTINUE_FALLBACK_PREFIX = "[System note: A new message"


def _is_auto_continue_noise(content: Any) -> bool:
    """Return True if this user-message content is a gateway-injected
    auto-continue note that should NOT be replayed as a real user turn."""
    if not isinstance(content, str):
        return False
    return (
        content.startswith(_AUTO_CONTINUE_NOTE_PREFIX)
        or content.startswith(_AUTO_CONTINUE_FALLBACK_PREFIX)
    )


def _strip_auto_continue_noise(content: Any) -> Any:
    """Remove persisted gateway auto-continue note prefix from user text.

    Older gateway builds prepended the recovery note directly to the user
    message, so the transcript row can contain both the synthetic note and
    the user's real question.  Strip one or more leading synthetic notes while
    preserving any real text that follows.
    """
    if not _is_auto_continue_noise(content):
        return content
    text = str(content)
    while _is_auto_continue_noise(text):
        end = text.find("]")
        if end < 0:
            return ""
        text = text[end + 1 :].lstrip()
    return text

# Tools in this set return their deliverable artifact as a JSON payload with a
# local-file path field rather than a literal ``MEDIA:`` tag (e.g. image_generate
# returns ``{"success": true, "image": "/abs/path.png"}``). The auto-append path
# extracts the path from these fields so delivery is deterministic and does not
# depend on the model restating the path in its final reply.
_JSON_MEDIA_TOOL_PATH_FIELDS = ("host_image", "image", "agent_visible_image")


# Extension-anchored MEDIA: matcher for tool results. Mirrors the dispatch-site
# pattern so a bare ``MEDIA:`` token in prose (no deliverable extension) is never
# auto-appended. Kept local to the auto-append path; the producer-tool allowlist
# below is the primary guard, this is the secondary precision guard.
_TOOL_MEDIA_RE = re.compile(
    r'MEDIA:((?:[A-Za-z]:[/\\]|/|~\/)\S+\.(?:png|jpe?g|gif|webp|'
    r'mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|'
    r'flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|'
    r'txt|csv|apk|ipa))',
    re.IGNORECASE,
)


def _collect_auto_append_media_tags(
    messages: List[Dict[str, Any]],
    history_offset: int = 0,
    history_media_paths: Optional[set] = None,
) -> tuple[List[str], bool]:
    """Collect real media tags from current-turn producer-tool results only.

    Two layered guards keep stale/example MEDIA: strings out of the reply:

    1. Producer-tool allowlist: only tools that intentionally emit deliverable
       artifacts (TTS) are eligible. Documentation, logs, and search results can
       contain example strings such as MEDIA:/absolute/path/to/file, which must
       never be delivered as attachments. (Fixes the original report behind #16721.)
    2. Current-turn isolation: only messages produced this turn are scanned, so a
       tool result from an earlier turn (still present in the full message list)
       cannot leak onto a later text-only reply (#34608).

    Mid-run context compression can rewrite/shrink the message list below the
    original history length. When that happens the slice boundary is no longer
    trustworthy, so fall back to scanning every message and rely on
    ``history_media_paths`` for dedup, preserving the compression-safe behaviour
    of #160. The producer-tool allowlist still applies on the fallback path.
    """
    history_media_paths = history_media_paths or set()
    # Only trust the slice boundary when the message list still contains the
    # full history prefix. Otherwise scan everything (compression-safe fallback).
    if history_offset and len(messages) >= history_offset:
        new_messages = messages[history_offset:]
    else:
        new_messages = messages

    tool_name_by_call_id: Dict[str, str] = {}
    for msg in new_messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            call_id = call.get("id") or call.get("call_id")
            fn = call.get("function") or {}
            name = str(fn.get("name") or call.get("name") or "")
            if call_id and name:
                tool_name_by_call_id[str(call_id)] = name

    media_tags: List[str] = []
    has_voice_directive = False
    for msg in new_messages:
        if msg.get("role") not in ("tool", "function"):
            continue
        call_id = str(msg.get("tool_call_id") or msg.get("call_id") or "")
        if tool_name_by_call_id.get(call_id) not in _AUTO_APPEND_MEDIA_TOOL_NAMES:
            continue
        content = str(msg.get("content") or "")
        tool_name = tool_name_by_call_id.get(call_id)
        # JSON-payload tools (image_generate) return a local-file path in a
        # known field rather than a MEDIA: tag. Extract it so delivery is
        # deterministic even when the model omits the path from its reply.
        if tool_name == "image_generate" and "MEDIA:" not in content:
            try:
                payload = json.loads(content)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("success"):
                for field in _JSON_MEDIA_TOOL_PATH_FIELDS:
                    path = payload.get(field)
                    if (isinstance(path, str)
                            and _TOOL_MEDIA_RE.fullmatch(f"MEDIA:{path}")
                            and path not in history_media_paths):
                        media_tags.append(f"MEDIA:{path}")
                        break
            continue
        if "MEDIA:" not in content:
            continue
        for match in _TOOL_MEDIA_RE.finditer(content):
            path = match.group(1).strip().rstrip('",}')
            if path and path not in history_media_paths:
                media_tags.append(f"MEDIA:{path}")
        if "[[audio_as_voice]]" in content:
            has_voice_directive = True

    return media_tags, has_voice_directive


def _collect_history_media_paths(agent_history: List[Dict[str, Any]]) -> set:
    """Collect every media path already delivered in prior tool results.

    Used to dedup auto-appended MEDIA tags so the same file is not re-sent on
    later turns. Must cover BOTH delivery shapes:
      * ``MEDIA:<path>`` text tags in tool results, and
      * ``image_generate`` JSON-payload paths (``host_image`` / ``image`` /
        ``agent_visible_image``), which carry no MEDIA: tag.

    Missing the JSON-payload shape caused #46627: after a compression
    boundary the auto-append fallback rescans full history, re-discovers an
    earlier ``image_generate`` result whose path was never in the dedup set,
    and re-emits the MEDIA tag every turn.
    """
    paths: set = set()
    tool_name_by_call_id: Dict[str, str] = {}
    for msg in agent_history:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                cid = call.get("id") or call.get("call_id")
                fn = call.get("function") or {}
                name = str(fn.get("name") or call.get("name") or "")
                if cid and name:
                    tool_name_by_call_id[str(cid)] = name
    for msg in agent_history:
        if msg.get("role") not in {"tool", "function"}:
            continue
        content = str(msg.get("content", "") or "")
        if "MEDIA:" in content:
            for match in _TOOL_MEDIA_RE.finditer(content):
                p = match.group(1).strip().rstrip('",}')
                if p:
                    paths.add(p)
            continue
        cid = str(msg.get("tool_call_id") or msg.get("call_id") or "")
        if tool_name_by_call_id.get(cid) == "image_generate":
            try:
                payload = json.loads(content)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("success"):
                for field in _JSON_MEDIA_TOOL_PATH_FIELDS:
                    jp = payload.get(field)
                    if isinstance(jp, str) and jp:
                        paths.add(jp)
                        break
    return paths

# ---------------------------------------------------------------------------
# SSL certificate auto-detection for NixOS and other non-standard systems.
# Must run BEFORE any HTTP library (discord, aiohttp, etc.) is imported.
# ---------------------------------------------------------------------------
def _ensure_ssl_certs() -> None:
    """Set SSL_CERT_FILE if the system doesn't expose CA certs to Python.

    Windows startup paths (Desktop, Scheduled Tasks, installer children) can
    occasionally inherit a stale SSL_CERT_FILE. Returning just because the
    variable is present makes every later httpx/OpenAI client construction fail
    with FileNotFoundError from ssl.load_verify_locations(). Treat a missing
    path as unset and fall back to certifi instead.
    """
    configured_cert = os.environ.get("SSL_CERT_FILE")
    if configured_cert:
        if os.path.exists(configured_cert):
            return  # user already configured it to a real file
        logging.getLogger(__name__).warning(
            "Ignoring stale SSL_CERT_FILE=%r because the path does not exist",
            configured_cert,
        )
        os.environ.pop("SSL_CERT_FILE", None)

    import ssl

    # 1. Python's compiled-in defaults
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return

    # 2. certifi (ships its own Mozilla bundle)
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        return
    except ImportError:
        pass

    # 3. Common distro / macOS locations
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",               # Debian/Ubuntu/Gentoo
        "/etc/pki/tls/certs/ca-bundle.crt",                 # RHEL/CentOS 7
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", # RHEL/CentOS 8+
        "/etc/ssl/ca-bundle.pem",                            # SUSE/OpenSUSE
        "/etc/ssl/cert.pem",                                 # Alpine / macOS
        "/etc/pki/tls/cert.pem",                             # Fedora
        "/usr/local/etc/openssl@1.1/cert.pem",               # macOS Homebrew Intel
        "/opt/homebrew/etc/openssl@1.1/cert.pem",            # macOS Homebrew ARM
    ):
        if os.path.exists(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return

def _home_target_env_var(platform_name: str) -> str:
    """Return the configured home-target env var for a platform.

    Consults built-in ``_HOME_TARGET_ENV_VARS`` first, then the plugin
    registry via ``cron.scheduler._resolve_home_env_var``, then falls back
    to ``<PLATFORM>_HOME_CHANNEL`` for unknown names.
    """
    from cron.scheduler import _resolve_home_env_var

    resolved = _resolve_home_env_var(platform_name)
    if resolved:
        return resolved
    return f"{platform_name.upper()}_HOME_CHANNEL"


def _home_thread_env_var(platform_name: str) -> str:
    """Return the optional thread/topic env var for a platform home target."""
    return f"{_home_target_env_var(platform_name)}_THREAD_ID"


def _restart_notification_pending() -> bool:
    """Return True when a /restart completion marker is waiting to be delivered."""
    return (_hermes_home / ".restart_notify.json").exists()


def _planned_restart_notification_path() -> Path:
    return _hermes_home / ".restart_pending.json"


def _planned_restart_notification_pending() -> bool:
    """Return True when a non-chat planned restart should notify home channels."""
    return _planned_restart_notification_path().exists()


def _clear_planned_restart_notification() -> None:
    _planned_restart_notification_path().unlink(missing_ok=True)


# Mark this process as a gateway so cli.py's module-level load_cli_config()
# knows not to clobber TERMINAL_CWD if lazily imported.
os.environ["_HERMES_GATEWAY"] = "1"

_ensure_ssl_certs()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Resolve Hermes home directory (respects HERMES_HOME override)
from hermes_constants import get_hermes_home, get_hermes_home_override
from utils import atomic_json_write, atomic_yaml_write, base_url_host_matches, is_truthy_value
_hermes_home = get_hermes_home()

# Load environment variables from ~/.hermes/.env first.
# User-managed env files should override stale shell exports on restart.
from dotenv import load_dotenv  # noqa: F401  # backward-compat for tests that monkeypatch this symbol
from hermes_cli.env_loader import load_hermes_dotenv
_env_path = _hermes_home / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=Path(__file__).resolve().parents[1] / '.env')


def _reload_runtime_env_preserving_config_authority() -> None:
    """Reload .env for fresh credentials without letting stale .env override config.

    Gateway processes are long-lived, so per-turn code reloads ~/.hermes/.env to
    pick up rotated API keys. config.yaml remains authoritative for agent budget
    settings such as agent.max_turns; otherwise a stale HERMES_MAX_ITERATIONS in
    .env can replace the startup bridge on later turns.

    In multiplex mode this is a NO-OP for the credential reload: secrets come
    from the per-turn ``set_secret_scope`` (installed by ``_profile_runtime_scope``)
    which loads the routed profile's ``.env`` into an isolated mapping. Mutating
    the process-global ``os.environ`` here would defeat that isolation and leak
    the default profile's keys to every profile's turns and subprocesses.
    """
    from agent.secret_scope import is_multiplex_active
    if is_multiplex_active():
        # Credentials are resolved from the active profile's secret scope, not
        # os.environ. Still honor config.yaml's agent.max_turns bridge below
        # using the scoped home, but never reload .env into global env.
        _bridge_max_turns_from_config(_hermes_home)
        return

    load_hermes_dotenv(
        hermes_home=_hermes_home,
        project_env=Path(__file__).resolve().parents[1] / '.env',
    )
    _bridge_max_turns_from_config(_hermes_home)


def _bridge_max_turns_from_config(home: "Path") -> None:
    """Bridge config.yaml agent.max_turns into HERMES_MAX_ITERATIONS (a global)."""
    config_path = home / 'config.yaml'
    if not config_path.exists():
        return
    try:
        import yaml as _yaml
        with open(config_path, encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        from hermes_cli.config import _expand_env_vars
        cfg = _expand_env_vars(cfg)
        # Managed scope: keep administrator-pinned values authoritative on every
        # turn too. This per-turn reload re-bridges config→env, so without the
        # overlay a managed agent.max_turns / timezone / redact_secrets would be
        # replaced by the user's value after the first turn. Fail-open.
        try:
            from hermes_cli import managed_scope
            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            pass
    except Exception:
        return

    agent_cfg = cfg.get("agent", {})
    if isinstance(agent_cfg, dict) and "max_turns" in agent_cfg:
        os.environ["HERMES_MAX_ITERATIONS"] = str(agent_cfg["max_turns"])


def _current_max_iterations() -> int:
    """Return the current per-turn iteration budget after runtime env refresh."""
    _reload_runtime_env_preserving_config_authority()
    try:
        return int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
    except (TypeError, ValueError):
        return 90


from contextlib import contextmanager as _contextmanager


# Platforms that bind a host TCP port (HTTP/webhook listeners). In a profile
# multiplexer the default profile owns the single shared listener and serves
# every profile through the /p/<profile>/ URL prefix, so a SECONDARY profile
# enabling one of these is always a misconfiguration: it would try to bind a
# port already held by the default's listener. We hard-error on it rather than
# silently dropping the adapter (see _start_one_profile_adapters).
# Stored as platform .value strings since the Platform enum is imported below.
_PORT_BINDING_PLATFORM_VALUES = frozenset({
    "webhook",
    "api_server",
    "msgraph_webhook",
    "wecom_callback",
    "bluebubbles",
})




@_contextmanager
def _profile_runtime_scope(profile_home: "Path"):
    """Scope config/skills/memory AND credentials to a profile for one turn.

    Combines the two seams the multiplexer needs:
      1. ``set_hermes_home_override`` — redirects ``get_hermes_home()`` (config,
         skills, memory, SOUL, sessions) to the profile's home. Contextvar, so
         it propagates into the agent worker thread via ``copy_context()``.
      2. ``set_secret_scope`` — installs the profile's ``.env`` secrets as the
         authoritative credential source, so ``get_secret`` reads this profile's
         keys and never the process-global ``os.environ`` (which in a
         multiplexer may hold another profile's values).

    Only used on the multiplexed inbound path. Single-profile gateways never
    enter this scope, so their behavior is unchanged. Loading the profile's
    ``.env`` here does NOT mutate ``os.environ`` — ``build_profile_secret_scope``
    returns an isolated dict — which is what keeps subprocesses (MCP, kanban)
    from inheriting cross-profile secrets.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import (
        build_profile_secret_scope,
        set_secret_scope,
        reset_secret_scope,
    )

    home_token = set_hermes_home_override(str(profile_home))
    secret_token = set_secret_scope(build_profile_secret_scope(Path(profile_home)))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


_DOCKER_VOLUME_SPEC_RE = re.compile(r"^(?P<host>.+):(?P<container>/[^:]+?)(?::(?P<options>[^:]+))?$")
_DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS = {"/output", "/outputs"}

# Bridge config.yaml values into the environment so os.getenv() picks them up.
# config.yaml is authoritative for terminal settings — overrides .env.
_config_path = _hermes_home / 'config.yaml'
if _config_path.exists():
    try:
        import yaml as _yaml
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        # Expand ${ENV_VAR} references before bridging to env vars.
        from hermes_cli.config import _expand_env_vars
        _cfg = _expand_env_vars(_cfg)
        # Managed scope: overlay administrator-pinned values BEFORE bridging to
        # env vars, so a managed timezone / redact_secrets / max_turns / terminal
        # setting wins over the user's value at the env layer too. This bridge
        # reads config.yaml directly (not via load_config), so without the
        # overlay every HERMES_*/TERMINAL_* env var below would carry the user's
        # value even when an administrator pinned it. Fail-open via the helper.
        try:
            from hermes_cli import managed_scope
            _cfg = managed_scope.apply_managed_overlay(_cfg)
        except Exception:
            pass
        # Top-level simple values (fallback only — don't override .env)
        for _key, _val in _cfg.items():
            if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
                os.environ[_key] = str(_val)
        # Terminal config is nested — bridge to TERMINAL_* env vars.
        # config.yaml overrides .env for these since it's the documented config path.
        _terminal_cfg = _cfg.get("terminal", {})
        if _terminal_cfg and isinstance(_terminal_cfg, dict):
            _terminal_backend = str(
                _terminal_cfg.get("backend") or os.environ.get("TERMINAL_ENV") or ""
            ).strip().lower()
            _terminal_env_map = {
                "backend": "TERMINAL_ENV",
                "cwd": "TERMINAL_CWD",
                "timeout": "TERMINAL_TIMEOUT",
                "home_mode": "TERMINAL_HOME_MODE",
                "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
                "docker_image": "TERMINAL_DOCKER_IMAGE",
                "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
                "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
                "modal_image": "TERMINAL_MODAL_IMAGE",
                "daytona_image": "TERMINAL_DAYTONA_IMAGE",
                "ssh_host": "TERMINAL_SSH_HOST",
                "ssh_user": "TERMINAL_SSH_USER",
                "ssh_port": "TERMINAL_SSH_PORT",
                "ssh_key": "TERMINAL_SSH_KEY",
                "container_cpu": "TERMINAL_CONTAINER_CPU",
                "container_memory": "TERMINAL_CONTAINER_MEMORY",
                "container_disk": "TERMINAL_CONTAINER_DISK",
                "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
                "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
                "docker_env": "TERMINAL_DOCKER_ENV",
                "docker_extra_args": "TERMINAL_DOCKER_EXTRA_ARGS",
                "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
                "docker_network": "TERMINAL_DOCKER_NETWORK",
                "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
                "docker_persist_across_processes": "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
                "docker_orphan_reaper": "TERMINAL_DOCKER_ORPHAN_REAPER",
                "sandbox_dir": "TERMINAL_SANDBOX_DIR",
                "persistent_shell": "TERMINAL_PERSISTENT_SHELL",
            }
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    _val = _terminal_cfg[_cfg_key]
                    # Skip cwd placeholder values (".", "auto", "cwd") — the
                    # gateway resolves these to Path.home() later (line ~255).
                    # Writing the raw placeholder here would just be noise.
                    # Only bridge explicit absolute paths from config.yaml.
                    if _cfg_key == "cwd" and str(_val) in {".", "auto", "cwd"}:
                        continue
                    # Expand shell tilde in local/container cwd so subprocess.Popen
                    # never receives a literal "~/" which the kernel rejects.
                    # SSH cwd is interpreted by the remote shell, so preserve
                    # "~" / "~/..." for the SSH backend instead of expanding it
                    # to the Hermes host/container HOME (often /opt/data). Shared
                    # predicate with terminal_tool so the two sites can't drift.
                    if _cfg_key == "cwd" and isinstance(_val, str):
                        from tools.terminal_tool import _is_ssh_remote_tilde_cwd
                        if not _is_ssh_remote_tilde_cwd(_terminal_backend, _val.strip()):
                            _val = os.path.expanduser(_val)
                    if isinstance(_val, (list, dict)):
                        os.environ[_env_var] = json.dumps(_val)
                    else:
                        os.environ[_env_var] = str(_val)
        # Compression config is read directly from config.yaml by run_agent.py
        # and auxiliary_client.py — no env var bridging needed.
        # Auxiliary model/direct-endpoint overrides (vision, web_extract,
        # approval, plus any plugin-registered auxiliary tasks).
        # Each task has provider/model/base_url/api_key; bridge non-default
        # values to env vars named AUXILIARY_<KEY_UPPER>_*. The legacy
        # hard-coded list (vision/web_extract/approval) is replaced by a
        # dynamic loop so plugin-registered tasks benefit from the same
        # config→env bridging without core knowing about each one.
        _auxiliary_cfg = _cfg.get("auxiliary", {})
        if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
            # Built-in tasks that previously had explicit env-var bridging.
            # Kept here as the canonical bridged set; plugin tasks are added
            # below via the plugin auxiliary registry.
            _aux_bridged_keys = {"vision", "web_extract", "approval"}
            try:
                from hermes_cli.plugins import get_plugin_auxiliary_tasks
                for _entry in get_plugin_auxiliary_tasks():
                    _aux_bridged_keys.add(_entry["key"])
            except Exception:
                # Plugin discovery failure must not break gateway startup;
                # built-in bridging stays intact.
                pass

            for _task_key in _aux_bridged_keys:
                _task_cfg = _auxiliary_cfg.get(_task_key, {})
                if not isinstance(_task_cfg, dict):
                    continue
                _prov = str(_task_cfg.get("provider", "")).strip()
                _model = str(_task_cfg.get("model", "")).strip()
                _base_url = str(_task_cfg.get("base_url", "")).strip()
                _api_key = str(_task_cfg.get("api_key", "")).strip()
                _upper = _task_key.upper()
                if _prov and _prov != "auto":
                    os.environ[f"AUXILIARY_{_upper}_PROVIDER"] = _prov
                if _model:
                    os.environ[f"AUXILIARY_{_upper}_MODEL"] = _model
                if _base_url:
                    os.environ[f"AUXILIARY_{_upper}_BASE_URL"] = _base_url
                if _api_key:
                    os.environ[f"AUXILIARY_{_upper}_API_KEY"] = _api_key
        # config.yaml is the documented, authoritative source for these
        # settings — it unconditionally wins over .env values. Previously
        # the guards below read `if X not in os.environ` and let stale
        # .env entries (e.g. HERMES_MAX_ITERATIONS=60 written by an old
        # `hermes setup` run) silently shadow the user's current config.
        # See PR #18413 / the 60-vs-500 max_turns incident.
        _agent_cfg = _cfg.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "max_turns" in _agent_cfg:
                os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
            if "gateway_timeout" in _agent_cfg:
                os.environ["HERMES_AGENT_TIMEOUT"] = str(_agent_cfg["gateway_timeout"])
            if "gateway_timeout_warning" in _agent_cfg:
                os.environ["HERMES_AGENT_TIMEOUT_WARNING"] = str(_agent_cfg["gateway_timeout_warning"])
            if "gateway_notify_interval" in _agent_cfg:
                os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = str(_agent_cfg["gateway_notify_interval"])
            if "restart_drain_timeout" in _agent_cfg:
                os.environ["HERMES_RESTART_DRAIN_TIMEOUT"] = str(_agent_cfg["restart_drain_timeout"])
            if "gateway_auto_continue_freshness" in _agent_cfg:
                os.environ["HERMES_AUTO_CONTINUE_FRESHNESS"] = str(
                    _agent_cfg["gateway_auto_continue_freshness"]
                )
        _display_cfg = _cfg.get("display", {})
        if _display_cfg and isinstance(_display_cfg, dict):
            if "busy_input_mode" in _display_cfg:
                os.environ["HERMES_GATEWAY_BUSY_INPUT_MODE"] = str(_display_cfg["busy_input_mode"])
            if "busy_text_mode" in _display_cfg:
                os.environ["HERMES_GATEWAY_BUSY_TEXT_MODE"] = str(_display_cfg["busy_text_mode"])
            if "busy_ack_enabled" in _display_cfg:
                os.environ["HERMES_GATEWAY_BUSY_ACK_ENABLED"] = str(_display_cfg["busy_ack_enabled"])
            # This process-level env var is documented as an override for
            # service managers, so preserve it when already set. Other display
            # bridges stay config-authoritative for backwards compatibility.
            if (
                "busy_steer_ack_enabled" in _display_cfg
                and "HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED" not in os.environ
            ):
                os.environ["HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED"] = str(
                    _display_cfg["busy_steer_ack_enabled"]
                )
        # Timezone: bridge config.yaml → HERMES_TIMEZONE env var.
        _tz_cfg = _cfg.get("timezone", "")
        if _tz_cfg and isinstance(_tz_cfg, str):
            os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
        # Security settings
        _security_cfg = _cfg.get("security", {})
        if isinstance(_security_cfg, dict):
            _redact = _security_cfg.get("redact_secrets")
            if _redact is not None:
                os.environ["HERMES_REDACT_SECRETS"] = str(_redact).lower()
        # Gateway settings (media delivery allowlist + recency trust + strict mode)
        _gateway_cfg = _cfg.get("gateway", {})
        if isinstance(_gateway_cfg, dict):
            _strict = _gateway_cfg.get("strict")
            if _strict is not None:
                os.environ["HERMES_MEDIA_DELIVERY_STRICT"] = (
                    "1" if _strict else "0"
                )
            _allow_dirs = _gateway_cfg.get("media_delivery_allow_dirs")
            if _allow_dirs:
                if isinstance(_allow_dirs, str):
                    _allow_dirs_str = _allow_dirs
                elif isinstance(_allow_dirs, (list, tuple)):
                    _allow_dirs_str = os.pathsep.join(str(p) for p in _allow_dirs if p)
                else:
                    _allow_dirs_str = ""
                if _allow_dirs_str:
                    os.environ["HERMES_MEDIA_ALLOW_DIRS"] = _allow_dirs_str
            _trust_recent = _gateway_cfg.get("trust_recent_files")
            if _trust_recent is not None:
                os.environ["HERMES_MEDIA_TRUST_RECENT_FILES"] = (
                    "1" if _trust_recent else "0"
                )
            _trust_recent_seconds = _gateway_cfg.get("trust_recent_files_seconds")
            if _trust_recent_seconds is not None:
                os.environ["HERMES_MEDIA_TRUST_RECENT_SECONDS"] = str(_trust_recent_seconds)
            # Bridge gateway.platform_connect_timeout → the internal env var the
            # connect path + Discord adapter ready-wait both read (#19776).
            # Unlike the agent.*/display.* bridges above (config-authoritative),
            # this env var is the manual-override escape hatch, so it WINS if
            # already set explicitly; otherwise config.yaml supplies the value.
            if (
                "platform_connect_timeout" in _gateway_cfg
                and not os.environ.get("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
            ):
                os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] = str(
                    _gateway_cfg["platform_connect_timeout"]
                )
    except Exception as _bridge_err:
        # Previously this was silent (`except Exception: pass`), which
        # hid partial bridge failures and let .env defaults shadow
        # config.yaml values — users observed max_turns=500 in config
        # but a 60-iteration cap in practice. Surface the failure to
        # stderr so operators see it even though `logger` is not yet
        # initialized at module-import time (logger is defined further
        # down this module).
        print(
            f"  Warning: config.yaml → env bridge failed: "
            f"{type(_bridge_err).__name__}: {_bridge_err}",
            file=sys.stderr,
        )
        print(
            "  Gateway will fall back to .env values, which may not match "
            "your current config.yaml. Run `hermes doctor` to investigate.",
            file=sys.stderr,
        )

# Apply IPv4 preference if configured (before any HTTP clients are created).
try:
    from hermes_constants import apply_ipv4_preference
    _network_cfg = (_cfg if '_cfg' in dir() else {}).get("network", {})
    if isinstance(_network_cfg, dict) and _network_cfg.get("force_ipv4"):
        apply_ipv4_preference(force=True)
except Exception as _bootstrap_exc:
    print(f"  Warning: IPv4 preference application failed: {_bootstrap_exc}", file=sys.stderr)

# Validate config structure early — log warnings so gateway operators see problems
try:
    from hermes_cli.config import print_config_warnings
    print_config_warnings()
except Exception as _bootstrap_exc:
    print(f"  Warning: config validation failed: {_bootstrap_exc}", file=sys.stderr)

# Warn if user has deprecated MESSAGING_CWD / TERMINAL_CWD in .env
try:
    from hermes_cli.config import warn_deprecated_cwd_env_vars
    warn_deprecated_cwd_env_vars()
except Exception as _bootstrap_exc:
    print(f"  Warning: deprecation check failed: {_bootstrap_exc}", file=sys.stderr)

# Gateway runs in quiet mode - suppress debug output and use cwd directly (no temp dirs)
os.environ["HERMES_QUIET"] = "1"

# Enable interactive exec approval for dangerous commands on messaging platforms
os.environ["HERMES_EXEC_ASK"] = "1"

# Set terminal working directory for messaging platforms.
# config.yaml terminal.cwd is the canonical source (bridged to TERMINAL_CWD
# by the config bridge above).  Placeholder values are resolved per-backend —
# see gateway/cwd_placeholder.py for the three-case contract (local vs docker
# mount-off vs docker mount-on).  MESSAGING_CWD is a backward-compat fallback.
from gateway.cwd_placeholder import CWD_PLACEHOLDERS, resolve_placeholder_terminal_cwd

_configured_cwd = os.environ.get("TERMINAL_CWD", "")
if not _configured_cwd or _configured_cwd in CWD_PLACEHOLDERS:
    _resolved_cwd = resolve_placeholder_terminal_cwd(
        configured_cwd=_configured_cwd,
        terminal_backend=os.environ.get("TERMINAL_ENV", ""),
        messaging_cwd=os.getenv("MESSAGING_CWD"),
        docker_mount_cwd_to_workspace=os.getenv(
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false"
        ).lower()
        in {"true", "1", "yes"},
        home_fallback=str(Path.home()),
    )
    if _resolved_cwd is None:
        os.environ.pop("TERMINAL_CWD", None)
    else:
        os.environ["TERMINAL_CWD"] = _resolved_cwd

from gateway.config import (
    ChannelOverride,
    Platform,
    _BUILTIN_PLATFORM_VALUES,
    GatewayConfig,
    HomeChannel,
    PlatformConfig,
    load_gateway_config,
)
from gateway.session import (
    SessionStore,
    SessionSource,
    SessionContext,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
    is_shared_multi_user_session,
)
from gateway.delivery import DeliveryRouter, looks_like_telegram_private_chat_id
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.slash_commands import GatewaySlashCommandsMixin
from gateway.platforms.base import (
    BasePlatformAdapter,
    EphemeralReply,
    MessageEvent,
    MessageType,
    _prefix_within_utf16_limit,
    _reply_anchor_for_event,
    merge_pending_message_event,
    utf16_len,
)
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_FATAL_CONFIG_EXIT_CODE,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_drain_timeout,
)


from gateway.whatsapp_identity import (
    canonical_whatsapp_identifier as _canonical_whatsapp_identifier,  # noqa: F401
    expand_whatsapp_aliases as _expand_whatsapp_auth_aliases,
    normalize_whatsapp_identifier as _normalize_whatsapp_identifier,
)


logger = logging.getLogger(__name__)


_OWN_POLICY_OPEN_ENV = {
    Platform.WEIXIN: ("WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY", "WEIXIN_ALLOW_ALL_USERS"),
    Platform.QQBOT: (None, None, "QQ_ALLOW_ALL_USERS"),
}


def _own_policy_open_startup_violation(config) -> Optional[str]:
    """Return a startup-abort reason when open policy lacks allow-all opt-in."""
    for platform, platform_config in getattr(config, "platforms", {}).items():
        if not getattr(platform_config, "enabled", False):
            continue
        open_env = _OWN_POLICY_OPEN_ENV.get(platform)
        if not open_env:
            continue
        dm_env, group_env, allow_all_env = open_env
        extra = getattr(platform_config, "extra", None) or {}
        dm_policy = str(
            extra.get("dm_policy")
            or (os.getenv(dm_env, "pairing") if dm_env else "pairing")
        ).strip().lower()
        group_policy = str(
            extra.get("group_policy")
            or (os.getenv(group_env, "pairing") if group_env else "pairing")
        ).strip().lower()
        if dm_policy != "open" and group_policy != "open":
            continue
        gateway_allow_all = os.getenv(
            "GATEWAY_ALLOW_ALL_USERS", ""
        ).lower() in {"true", "1", "yes"}
        platform_opted_in = gateway_allow_all or (
            allow_all_env
            and os.getenv(allow_all_env, "").lower() in {"true", "1", "yes"}
        )
        if platform_opted_in:
            continue
        return f"{platform.value}: open policy without allow-all opt-in"
    return None


# Sentinel placed into _running_agents immediately when a session starts
# processing, *before* any await.  Prevents a second message for the same
# session from bypassing the "already running" guard during the async gap
# between the guard check and actual agent creation.
_AGENT_PENDING_SENTINEL = object()


def _resolve_runtime_agent_kwargs() -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances.

    Provider is read from ``config.yaml`` ``model.provider`` (the single
    source of truth). ``resolve_runtime_provider()`` falls through to env
    var lookups internally for legacy compatibility, but the gateway does
    not consult environment variables for behavioral config — config.yaml
    is authoritative.

    If the primary provider fails with an authentication error, attempt to
    resolve credentials using the fallback provider chain from config.yaml
    before giving up.
    """
    from hermes_cli.runtime_provider import (
        resolve_runtime_provider,
        format_runtime_provider_error,
        _get_model_config,
    )
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error

    try:
        runtime = resolve_runtime_provider()
    except AuthError as auth_exc:
        # Distinguish a transient rate-limit/quota cap (credentials are fine,
        # re-auth cannot help) from a genuine auth failure (expired/revoked
        # token). Both fall through to the fallback chain, but the log message
        # must not mislabel a quota exhaustion as an auth failure (#32790).
        if is_rate_limited_auth_error(auth_exc):
            logger.warning("Primary provider rate-limited (429): %s — trying fallback", auth_exc)
        else:
            logger.warning("Primary provider auth failed: %s — trying fallback", auth_exc)
        fb_config = _try_resolve_fallback_provider()
        if fb_config is not None:
            return fb_config
        raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    model_cfg = _get_model_config()
    max_tokens = None
    _env_mt = os.environ.get("HERMES_MAX_TOKENS")
    if _env_mt:
        try:
            max_tokens = int(_env_mt)
        except (ValueError, TypeError):
            max_tokens = None
    elif isinstance(model_cfg, dict):
        mt = model_cfg.get("max_tokens")
        if isinstance(mt, int):
            max_tokens = mt
    # Fall back to a per-provider output cap (custom_providers max_output_tokens)
    # only when the documented global model.max_tokens isn't set, so the global
    # key always wins.
    if max_tokens is None:
        _runtime_mot = runtime.get("max_output_tokens")
        if isinstance(_runtime_mot, int) and _runtime_mot > 0:
            max_tokens = _runtime_mot

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
        "max_tokens": max_tokens,
    }


def _resolve_runtime_agent_kwargs_for_provider(provider: str) -> dict:
    """Resolve runtime credentials for a specific provider (e.g. from channel override)."""
    from hermes_cli.runtime_provider import (
        resolve_runtime_provider,
        format_runtime_provider_error,
    )
    try:
        runtime = resolve_runtime_provider(requested=provider)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc
    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
    }


def _credential_pool_for_provider(provider: Optional[str]):
    """Return the live credential pool for a provider id (e.g. ``custom:hyper``)."""
    if not provider or not str(provider).strip():
        return None
    try:
        return _resolve_runtime_agent_kwargs_for_provider(str(provider).strip()).get(
            "credential_pool"
        )
    except Exception:
        logger.debug(
            "Failed to resolve credential pool for provider=%s",
            provider,
            exc_info=True,
        )
        return None


def _try_resolve_fallback_provider() -> dict | None:
    """Attempt to resolve credentials from the fallback_model/fallback_providers config."""
    from hermes_cli.runtime_provider import resolve_runtime_provider
    try:
        import yaml as _y
        cfg_path = _hermes_home / "config.yaml"
        if not cfg_path.exists():
            return None
        with open(cfg_path, encoding="utf-8") as _f:
            cfg = _y.safe_load(_f) or {}
        fb_list = get_fallback_chain(cfg)
        if not fb_list:
            return None
        for entry in fb_list:
            try:
                explicit_api_key = entry.get("api_key")
                if not explicit_api_key:
                    key_env = str(
                        entry.get("key_env") or entry.get("api_key_env") or ""
                    ).strip()
                    if key_env:
                        explicit_api_key = os.getenv(key_env, "").strip() or None
                runtime = resolve_runtime_provider(
                    requested=entry.get("provider"),
                    explicit_base_url=entry.get("base_url"),
                    explicit_api_key=explicit_api_key,
                )
                # Log the literal `provider` key from config, not the resolved
                # runtime category — an Ollama fallback resolves through the
                # OpenAI-compatible path and would otherwise be logged as
                # "openrouter", contradicting the operator's config (#32790).
                logger.info(
                    "Fallback provider resolved: %s model=%s",
                    entry.get("provider") or runtime.get("provider"),
                    entry.get("model"),
                )
                return {
                    "api_key": runtime.get("api_key"),
                    "base_url": runtime.get("base_url"),
                    "provider": runtime.get("provider"),
                    "api_mode": runtime.get("api_mode"),
                    "command": runtime.get("command"),
                    "args": list(runtime.get("args") or []),
                    "credential_pool": runtime.get("credential_pool"),
                    "model": entry.get("model"),
                }
            except Exception as fb_exc:
                logger.debug("Fallback entry %s failed: %s", entry.get("provider"), fb_exc)
                continue
    except Exception:
        pass
    return None


def _event_media_type_at(event, index: int) -> str:
    """Return the per-attachment MIME for the attachment at *index*.

    Empty string when the platform didn't populate a per-file MIME for
    that slot (some adapters only set a message-level type).
    """
    media_types = getattr(event, "media_types", None) or []
    return media_types[index] if index < len(media_types) else ""


def _event_media_is_image(event, index: int) -> bool:
    """True if the attachment at *index* is an image.

    Trust the per-attachment MIME when present. Only fall back to the
    message-level ``PHOTO`` type when this attachment's MIME is unknown --
    otherwise a document (or any non-image) uploaded alongside an image in
    the same message gets mis-routed as an image, base64'd into a vision
    content part, and the provider 400s ("Could not process image").
    """
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith("image/")
    return getattr(event, "message_type", None) == MessageType.PHOTO


def _event_media_is_audio(event, index: int) -> bool:
    """True if the attachment at *index* is audio (per-attachment MIME first)."""
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith("audio/")
    return getattr(event, "message_type", None) in {MessageType.VOICE, MessageType.AUDIO}


def _event_media_is_video(event, index: int) -> bool:
    """True if the attachment at *index* is video (per-attachment MIME first)."""
    mtype = _event_media_type_at(event, index)
    if mtype:
        return mtype.startswith("video/")
    return getattr(event, "message_type", None) == MessageType.VIDEO


def _build_media_placeholder(event) -> str:
    """Build a text placeholder for media-only events so they aren't dropped.

    When a photo/document is queued during active processing and later
    dequeued, only .text is extracted.  If the event has no caption,
    the media would be silently lost.  This builds a placeholder that
    the vision enrichment pipeline will replace with a real description.
    """
    parts = []
    media_urls = getattr(event, "media_urls", None) or []
    for i, url in enumerate(media_urls):
        if _event_media_is_image(event, i):
            parts.append(f"[User sent an image: {url}]")
        elif _event_media_is_audio(event, i):
            parts.append(f"[User sent audio: {url}]")
        elif _event_media_is_video(event, i):
            parts.append(f"[User sent a video: {url}]")
        else:
            parts.append(f"[User sent a file: {url}]")
    return "\n".join(parts)


def _build_document_context_note(display_name: str, agent_path: str, mtype: str) -> str:
    """Context note prepended to a user turn when they attach a document.

    Text documents (``text/*``) have their content inlined upstream by the
    platform adapter, so the note just confirms that and records the path.

    Binary documents (PDF, DOCX, XLSX, …) cannot be inlined as text. The note
    must tell the agent to *extract* the text itself before answering — earlier
    wording ("Ask the user what they'd like you to do with it") steered the
    model into punting back to the user, which is why attached PDFs/DOCX looked
    "unreadable" to the agent even though it has the tools to read them.
    """
    if mtype.startswith("text/"):
        return (
            f"[The user sent a text document: '{display_name}'. "
            f"Its content has been included below. "
            f"The file is also saved at: {agent_path}]"
        )
    return (
        f"[The user sent a document: '{display_name}'. It is saved at: {agent_path}. "
        f"Its text is not inlined here (it's a binary format such as PDF or DOCX). "
        f"To read it, extract the document's text yourself — for example with the "
        f"terminal tool or the ocr-and-documents skill — before answering, instead "
        f"of asking the user to paste the contents.]"
    )


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def _probe_audio_duration(path: str) -> Optional[str]:
    """Best-effort duration probe. Returns formatted MM:SS / HH:MM:SS, or None on failure."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".wav":
        try:
            def _wav_duration() -> float:
                import wave
                with wave.open(path, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    return frames / float(rate)
            secs = await asyncio.to_thread(_wav_duration)
            return _format_duration(secs)
        except Exception:
            pass

    if ext in (".ogg", ".opus", ".oga"):
        try:
            def _ogg_duration() -> float:
                from mutagen.oggopus import OggOpus
                return float(OggOpus(path).info.length)
            secs = await asyncio.to_thread(_ogg_duration)
            return _format_duration(secs)
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            return _format_duration(float(stdout.decode().strip()))
    except Exception:
        pass

    return None


def _dequeue_pending_event(adapter, session_key: str) -> MessageEvent | None:
    """Consume and return the full pending event for a session.

    Queued follow-ups must preserve their media metadata so they can re-enter
    the normal image/STT/document preprocessing path instead of being reduced
    to a placeholder string.
    """
    return adapter.get_pending_message(session_key)


_INTERRUPT_REASON_STOP = "Stop requested"
_INTERRUPT_REASON_RESET = "Session reset requested"
_INTERRUPT_REASON_TIMEOUT = "Execution timed out (inactivity)"
_INTERRUPT_REASON_SSE_DISCONNECT = "SSE client disconnected"
_INTERRUPT_REASON_GATEWAY_SHUTDOWN = "Gateway shutting down"
_INTERRUPT_REASON_GATEWAY_RESTART = "Gateway restarting"

_CONTROL_INTERRUPT_MESSAGES = frozenset(
    {
        _INTERRUPT_REASON_STOP.lower(),
        _INTERRUPT_REASON_RESET.lower(),
        _INTERRUPT_REASON_TIMEOUT.lower(),
        _INTERRUPT_REASON_SSE_DISCONNECT.lower(),
        _INTERRUPT_REASON_GATEWAY_SHUTDOWN.lower(),
        _INTERRUPT_REASON_GATEWAY_RESTART.lower(),
    }
)


def _is_control_interrupt_message(message: Optional[str]) -> bool:
    """Return True when an interrupt message is internal control flow."""
    if not message:
        return False
    normalized = " ".join(str(message).strip().split()).lower()
    return normalized in _CONTROL_INTERRUPT_MESSAGES


def _skill_slug_from_frontmatter(skill_md: Path) -> tuple[str | None, str | None]:
    """Derive the /command slug and declared frontmatter name from a SKILL.md.

    Matches the exact normalization used by
    :func:`agent.skill_commands.scan_skill_commands` so the slug here is the
    same string a user types after the leading ``/`` (e.g. a skill with
    frontmatter ``name: Stable Diffusion Image Generation`` resolves to
    ``stable-diffusion-image-generation`` — NOT the parent directory name,
    which is commonly shorter/different, e.g. ``stable-diffusion``).

    Using the directory name silently broke :func:`_check_unavailable_skill`
    for every skill whose directory name drifted from its frontmatter name
    (19 such skills on a standard install as of 2026-05), causing a generic
    "unknown command" response where a "disabled — enable with …" or
    "not installed — install with …" hint was expected.

    Returns ``(slug, declared_name)`` or ``(None, None)`` when the file
    can't be read or lacks a ``name:`` in its frontmatter.
    """
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None, None
    if not content.startswith("---"):
        return None, None
    end = content.find("\n---", 3)
    if end < 0:
        return None, None
    declared_name: str | None = None
    for line in content[3:end].splitlines():
        line = line.strip()
        if line.startswith("name:"):
            raw = line.split(":", 1)[1].strip()
            # Strip YAML quote wrappers if present
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
                raw = raw[1:-1]
            declared_name = raw.strip()
            break
    if not declared_name:
        return None, None
    slug = declared_name.lower().replace(" ", "-").replace("_", "-")
    # Mirror _SKILL_INVALID_CHARS and _SKILL_MULTI_HYPHEN from skill_commands
    import re as _re
    slug = _re.sub(r"[^a-z0-9-]", "", slug)
    slug = _re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return None, declared_name
    return slug, declared_name


def _check_unavailable_skill(command_name: str) -> str | None:
    """Check if a command matches a known-but-inactive skill.

    Returns a helpful message if the skill exists but is disabled or only
    available as an optional install. Returns None if no match found.

    The slug for each on-disk skill is derived from its frontmatter ``name:``
    (via :func:`_skill_slug_from_frontmatter`), NOT from its containing
    directory name — because the two can differ (e.g. directory
    ``stable-diffusion`` + frontmatter ``Stable Diffusion Image Generation``
    yields slug ``stable-diffusion-image-generation``). Matching on
    directory name would miss that slug entirely and fall through to the
    generic "unknown command" path.
    """
    # Normalize: command uses hyphens, skill names may use hyphens or underscores
    normalized = command_name.lower().replace("_", "-")
    try:
        from tools.skills_tool import _get_disabled_skill_names
        from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
        disabled = _get_disabled_skill_names()

        # Check disabled skills across all dirs (local + external)
        for skills_dir in get_all_skills_dirs():
            if not skills_dir.exists():
                continue
            for skill_md in skills_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, declared_name = _skill_slug_from_frontmatter(skill_md)
                if not slug or not declared_name:
                    continue
                # disabled is keyed by the declared frontmatter name (what
                # skills.disabled / skills.platform_disabled store).
                if slug == normalized and declared_name in disabled:
                    return (
                        f"The **{command_name}** skill is installed but disabled.\n"
                        f"Enable it with: `hermes skills config`"
                    )

        # Check optional skills (shipped with repo but not installed)
        from hermes_constants import get_optional_skills_dir
        repo_root = Path(__file__).resolve().parent.parent
        optional_dir = get_optional_skills_dir(repo_root / "optional-skills")
        if optional_dir.exists():
            for skill_md in optional_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, _declared = _skill_slug_from_frontmatter(skill_md)
                if not slug:
                    continue
                if slug == normalized:
                    # Build install path: official/<category>/<name>
                    rel = skill_md.parent.relative_to(optional_dir)
                    parts = list(rel.parts)
                    install_path = f"official/{'/'.join(parts)}"
                    return (
                        f"The **{command_name}** skill is available but not installed.\n"
                        f"Install it with: `hermes skills install {install_path}`"
                    )
    except Exception:
        pass
    return None


def _platform_config_key(platform: "Platform") -> str:
    """Map a Platform enum to its config.yaml key (LOCAL→"cli", rest→enum value)."""
    return "cli" if platform == Platform.LOCAL else platform.value


def _teams_pipeline_plugin_enabled() -> bool:
    """Return True when the standalone Teams pipeline plugin is enabled."""
    config = _load_gateway_config()
    enabled = cfg_get(config, "plugins", "enabled", default=[])
    if not isinstance(enabled, list):
        return False
    return "teams_pipeline" in enabled or "teams-pipeline" in enabled


def _gateway_config_home() -> Path:
    """Return the Hermes home that gateway config reads should use."""
    override = get_hermes_home_override()
    if override:
        return Path(override)
    return _hermes_home


def _load_gateway_config() -> dict:
    """Load and parse ~/.hermes/config.yaml, returning {} on any error.

    Uses the module-level ``_hermes_home`` (so tests that monkeypatch it
    still see their fixture) and shares the mtime-keyed raw-yaml cache
    from ``hermes_cli.config.read_raw_config`` when the paths match.

    Managed scope is overlaid on the result (via the shared helper) so the
    gateway honors administrator-pinned values — neither read_raw_config nor a
    direct yaml.safe_load carries the managed merge on its own. Fail-open.
    """
    config_home = _gateway_config_home()
    config_path = config_home / 'config.yaml'
    raw: dict = {}
    used_canonical = False
    try:
        from hermes_cli.config import get_config_path, read_raw_config
        # Fast path: if _hermes_home agrees with the canonical config
        # location, reuse the shared cache. Otherwise fall through to a
        # direct read (keeps test fixtures with a monkeypatched
        # _hermes_home working).
        if config_path == get_config_path():
            raw = read_raw_config()
            used_canonical = True
    except Exception:
        pass

    if not used_canonical:
        try:
            if config_path.exists():
                import yaml
                with open(config_path, 'r', encoding='utf-8') as f:
                    raw = yaml.safe_load(f) or {}
        except Exception:
            logger.debug("Could not load gateway config from %s", config_path)
            raw = {}

    # Overlay managed scope. read_raw_config() returns the user's raw YAML
    # WITHOUT the managed merge (that lives in load_config/_load_config_impl),
    # so the overlay is required on both paths for the gateway to honor pinned
    # values. Helper is fail-open and a no-op when no managed scope exists.
    try:
        from hermes_cli import managed_scope
        raw = managed_scope.apply_managed_overlay(raw if isinstance(raw, dict) else {})
    except Exception:
        pass
    if not isinstance(raw, dict):
        return {}
    # Canonicalize model-id aliases (model.name / model.model → model.default)
    # and migrate stale root-level provider/base_url into the model section.
    # The gateway bypasses load_config() (it reads raw YAML for speed), so the
    # normalization that load_config() applies must be replayed here or the
    # gateway would resolve an empty model for ``model: {name: <id>}`` configs
    # while the CLI resolves it correctly. See issue #34500. Fail-open.
    try:
        from hermes_cli.config import _normalize_root_model_keys
        raw = _normalize_root_model_keys(raw)
    except Exception:
        pass
    return raw


def _load_gateway_runtime_config() -> dict:
    """Load gateway config for runtime reads, expanding supported ``${VAR}`` refs.

    Runtime helpers should honor the same env-template expansion documented for
    ``config.yaml`` while still respecting tests that monkeypatch
    ``gateway.run._hermes_home``. Build on ``_load_gateway_config()`` rather
    than calling the canonical loader directly so both behaviors stay aligned.

    Expansion failures are intentionally NOT swallowed — silently returning
    the unexpanded dict would mask the very bug this helper exists to fix.
    """
    cfg = _load_gateway_config()
    if not isinstance(cfg, dict) or not cfg:
        return {}
    from hermes_cli.config import _expand_env_vars

    expanded = _expand_env_vars(cfg)
    return expanded if isinstance(expanded, dict) else {}


def _resolve_gateway_model(config: dict | None = None) -> str:
    """Read model from config.yaml — single source of truth.

    Without this, temporary AIAgent instances (e.g. /compress) fall
    back to the hardcoded default which fails when the active provider is
    openai-codex.
    """
    cfg = config if config is not None else _load_gateway_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, str):
        return model_cfg
    elif isinstance(model_cfg, dict):
        return model_cfg.get("default") or model_cfg.get("model") or ""
    return ""


def _channel_override_lookup_keys(
    chat_id: str,
    *,
    thread_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> list[str]:
    """Ordered, de-duplicated keys for ``channel_overrides`` lookup.

    Matches ``resolve_channel_prompt`` semantics: exact thread/channel id first,
    then parent channel/forum id (Discord threads inherit parent overrides).
    """
    keys: list[str] = []
    seen: set[str] = set()
    for key in (chat_id, thread_id, parent_id):
        if not key:
            continue
        sk = str(key)
        if sk in seen:
            continue
        seen.add(sk)
        keys.append(sk)
    return keys


def _get_channel_override(
    config: GatewayConfig,
    platform: Platform,
    chat_id: str,
    *,
    thread_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[ChannelOverride]:
    """Return per-channel override for this platform/chat_id, or None.

    Looks up ``channel_overrides`` by ``chat_id``, then ``thread_id``, then
    ``parent_id`` (forum threads / child channels inherit the parent entry).
    """
    platforms = getattr(config, "platforms", None)
    if not platforms:
        return None
    platform_config = platforms.get(platform)
    if not platform_config or not platform_config.channel_overrides:
        return None
    overrides = platform_config.channel_overrides
    for key in _channel_override_lookup_keys(
        chat_id, thread_id=thread_id, parent_id=parent_id
    ):
        ov = overrides.get(key)
        if ov is not None:
            return ov
    return None


def _resolve_hermes_bin() -> Optional[list[str]]:
    """Resolve the Hermes update command as argv parts.

    Tries in order:
    1. ``shutil.which("hermes")`` — standard PATH lookup
    2. ``sys.executable -m hermes_cli.main`` — fallback when Hermes is running
       from a venv/module invocation and the ``hermes`` shim is not on PATH

    Returns argv parts ready for quoting/joining, or ``None`` if neither works.
    """
    import shutil

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        return [hermes_bin]

    try:
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is not None:
            return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        pass

    return None


def _parse_session_key(session_key: str) -> "dict | None":
    """Parse a session key into its component parts.

    Session keys follow the format
    ``agent:main:{platform}:{chat_type}:{chat_id}[:{extra}...]``.
    Returns a dict with ``platform``, ``chat_type``, ``chat_id``, and
    optionally ``thread_id`` keys, or None if the key doesn't match.

    The 6th element is only returned as ``thread_id`` for chat types where
    it is unambiguous (``dm`` and ``thread``).  For group/channel sessions
    the suffix may be a user_id (per-user isolation) rather than a
    thread_id, so we leave ``thread_id`` out to avoid mis-routing.
    """
    parts = session_key.split(":")
    if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main":
        result = {
            "platform": parts[2],
            "chat_type": parts[3],
            "chat_id": parts[4],
        }
        if len(parts) > 5 and parts[3] in {"dm", "thread"}:
            result["thread_id"] = parts[5]
        return result
    return None


def _format_gateway_process_notification(evt: dict) -> "str | None":
    """Format a watch pattern event from completion_queue into a [IMPORTANT:] message."""
    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[IMPORTANT: {evt.get('message', '')}]"

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f"watch pattern \"{_pat}\".\n"
            f"Command: {_cmd}\n"
            f"Matched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    if evt_type == "async_delegation":
        # Reuse the shared rich formatter (self-contained task-source block).
        from tools.process_registry import format_process_notification
        return format_process_notification(evt)

    return None


def _drain_gateway_watch_events(completion_queue) -> "list[dict]":
    """Drain gateway-owned watch events without spinning on requeued events.

    Watch events are handled by the post-turn gateway drain. Process
    completions are owned by their per-process watcher task, and async
    delegation completions are owned by ``_async_delegation_watcher``.
    Requeueing async events inside ``while not queue.empty()`` would make the
    loop non-terminating, so detach the current batch first, then requeue any
    events this drain does not own after the queue is empty.
    """
    watch_events: list[dict] = []
    requeue: list[dict] = []
    while not completion_queue.empty():
        try:
            evt = completion_queue.get_nowait()
        except Exception:
            break
        evt_type = evt.get("type", "completion")
        if evt_type in {"watch_match", "watch_disabled"}:
            watch_events.append(evt)
        elif evt_type == "async_delegation":
            requeue.append(evt)
        # else: process completion events are handled by the watcher task
    for evt in requeue:
        completion_queue.put(evt)
    return watch_events


# Module-level weak reference to the active GatewayRunner instance.
# Used by tools (e.g. send_message) that need to route through a live
# adapter for plugin platforms.  Set in GatewayRunner.__init__().
import weakref as _weakref
_gateway_runner_ref: _weakref.ref = lambda: None


def _normalize_empty_agent_response(
    agent_result: dict,
    response: str,
    *,
    history_len: int = 0,
) -> str:
    """Normalize empty/None agent responses into user-facing messages.

    Consolidates the existing ``failed`` handler and adds a catch-all for
    the case where the agent did work (api_calls > 0) but returned no text.
    Fix for #18765.

    Also surfaces a retry hint when the agent never ran at all
    (api_calls == 0) for a non-interrupted, non-failed turn -- this is the
    silent-drop pattern observed after ``/stop`` where the next user
    message hits a stale generation token and returns an empty result,
    leaving the platform with nothing to send. (#31884)
    """
    if response:
        return response

    if agent_result.get("failed"):
        error_detail = agent_result.get("error", "unknown error")
        error_str = str(error_detail).lower()
        is_context_failure = any(
            p in error_str
            for p in ("context", "token", "too large", "too long", "exceed", "payload")
        ) or ("400" in error_str and history_len > 50)
        if is_context_failure:
            return (
                "⚠️ Session too large for the model's context window.\n"
                "Use /compact to compress the conversation, or "
                "/reset to start fresh."
            )
        return (
            f"The request failed: {str(error_detail)[:300]}\n"
            "Try again or use /reset to start a fresh session."
        )

    api_calls = int(agent_result.get("api_calls", 0) or 0)
    if agent_result.get("interrupted"):
        # An interrupted run that did work (api_calls > 0) is the drain of a
        # run the user deliberately stopped or steered — its silence is
        # intentional, and any queued/interrupting message is delivered by
        # the recursive drain inside _run_agent before this result is seen.
        # An interrupted run with ZERO api_calls never processed the user's
        # message at all: it was killed at the top of the tool loop by an
        # interrupt flag left over from a recent /stop (#44212).  Pure
        # silence there swallows a real user message, so surface it.
        if api_calls == 0:
            return (
                "⚠️ Your message was interrupted before processing started "
                "(likely by a recent /stop). Please send it again."
            )
        return response
    if api_calls > 0:
        if agent_result.get("partial"):
            err = agent_result.get("error", "processing incomplete")
            return f"⚠️ Processing stopped: {str(err)[:200]}. Try again."
        return (
            "⚠️ Processing completed but no response was generated. "
            "This may be a transient error — try sending your message again."
        )

    # api_calls == 0, not failed, not interrupted: the agent never ran for
    # this turn. This is the post-/stop generation-race pattern where the
    # gateway would otherwise silently drop the turn (response=0 chars) and
    # the user sees no reply at all. Surface a short retry hint so the
    # message isn't lost in silence. (#31884)
    if (
        api_calls == 0
        and not agent_result.get("interrupted")
        and not agent_result.get("failed")
        and not agent_result.get("partial")
    ):
        return (
            "⚠️ Your message wasn't processed (the previous turn was still "
            "being cleaned up). Please send it again."
        )

    return response


def _should_clear_resume_pending_after_turn(agent_result: dict) -> bool:
    """Return True only when a gateway turn really completed successfully.

    Restart recovery uses ``resume_pending`` as a durable marker for sessions
    interrupted during gateway drain.  A soft interrupt can still bubble out as
    a syntactically normal agent result with an empty final response; clearing
    the marker in that case loses the recovery signal and startup auto-resume
    has nothing to schedule.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("interrupted"):
        return False
    if agent_result.get("failed") or agent_result.get("partial") or agent_result.get("error"):
        return False
    if agent_result.get("completed") is False:
        return False
    return True


def _preserve_queued_followup_history_offset(
    current_result: dict,
    followup_result: dict,
) -> dict:
    """Carry the outer history offset through queued follow-up drains.

    ``_process_message_background()`` persists transcript rows only once, after the
    entire in-band queued-follow-up chain returns.  Each recursive ``_run_agent()``
    call advances ``history_offset`` to the history it received, so without
    correction the outermost persistence step sees only the *last* queued turn as
    "new" and silently drops earlier turns from the same drain chain.

    Preserve the earliest (outermost) history offset so the final transcript slice
    still includes every queued turn that ran during the chain.
    """
    if not isinstance(followup_result, dict):
        return followup_result
    if not isinstance(current_result, dict):
        return followup_result

    current_offset = current_result.get("history_offset")
    followup_offset = followup_result.get("history_offset")
    if not isinstance(current_offset, int):
        return followup_result
    if isinstance(followup_offset, int) and followup_offset <= current_offset:
        return followup_result

    merged = dict(followup_result)
    merged["history_offset"] = current_offset
    return merged


async def _dispose_unused_adapter(adapter: "BasePlatformAdapter | None") -> None:
    """Best-effort dispose for an adapter that never made it onto ``self.adapters``.

    The reconnect watcher in ``GatewayRunner._platform_reconnect_watcher``
    constructs a fresh adapter on every retry attempt. When the connect
    call fails — for any of the three reasons (non-retryable error,
    retryable error, exception during connect) — the adapter is dropped
    without ever being installed, so nothing else will call its
    ``disconnect()``. Any resources the adapter opened in ``__init__``
    (e.g. ``APIServerAdapter`` opens a SQLite ``ResponseStore`` that
    holds 2 fds — the db file and its WAL sidecar) stay open until
    garbage collection sweeps the unreachable object, which Python's
    cyclic GC does not do promptly for asyncio-bound objects with
    native handles. The cumulative leak is 2 fds × every retry at the
    300s backoff cap ≈ 12 fds/hour, and the default 2560-fd ulimit
    is exhausted in ~12h of continuous failure, after which every
    open() call on the gateway raises ``OSError: [Errno 24] Too many
    open files`` and the gateway becomes a zombie (#37011).

    This helper centralises the dispose-with-suppression so the three
    failure paths in the reconnect watcher can all call it without
    each one having to know that ``disconnect()`` may itself raise
    on a half-constructed adapter.

    ``adapter`` may be ``None``: the reconnect watcher initialises
    ``adapter = None`` before the ``try`` so the ``except Exception``
    arm can dispose a half-constructed object, and also early-returns
    here when ``_create_adapter()`` returned ``None``.
    """
    if adapter is None:
        return
    try:
        await adapter.disconnect()
    except Exception:
        # Half-constructed adapters (e.g. APIServerAdapter that
        # crashed during aiohttp app setup) can raise from
        # disconnect() on objects that never finished initializing.
        # We must not let that escape and abort the watcher loop.
        #
        # On Python 3.8+, ``asyncio.CancelledError`` inherits from
        # ``BaseException`` (not ``Exception``), so this ``except
        # Exception`` does not swallow task cancellation. We don't
        # re-raise explicitly because the watcher loop intentionally
        # treats dispose failures as best-effort: a failed ``disconnect``
        # call should not take down the reconnect watcher that
        # itself is what's keeping the gateway alive during a partial
        # outage.
        logger.debug(
            "Adapter dispose raised on unowned adapter %r",
            getattr(adapter, "name", type(adapter).__name__),
            exc_info=True,
        )




def _run_planned_stop_watcher(
    stop_event: threading.Event,
    runner,
    loop: asyncio.AbstractEventLoop,
    shutdown_handler,
    *,
    poll_interval: float = 0.5,
) -> None:
    """Poll for the planned-stop marker and trigger graceful shutdown.

    On Windows, ``asyncio.add_signal_handler`` raises NotImplementedError
    for SIGTERM/SIGINT, so the standard signal-driven shutdown path
    never runs when ``hermes gateway stop`` signals the gateway. The
    consequence is that the drain loop is skipped — in-flight agent
    sessions are killed mid-turn and ``resume_pending`` is never set,
    so the next gateway boot has no idea those sessions need to be
    auto-resumed (issue #33778, v0.13.0 session-resume feature broken
    on native Windows).

    This watcher runs on every platform (cheap, defensive) and bridges
    the gap on Windows by translating a filesystem marker into the
    same shutdown-handler invocation a real SIGTERM would have produced
    on POSIX. The CLI's ``hermes_cli.gateway_windows.stop()`` writes
    the marker via ``write_planned_stop_marker(pid)`` and then waits
    for the gateway PID to exit; this watcher is what makes that
    exit happen cleanly.

    On POSIX this is a no-op safety net — the signal handler always
    races us to consuming the marker file because it fires synchronously
    from the kernel's signal delivery.

    Args:
        stop_event: cleared by start_gateway() during normal shutdown
            to tell the watcher to exit.
        runner: the GatewayRunner instance; we check ``_running`` and
            ``_draining`` to avoid triggering shutdown if the gateway
            is already in one of those states.
        loop: the asyncio event loop the shutdown handler must run on.
        shutdown_handler: same callable that's wired to SIGTERM —
            tolerates a ``None`` signal argument (planned stop case)
            and consumes the marker via
            ``consume_planned_stop_marker_for_self()``.
        poll_interval: seconds between marker checks. 0.5s gives a
            responsive shutdown without burning CPU.
    """
    from gateway.status import (
        _get_planned_stop_marker_path,
        planned_stop_marker_targets_self,
    )
    marker_path = _get_planned_stop_marker_path()
    while not stop_event.is_set():
        try:
            if (
                marker_path.exists()
                and not getattr(runner, "_draining", False)
                and getattr(runner, "_running", False)
            ):
                # A marker existing is NOT sufficient — it may have been
                # written for a PREVIOUS gateway instance (different PID)
                # and left behind because that process exited before the
                # CLI's stop() could clean it up. Firing the handler on a
                # stale/foreign marker drives the gateway into shutdown,
                # then consume_planned_stop_marker_for_self() correctly
                # reports a PID mismatch — but by then we're already
                # stopping, so it's logged as an unexpected "UNKNOWN" exit
                # and the watchdog crash-loops the gateway (issue #34597,
                # a regression from PR #33798 which added this watcher
                # without the PID check).
                #
                # Only fire when the marker actually targets us. The probe
                # is non-destructive on a match (the handler does the
                # authoritative consume on the loop thread) and self-heals
                # by unlinking stale/malformed markers so they cannot wedge
                # a freshly booted gateway.
                if not planned_stop_marker_targets_self():
                    stop_event.wait(poll_interval)
                    continue
                # Drive the same path as a real signal handler.
                # Pass signal=None — the handler tolerates that and consumes
                # the marker via consume_planned_stop_marker_for_self,
                # which also validates target_pid + start_time match us.
                loop.call_soon_threadsafe(shutdown_handler, None)
                # Done — the handler will set _draining; we exit on next tick.
                break
        except Exception as _e:
            logger.debug("Planned-stop watcher tick error: %s", _e)
        stop_event.wait(poll_interval)


def _start_gateway_housekeeping(stop_event: threading.Event, adapters=None, loop=None, interval: int = 60):
    """Background thread for gateway-only periodic chores (NOT cron).

    Split out of the historical ``_start_cron_ticker`` so the cron *trigger*
    can live behind the ``CronScheduler`` provider (built-in or external) while
    these gateway-specific chores keep running independently of which provider
    fires cron. An external scale-to-zero provider has no 60s loop at all, but
    this housekeeping still wants its hourly cadence — so it owns its own loop.

    Refreshes the channel directory every 5 minutes and prunes the
    image/audio/document cache + expired ``hermes debug share`` pastes once per
    hour, and polls the curator hourly (its inner gate enforces the real
    weekly cadence).
    """
    from gateway.platforms.base import cleanup_image_cache, cleanup_document_cache
    from hermes_cli.debug import _sweep_expired_pastes

    IMAGE_CACHE_EVERY = 60   # ticks — once per hour at default 60s interval
    CHANNEL_DIR_EVERY = 5    # ticks — every 5 minutes
    PASTE_SWEEP_EVERY = 60   # ticks — once per hour
    CURATOR_EVERY = 60       # ticks — poll hourly (inner gate handles the real cadence)

    logger.info("Gateway housekeeping started (interval=%ds)", interval)
    tick_count = 0
    while not stop_event.is_set():
        tick_count += 1

        if tick_count % CHANNEL_DIR_EVERY == 0 and adapters:
            try:
                from gateway.channel_directory import build_channel_directory
                if loop is not None:
                    # build_channel_directory is async (Slack web calls), and
                    # this runs in a background thread. Schedule onto the
                    # gateway event loop and wait briefly for completion so
                    # refresh failures are still logged via the except.
                    fut = safe_schedule_threadsafe(
                        build_channel_directory(adapters), loop,
                        logger=logger,
                        log_message="Channel directory refresh scheduling error",
                    )
                    if fut is not None:
                        fut.result(timeout=30)
            except Exception as e:
                logger.debug("Channel directory refresh error: %s", e)

        if tick_count % IMAGE_CACHE_EVERY == 0:
            try:
                removed = cleanup_image_cache(max_age_hours=24)
                if removed:
                    logger.info("Image cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Image cache cleanup error: %s", e)
            try:
                removed = cleanup_document_cache(max_age_hours=24)
                if removed:
                    logger.info("Document cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Document cache cleanup error: %s", e)

        if tick_count % PASTE_SWEEP_EVERY == 0:
            try:
                deleted, remaining = _sweep_expired_pastes()
                if deleted:
                    logger.info(
                        "Paste sweep: deleted %d expired paste(s), %d pending",
                        deleted, remaining,
                    )
            except Exception as e:
                logger.debug("Paste sweep error: %s", e)

        # Curator — piggy-back on the housekeeping loop so long-running
        # gateways get weekly skill maintenance without needing restarts.
        # maybe_run_curator() is internally gated by config.interval_hours
        # (7 days by default), so CURATOR_EVERY is just the poll rate — the
        # real work only fires once per config interval.
        if tick_count % CURATOR_EVERY == 0:
            try:
                from agent.curator import maybe_run_curator
                maybe_run_curator(
                    idle_for_seconds=float("inf"),
                    on_summary=lambda msg: logger.info("curator: %s", msg),
                )
            except Exception as e:
                logger.debug("Curator tick error: %s", e)

        stop_event.wait(timeout=interval)
    logger.info("Gateway housekeeping stopped")


def _start_cron_ticker(stop_event: threading.Event, adapters=None, loop=None, interval: int = 60):
    """DEPRECATED shim — preserved for backward compatibility.

    The cron trigger now lives behind the ``CronScheduler`` provider
    (``cron.scheduler_provider``); the gateway resolves a provider and runs its
    ``start()`` directly (see ``start_gateway``). This shim runs ONLY the
    built-in in-process tick loop, exactly as before, for any external caller
    or test that still references this symbol (e.g. hermes_cli/debug.py). It no
    longer runs gateway housekeeping — that moved to
    ``_start_gateway_housekeeping``.
    """
    from cron.scheduler_provider import InProcessCronScheduler
    InProcessCronScheduler().start(stop_event, adapters=adapters, loop=loop, interval=interval)


# Upper bound for cooperatively draining the cron ticker on shutdown. The cron
# thread delivers via ``safe_schedule_threadsafe`` and blocks on
# ``future.result(timeout=60)`` (see cron/scheduler.py::_deliver_result), so a
# single in-flight delivery unblocks within ~60s. The extra margin covers the
# hop back through run_one_job's bookkeeping.
_CRON_SHUTDOWN_DRAIN_TIMEOUT = 65.0

# Upper bound for cooperatively draining the housekeeping ticker on shutdown.
# Housekeeping periodically refreshes the channel directory via
# ``safe_schedule_threadsafe(build_channel_directory(...), loop)`` and blocks on
# ``fut.result(timeout=30)`` (see ``_start_gateway_housekeeping``) — the same
# loop-scheduled-future pattern as cron. So the cooperative bound must cover
# that 30s future (plus margin) rather than the old 5s join, otherwise a
# channel-directory refresh in flight at shutdown gets abandoned mid-resolve.
# Unlike a dropped cron delivery this is not user-facing (it self-heals on the
# next tick), but bounding it correctly keeps the drain honest.
_HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT = 35.0


async def _await_thread_exit(
    thread: Optional[threading.Thread], timeout: float, poll: float = 0.1
) -> bool:
    """Wait for a daemon thread to exit WITHOUT blocking the event loop.

    A synchronous ``thread.join()`` here would freeze the event loop — fatal
    for the cron ticker, whose in-flight delivery is a coroutine scheduled onto
    *this* loop via ``safe_schedule_threadsafe``. Blocking the loop deadlocks
    that delivery (the loop can never run it), so ``join(timeout=5)`` always
    times out and the message is silently dropped on restart (#58818).

    Polling ``is_alive()`` with ``await asyncio.sleep`` keeps the loop running
    so the pending delivery completes, then the ticker sees ``stop_event`` and
    exits. Returns True if the thread exited within ``timeout``.
    """
    if thread is None:
        return True
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while thread.is_alive() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll)
    return not thread.is_alive()


async def start_gateway(config: Optional[GatewayConfig] = None, replace: bool = False, verbosity: Optional[int] = 0) -> bool:
    """
    Start the gateway and run until interrupted.
    
    This is the main entry point for running the gateway.
    Returns True if the gateway ran successfully, False if it failed to start.
    A False return causes a non-zero exit code so systemd can auto-restart.
    
    Args:
        config: Optional gateway configuration override.
        replace: If True, kill any existing gateway instance before starting.
                 Useful for systemd services to avoid restart-loop deadlocks
                 when the previous process hasn't fully exited yet.
    """
    # Snapshot the checkout revision now, while sys.modules still matches disk,
    # so a later `git pull` under this long-lived process can be detected (and
    # risky work like model switching refused) instead of crashing on a stale
    # in-memory module.
    from gateway.code_skew import record_boot_fingerprint
    record_boot_fingerprint()

    # ── Duplicate-instance guard ──────────────────────────────────────
    # Prevent two gateways from running under the same HERMES_HOME.
    # The PID file is scoped to HERMES_HOME, so future multi-profile
    # setups (each profile using a distinct HERMES_HOME) will naturally
    # allow concurrent instances without tripping this guard.
    from gateway.status import (
        acquire_gateway_runtime_lock,
        get_running_pid,
        get_process_start_time,
        release_gateway_runtime_lock,
        remove_pid_file,
        terminate_pid,
    )
    existing_pid = get_running_pid()
    if existing_pid is not None and existing_pid != os.getpid():
        if replace:
            existing_start_time = get_process_start_time(existing_pid)
            logger.info(
                "Replacing existing gateway instance (PID %d) with --replace.",
                existing_pid,
            )
            # Record a takeover marker so the target's shutdown handler
            # recognises its SIGTERM as a planned takeover and exits 0
            # (rather than exit 1, which would trigger systemd's
            # Restart=on-failure and start a flap loop against us).
            # Best-effort — proceed even if the write fails.
            try:
                from gateway.status import write_takeover_marker
                write_takeover_marker(existing_pid)
            except Exception as e:
                logger.debug("Could not write takeover marker: %s", e)
            try:
                terminate_pid(existing_pid, force=False)
            except ProcessLookupError:
                pass  # Already gone
            except (PermissionError, OSError):
                logger.error(
                    "Permission denied killing PID %d. Cannot replace.",
                    existing_pid,
                )
                # Marker is scoped to a specific target; clean it up on
                # give-up so it doesn't grief an unrelated future shutdown.
                try:
                    from gateway.status import clear_takeover_marker
                    clear_takeover_marker()
                except Exception:
                    pass
                return False
            # Wait up to 10 seconds for the old process to exit.
            # ``os.kill(pid, 0)`` on Windows is NOT a no-op — use the
            # handle-based existence check instead.
            from gateway.status import _pid_exists
            old_gateway_exited = False
            for _ in range(20):
                if not _pid_exists(existing_pid):
                    old_gateway_exited = True
                    break  # Process is gone
                time.sleep(0.5)
            else:
                # Still alive after 10s — force kill
                logger.warning(
                    "Old gateway (PID %d) did not exit after SIGTERM, sending SIGKILL.",
                    existing_pid,
                )
                try:
                    terminate_pid(existing_pid, force=True)
                except ProcessLookupError:
                    old_gateway_exited = True
                except (PermissionError, OSError):
                    pass
                # Confirm the force-kill actually reaped the process before we
                # clear its PID file / scoped locks. SIGKILL can fail to take
                # (e.g. an uninterruptible-sleep or zombie-reaping parent), and
                # if we blindly clear the metadata and start a fresh instance
                # we end up with two live gateways fighting over the same
                # token — the duplicate-gateway failure in #19471.
                if not old_gateway_exited:
                    for _ in range(20):
                        if not _pid_exists(existing_pid):
                            old_gateway_exited = True
                            break
                        time.sleep(0.25)
                if not old_gateway_exited:
                    logger.error(
                        "Old gateway (PID %d) still appears alive after SIGKILL; "
                        "aborting replacement to avoid a duplicate gateway.",
                        existing_pid,
                    )
                    try:
                        from gateway.status import clear_takeover_marker
                        clear_takeover_marker()
                    except Exception:
                        pass
                    return False
            remove_pid_file()
            # remove_pid_file() is a no-op when the PID doesn't match.
            # Force-unlink to cover the old-process-crashed case.
            try:
                (get_hermes_home() / "gateway.pid").unlink(missing_ok=True)
            except Exception:
                pass
            # Clean up any takeover marker the old process didn't consume
            # (e.g. SIGKILL'd before its shutdown handler could read it).
            try:
                from gateway.status import clear_takeover_marker
                clear_takeover_marker()
            except Exception:
                pass
            # Also release all scoped locks left by the old process.
            # Stopped (Ctrl+Z) processes don't release locks on exit,
            # leaving stale lock files that block the new gateway from starting.
            try:
                from gateway.status import release_all_scoped_locks
                _released = release_all_scoped_locks(
                    owner_pid=existing_pid,
                    owner_start_time=existing_start_time,
                )
                if _released:
                    logger.info("Released %d stale scoped lock(s) from old gateway.", _released)
            except Exception:
                pass
        else:
            hermes_home = str(get_hermes_home())
            logger.error(
                "Another gateway instance is already running (PID %d, HERMES_HOME=%s). "
                "Use 'hermes gateway restart' to replace it, or 'hermes gateway stop' first.",
                existing_pid, hermes_home,
            )
            print(
                f"\n❌ Gateway already running (PID {existing_pid}).\n"
                f"   Use 'hermes gateway restart' to replace it,\n"
                f"   or 'hermes gateway stop' to kill it first.\n"
                f"   Or use 'hermes gateway run --replace' to auto-replace.\n"
            )
            return False

    # Sync bundled skills on gateway start (fast -- skips unchanged)
    try:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)
    except Exception:
        pass

    # Centralized logging — agent.log (INFO+), errors.log (WARNING+),
    # and gateway.log (INFO+, gateway-component records only).
    # Idempotent, so repeated calls from AIAgent.__init__ won't duplicate.
    from hermes_logging import setup_logging, _safe_stderr
    setup_logging(hermes_home=_hermes_home, mode="gateway")

    # Startup security posture audit — warn-on-load, never blocks. Surfaces
    # root / weak-SSH / ephemeral-container / unauthenticated-listener posture
    # so operators get the "you're exposed" signal the June 2026 MCP-config
    # persistence campaign victims never had.
    try:
        from hermes_cli.security_audit_startup import log_startup_security_warnings

        _audit_cfg = None
        try:
            from hermes_cli.config import read_raw_config

            _audit_cfg = read_raw_config()
        except Exception:
            _audit_cfg = None
        log_startup_security_warnings(hermes_home=_hermes_home, config=_audit_cfg)
    except Exception as _audit_exc:
        logger.debug("Startup security audit failed (non-fatal): %s", _audit_exc)

    # Optional stderr handler — level driven by -v/-q flags on the CLI.
    # verbosity=None (-q/--quiet): no stderr output
    # verbosity=0    (default):    WARNING and above
    # verbosity=1    (-v):         INFO and above
    # verbosity=2+   (-vv/-vvv):   DEBUG
    if verbosity is not None:
        from agent.redact import RedactingFormatter

        _stderr_level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
        _stderr_handler = logging.StreamHandler(_safe_stderr())
        _stderr_handler.setLevel(_stderr_level)
        _stderr_handler.setFormatter(RedactingFormatter('%(levelname)s %(name)s: %(message)s'))
        logging.getLogger().addHandler(_stderr_handler)
        # Lower root logger level if needed so DEBUG records can reach the handler
        if _stderr_level < logging.getLogger().level:
            logging.getLogger().setLevel(_stderr_level)

    # `GatewayRunner` is defined in `gateway.run`; import it lazily here
    # (call-time) so we don't re-introduce a circular import at module load.
    # `gateway.run` is always fully imported by the time `start_gateway` runs.
    from gateway.run import GatewayRunner

    runner = GatewayRunner(config)
    
    # Track whether an unexpected signal initiated the shutdown. When an
    # unexpected SIGTERM kills the gateway, we exit non-zero so service
    # managers can revive the process. Planned stop paths write a marker
    # before signalling us so they can exit cleanly instead.
    _signal_initiated_shutdown = False

    # Set up signal handlers
    def shutdown_signal_handler(received_signal=None):
        nonlocal _signal_initiated_shutdown
        # Planned --replace takeover check: when a sibling gateway is
        # taking over via --replace, it wrote a marker naming this PID
        # before sending SIGTERM. If present, treat the signal as a
        # planned shutdown and exit 0 so systemd's Restart=on-failure
        # doesn't revive us (which would flap-fight the replacer when
        # both services are enabled, e.g. hermes.service + hermes-
        # gateway.service from pre-rename installs).
        planned_takeover = False
        try:
            from gateway.status import consume_takeover_marker_for_self
            planned_takeover = consume_takeover_marker_for_self()
        except Exception as e:
            logger.debug("Takeover marker check failed: %s", e)

        # Planned stop check: service managers and `hermes gateway stop`
        # also send SIGTERM, which is indistinguishable from an unexpected
        # external kill unless the CLI marks it first. SIGINT comes from an
        # interactive Ctrl+C and is likewise an intentional foreground stop.
        planned_stop = False
        if received_signal == signal.SIGINT:
            planned_stop = True
        elif not planned_takeover:
            try:
                from gateway.status import consume_planned_stop_marker_for_self
                planned_stop = consume_planned_stop_marker_for_self()
            except Exception as e:
                logger.debug("Planned stop marker check failed: %s", e)

        # Fast (<10ms) snapshot of who's asking us to shut down — runs
        # synchronously inside the asyncio signal handler, so we keep it
        # purely stdlib + /proc reads, no subprocesses.  See PR #15826
        # (May 2026): the previous implementation called `ps aux` here
        # synchronously, blocking the event loop for up to 3s while
        # adapter teardown couldn't begin.
        try:
            from gateway.shutdown_forensics import (
                format_context_for_log,
                snapshot_shutdown_context,
                spawn_async_diagnostic,
            )
            _shutdown_ctx = snapshot_shutdown_context(received_signal)
        except Exception as _e:
            _shutdown_ctx = None
            logger.debug("snapshot_shutdown_context failed: %s", _e)

        if planned_takeover:
            logger.info(
                "Received %s as a planned --replace takeover — exiting cleanly",
                _shutdown_ctx["signal"] if _shutdown_ctx else "SIGTERM",
            )
        elif planned_stop:
            logger.info(
                "Received %s as a planned gateway stop — exiting cleanly",
                _shutdown_ctx["signal"] if _shutdown_ctx else "SIGTERM/SIGINT",
            )
        else:
            _signal_initiated_shutdown = True
            # Mirror onto the runner so _stop_impl can suppress the
            # gateway_state=stopped persist for unexpected signals
            # (container/s6 SIGTERM on restart, OOM, bare kill) — see
            # issue #42675. Operator-initiated stops set a planned-stop
            # marker first, land in the `planned_stop` branch above, and
            # leave this flag False so they DO persist "stopped".
            runner._signal_initiated_shutdown = True
            logger.info(
                "Received %s — initiating shutdown",
                _shutdown_ctx["signal"] if _shutdown_ctx else "SIGTERM/SIGINT",
            )

        # Always log who/what triggered the signal — most useful single
        # line when diagnosing "the gateway keeps dying" tickets.  Format
        # is one line, key=value, parent_cmdline last (often long).
        if _shutdown_ctx is not None:
            try:
                logger.warning(
                    "Shutdown context: %s", format_context_for_log(_shutdown_ctx)
                )
            except Exception as _e:
                logger.debug("format_context_for_log failed: %s", _e)

            # Spawn the heavyweight diagnostic (ps auxf, pstree, dmesg) in
            # a detached subprocess so it can finish writing to disk even
            # if our cgroup is being torn down.  Bounded by an internal
            # timeout; never blocks the event loop here.
            try:
                _diag_log = _hermes_home / "logs" / "gateway-shutdown-diag.log"
                spawn_async_diagnostic(
                    _diag_log, _shutdown_ctx["signal"], timeout_seconds=5.0
                )
            except Exception as _e:
                logger.debug("spawn_async_diagnostic failed: %s", _e)
        asyncio.create_task(runner.stop())

    def restart_signal_handler():
        runner.request_restart(detached=False, via_service=True)
    
    loop = asyncio.get_running_loop()

    # Install a loop-level exception handler that swallows transient
    # network errors from background tasks. Issues #31066 / #31110:
    # an unhandled ``telegram.error.TimedOut`` (or peer NetworkError /
    # httpx connection error) in any awaited coroutine would propagate
    # to the loop and kill the gateway process, taking down every
    # profile attached to the same runner. systemd then restarts the
    # service after ~5s but the active conversation turn is lost.
    #
    # The fix is intentionally narrow: only well-known transient
    # network errors are swallowed (and logged with full traceback so
    # the originating call site is still discoverable). Anything else
    # is forwarded to the default handler so real bugs still surface.
    loop.set_exception_handler(_gateway_loop_exception_handler)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_signal_handler, sig)  # windows-footgun: ok — wrapped in try/except NotImplementedError for Windows
            except NotImplementedError:
                pass
        if hasattr(signal, "SIGUSR1"):
            try:
                loop.add_signal_handler(signal.SIGUSR1, restart_signal_handler)  # windows-footgun: ok — POSIX signal, guarded by hasattr above + try/except NotImplementedError
            except NotImplementedError:
                pass
    else:
        logger.info("Skipping signal handlers (not running in main thread).")

    # Windows fallback: asyncio.add_signal_handler raises NotImplementedError
    # on Windows, so `hermes gateway stop`'s SIGTERM (which Python maps to
    # TerminateProcess on Windows) never invokes shutdown_signal_handler.
    # That means the drain loop never runs, mark_resume_pending never fires,
    # and sessions are silently lost across restarts (issue #33778).
    #
    # The fix is a marker-polling thread: `hermes gateway stop` writes the
    # planned-stop marker BEFORE killing, and this thread notices it and
    # drives the same shutdown path the signal handler would have.  Runs
    # on every platform (cheap, defensive) so non-signal-bearing
    # environments (Windows native, sandboxed CI runners that mask
    # SIGTERM) still get a clean drain.
    _planned_stop_watcher_stop = threading.Event()
    _planned_stop_watcher_thread = threading.Thread(
        target=_run_planned_stop_watcher,
        args=(_planned_stop_watcher_stop, runner, loop, shutdown_signal_handler),
        daemon=True,
        name="planned-stop-watcher",
    )
    _planned_stop_watcher_thread.start()

    # Claim the PID file BEFORE bringing up any platform adapters.
    # This closes the --replace race window: two concurrent `gateway run
    # --replace` invocations both pass the termination-wait above, but
    # only the winner of the O_CREAT|O_EXCL race below will ever open
    # Telegram polling, Discord gateway sockets, etc. The loser exits
    # cleanly before touching any external service.
    import atexit
    from gateway.status import write_pid_file, remove_pid_file, get_running_pid
    _current_pid = get_running_pid()
    if _current_pid is not None and _current_pid != os.getpid():
        logger.error(
            "Another gateway instance (PID %d) started during our startup. "
            "Exiting to avoid double-running.", _current_pid
        )
        return False
    if not acquire_gateway_runtime_lock():
        logger.error(
            "Gateway runtime lock is already held by another instance. Exiting."
        )
        return False
    try:
        write_pid_file()
    except FileExistsError:
        release_gateway_runtime_lock()
        logger.error(
            "PID file race lost to another gateway instance. Exiting."
        )
        return False
    atexit.register(remove_pid_file)
    atexit.register(release_gateway_runtime_lock)

    try:
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive

        start_nous_auth_keepalive()
    except Exception as exc:
        logger.debug("Nous auth keepalive did not start: %s", exc)

    _ensure_windows_gateway_venv_imports()

    # MCP tool discovery — run in an executor so the asyncio event loop
    # stays responsive even when a configured MCP server is slow or
    # unreachable.  discover_mcp_tools() uses a blocking 120s wait
    # internally; calling it from the loop thread would freeze platform
    # heartbeats (Discord shard, Telegram polling) until it returned.
    # See #16856.
    try:
        from tools.mcp_tool import discover_mcp_tools
        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(None, discover_mcp_tools)
    except Exception as e:
        logger.debug("MCP tool discovery failed: %s", e)

    # Start the gateway
    success = await runner.start()
    if not success:
        return False
    if runner.should_exit_cleanly:
        if runner.exit_reason:
            logger.error("Gateway exiting cleanly: %s", runner.exit_reason)
        # A clean exit that carries an explicit exit code (e.g. a fatal
        # config error stamped with GATEWAY_FATAL_CONFIG_EXIT_CODE) must
        # propagate that code to the process so the s6 finish script can
        # translate it (78 → 125) and stop the supervisor restart loop.
        # Without this, the early `return True` below makes main() exit 0,
        # the finish script's `[ "$1" = "78" ]` check never matches, and
        # s6 crash-loops the gateway anyway (#51228).
        if runner.exit_code is not None:
            raise SystemExit(runner.exit_code)
        return True
    if not runner._running:
        # Startup was intentionally aborted by restart/shutdown before entering
        # running mode; preserve that lifecycle path without starting cron.
        await runner.wait_for_shutdown()
        if runner.should_exit_with_failure:
            if runner.exit_reason:
                logger.error("Gateway exiting with failure: %s", runner.exit_reason)
            return False
        try:
            from tools.mcp_tool import shutdown_mcp_servers
            shutdown_mcp_servers()
        except Exception:
            pass
        if runner.exit_code is not None:
            raise SystemExit(runner.exit_code)
        return True
    
    # Start the background cron scheduler via the resolved provider so
    # scheduled jobs fire automatically. The built-in provider is the
    # historical in-process 60s ticker; an external provider (e.g. chronos)
    # may arm a schedule and return. Pass the event loop so cron delivery can
    # use live adapters (E2EE support).
    from cron.scheduler_provider import resolve_cron_scheduler
    cron_stop = threading.Event()
    cron_provider = resolve_cron_scheduler()
    cron_thread = threading.Thread(
        target=cron_provider.start,
        args=(cron_stop,),
        kwargs={"adapters": runner.adapters, "loop": asyncio.get_running_loop()},
        daemon=True,
        name="cron-scheduler",
    )
    cron_thread.start()

    # Gateway-only periodic housekeeping (channel dir, cache cleanup, paste
    # sweep, curator) — runs independently of which cron provider is active.
    # Shares cron_stop as the shutdown signal.
    housekeeping_thread = threading.Thread(
        target=_start_gateway_housekeeping,
        args=(cron_stop,),
        kwargs={"adapters": runner.adapters, "loop": asyncio.get_running_loop()},
        daemon=True,
        name="gateway-housekeeping",
    )
    housekeeping_thread.start()
    
    # Wait for shutdown
    await runner.wait_for_shutdown()

    try:
        from hermes_cli.nous_auth_keepalive import stop_nous_auth_keepalive

        stop_nous_auth_keepalive()
    except Exception:
        pass

    if runner.should_exit_with_failure:
        if runner.exit_reason:
            logger.error("Gateway exiting with failure: %s", runner.exit_reason)
        return False
    
    # Stop cron scheduler + housekeeping cleanly.
    #
    # These MUST be awaited cooperatively, not join()ed. A cron delivery in
    # flight when the gateway restarts is a coroutine scheduled onto THIS event
    # loop (safe_schedule_threadsafe); the ticker thread is blocked on its
    # future.result(). A synchronous cron_thread.join() would block the loop,
    # so that delivery could never run — it timed out and the message was
    # silently dropped (#58818). Awaiting keeps the loop alive so the in-flight
    # delivery finishes before we tear down.
    cron_stop.set()
    try:
        cron_provider.stop()
    except Exception as e:
        logger.debug("Cron provider stop() error: %s", e)
    if not await _await_thread_exit(cron_thread, timeout=_CRON_SHUTDOWN_DRAIN_TIMEOUT):
        logger.warning(
            "Cron ticker did not exit within %.0fs of shutdown — an in-flight "
            "delivery may have been dropped.", _CRON_SHUTDOWN_DRAIN_TIMEOUT,
        )
    await _await_thread_exit(
        housekeeping_thread, timeout=_HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT
    )

    # Stop the planned-stop watcher (daemon=True so this is belt-and-suspenders).
    _planned_stop_watcher_stop.set()
    _planned_stop_watcher_thread.join(timeout=2)

    # Close MCP server connections
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except Exception:
        pass

    if runner.exit_code is not None:
        raise SystemExit(runner.exit_code)

    # When an unexpected SIGTERM caused the shutdown and it wasn't a planned
    # restart (/restart, /update, SIGUSR1), exit non-zero so systemd's
    # Restart=on-failure revives the process.  This covers:
    #   - hermes update killing the gateway mid-work
    #   - External kill commands
    #   - WSL2/container runtime sending unexpected signals
    # `hermes gateway stop` and interactive Ctrl+C are handled above as
    # planned stops and should not trigger service-manager revival.
    if _signal_initiated_shutdown and not runner._restart_requested:
        logger.info(
            "Exiting with code 1 (signal-initiated shutdown without restart "
            "request) so systemd Restart=on-failure can revive the gateway."
        )
        return False  # → sys.exit(1) in the caller

    # Older restart paths may reach here without ``runner.exit_code`` set.
    # Keep the historical non-zero fallback for service-managed restarts.
    if runner._restart_via_service:
        logger.info(
            "Exiting with code 75 (service-restart requested) so the service "
            "manager relaunches the gateway."
        )
        raise SystemExit(75)

    return True


def main():
    """CLI entry point for the gateway."""
    # Force UTF-8 stdio on Windows — gateway logs and startup banner would
    # otherwise UnicodeEncodeError on cp1252 consoles.  No-op on POSIX.
    try:
        from hermes_cli.stdio import configure_windows_stdio
        configure_windows_stdio()
    except Exception:
        pass

    import argparse
    
    parser = argparse.ArgumentParser(description="Hermes Gateway - Multi-platform messaging")
    parser.add_argument("--config", "-c", help="Path to gateway config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    config = None
    if args.config:
        import yaml
        with open(args.config, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            config = GatewayConfig.from_dict(data)
    
    # start_gateway() performs the full graceful teardown (adapters
    # disconnected, sessions saved + flushed, SQLite closed, cron/MCP stopped,
    # PID file + runtime lock released) before it returns OR raises SystemExit
    # with an explicit code. Force-exit afterwards so a wedged non-daemon worker
    # thread (e.g. a ThreadPoolExecutor tool/LLM call blocked with no timeout)
    # cannot block interpreter finalization (Py_FinalizeEx joins all non-daemon
    # threads, incl. concurrent.futures' _python_exit) and strand the gateway
    # half-shut down with the supervisor unable to restart it (#53107).
    #
    # SystemExit is caught explicitly: start_gateway raises it on the
    # clean-fatal-config (#51228), planned-restart, and service-restart paths,
    # all of which complete teardown first. Routing those codes through the
    # same os._exit backstop means EVERY exit path is wedge-proof, not just the
    # boolean-return ones.
    try:
        success = asyncio.run(start_gateway(config))
        exit_code = 0 if success else 1
    except SystemExit as e:
        # e.code may be None (→ 0), an int, or a str (→ 1, like CPython).
        if e.code is None:
            exit_code = 0
        elif isinstance(e.code, int):
            exit_code = e.code
        else:
            exit_code = 1
    _exit_after_graceful_shutdown(exit_code)


def _exit_after_graceful_shutdown(exit_code: int) -> None:
    """Flush stdio, release the PID file + runtime lock, then hard-exit.

    Graceful teardown is already complete by the time this runs, so there is
    nothing left that needs a clean interpreter shutdown. We deliberately use
    ``os._exit`` (not ``sys.exit``): ``sys.exit`` raises ``SystemExit``, which
    triggers ``Py_FinalizeEx`` → ``wait_for_thread_shutdown`` and joins every
    non-daemon thread — exactly the hang (#53107) a wedged tool-worker causes.

    ``os._exit`` bypasses ``atexit`` handlers, so we cannot rely on the
    ``atexit``-registered ``remove_pid_file`` / ``release_gateway_runtime_lock``
    (registered in ``start_gateway``) to run. The full-shutdown path releases
    both explicitly in ``_stop_impl``, but the EARLY exit paths —
    clean-fatal-config (#51228) and startup-aborted-before-running — raise
    ``SystemExit`` right after ``runner.start()`` without going through
    ``_stop_impl``, so on those paths ``atexit`` was the only thing releasing
    them. Now that those paths are routed through this backstop (#53107),
    release both here explicitly. Both calls are idempotent —
    ``remove_pid_file`` only unlinks a PID file that belongs to this process,
    and ``release_gateway_runtime_lock`` no-ops when the lock is already
    released — so this is a no-op on the normal shutdown path and the actual
    cleanup on the early-exit paths.

    Logging IS drained here: the rotating file handlers are driven by an
    async ``QueueListener`` on a dedicated thread (see
    ``hermes_logging._register_queued_handler``), so records emitted right
    before shutdown may still be sitting in the in-memory queue. ``os._exit``
    below bypasses ``atexit``, so the ``atexit``-registered listener drain
    never runs on this path — we drain explicitly (bounded, via
    ``drain_log_queue``) or lose the last log lines (including the shutdown
    reason on the early-exit paths). Stdio is flushed too.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    # Release PID + runtime lock BEFORE the log drain: the drain is bounded but
    # could still take up to its timeout on a wedged disk, and these locks must
    # never be stranded. os._exit skips atexit, and the early SystemExit exit
    # paths never run _stop_impl, so release here (idempotent).
    try:
        from gateway.status import remove_pid_file, release_gateway_runtime_lock
        remove_pid_file()
        release_gateway_runtime_lock()
    except Exception:
        pass
    # Drain the async log queue: os._exit bypasses atexit, so the listener's
    # atexit drain won't fire. Use drain_log_queue() (bounded, no restart), NOT
    # flush_log_queue(): if the listener is wedged on the rotation lock — the
    # exact failure this async-logging change survives — an unbounded stop()
    # join would re-freeze the shutdown. drain_log_queue() no-ops when logging
    # never initialized a queue (very early aborts), so this is always safe.
    try:
        from hermes_logging import drain_log_queue
        drain_log_queue(timeout=1.0)
    except Exception:
        pass
    os._exit(exit_code)


if __name__ == "__main__":
    main()


# Exported for re-import by gateway.run
__all__ = [
    "_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT",
    "_AGENT_PENDING_SENTINEL",
    "_auto_continue_freshness_window",
    "_await_thread_exit",
    "_bridge_max_turns_from_config",
    "_build_document_context_note",
    "_build_gateway_agent_history",
    "_build_media_placeholder",
    "_build_replay_entry",
    "_channel_override_lookup_keys",
    "_check_unavailable_skill",
    "_clear_planned_restart_notification",
    "_coerce_gateway_timestamp",
    "_collect_auto_append_media_tags",
    "_collect_history_media_paths",
    "_config_path",
    "_configured_cwd",
    "_credential_pool_for_provider",
    "_current_max_iterations",
    "_dequeue_pending_event",
    "_dispose_unused_adapter",
    "_DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS",
    "_DOCKER_VOLUME_SPEC_RE",
    "_drain_gateway_watch_events",
    "_ensure_ssl_certs",
    "_ensure_windows_gateway_venv_imports",
    "_env_path",
    "_event_media_is_audio",
    "_event_media_is_image",
    "_event_media_is_video",
    "_event_media_type_at",
    "_exit_after_graceful_shutdown",
    "_float_env",
    "_format_duration",
    "_format_gateway_process_notification",
    "_GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS",
    "_gateway_config_home",
    "_gateway_loop_exception_handler",
    "_gateway_platform_value",
    "_gateway_provider_error_reply",
    "_gateway_surface_passes_raw_text",
    "_get_channel_override",
    "_has_platform_display_override",
    "_hermes_home",
    "_home_target_env_var",
    "_home_thread_env_var",
    "_INTERRUPT_REASON_TIMEOUT",
    "_is_auto_continue_noise",
    "_is_control_interrupt_message",
    "_is_fresh_gateway_interruption",
    "_is_transient_network_error",
    "_last_transcript_timestamp",
    "_load_gateway_config",
    "_load_gateway_runtime_config",
    "_looks_like_gateway_provider_error",
    "_message_timestamps_enabled",
    "_non_conversational_metadata",
    "_normalize_empty_agent_response",
    "_own_policy_open_startup_violation",
    "_parse_session_key",
    "_planned_restart_notification_path",
    "_planned_restart_notification_pending",
    "_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT",
    "_platform_config_key",
    "_PORT_BINDING_PLATFORM_VALUES",
    "_prefix_within_utf16_limit",
    "_prepare_gateway_status_message",
    "_preserve_queued_followup_history_offset",
    "_probe_audio_duration",
    "_profile_runtime_scope",
    "_redact_approval_command",
    "_redact_gateway_user_facing_secrets",
    "_reload_runtime_env_preserving_config_authority",
    "_reply_anchor_for_event",
    "_resolve_gateway_display_bool",
    "_resolve_gateway_model",
    "_resolve_hermes_bin",
    "_resolve_progress_thread_id",
    "_resolve_runtime_agent_kwargs",
    "_resolve_runtime_agent_kwargs_for_provider",
    "_restart_notification_pending",
    "_run_planned_stop_watcher",
    "_sanitize_gateway_final_response",
    "_select_cached_agent_history",
    "_send_or_update_status_coro",
    "_should_clear_resume_pending_after_turn",
    "_skill_slug_from_frontmatter",
    "_start_cron_ticker",
    "_start_gateway_housekeeping",
    "_strip_auto_continue_noise",
    "_teams_pipeline_plugin_enabled",
    "_telegramize_command_mentions",
    "_try_resolve_fallback_provider",
    "_uses_telegram_observed_group_context",
    "_wrap_current_message_with_observed_context",
    "BasePlatformAdapter",
    "build_session_context",
    "build_session_context_prompt",
    "build_session_key",
    "EphemeralReply",
    "is_shared_multi_user_session",
    "logger",
    "looks_like_telegram_private_chat_id",
    "main",
    "merge_pending_message_event",
    "MessageEvent",
    "MessageType",
    "render_notice_line",
    "SessionContext",
    "SessionSource",
    "start_gateway",
    "utf16_len",
]