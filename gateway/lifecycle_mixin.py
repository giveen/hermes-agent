"""
GatewayLifecycleMixin — extracted from gateway/run.py.
"""

from __future__ import annotations

# Must be first (UTF-8 stdio on Windows — no-op on POSIX).
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
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

# Star import from helpers provides module-level utilities and functions
# that lifecycle mixin methods depend on (same as run.py).
from gateway.helpers import *  # noqa: F401,F811,F403
from gateway.config import Platform
from agent.onboarding import TOOL_PROGRESS_FLAG

# Explicit imports of private constants that the star-import's __all__ excludes.
from gateway.helpers import (
    _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT,
    _AGENT_PENDING_SENTINEL,
    _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS,
    _DOCKER_VOLUME_SPEC_RE,
    _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS,
    _INTERRUPT_REASON_TIMEOUT,
    _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT,
    _PORT_BINDING_PLATFORM_VALUES,
)

from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.async_utils import safe_schedule_threadsafe
from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
from agent.i18n import t
from hermes_cli.config import cfg_get
from hermes_cli.fallback_config import get_fallback_chain

logger = logging.getLogger(__name__)


class GatewayLifecycleMixin:
    """GatewayLifecycleMixin — mixed into GatewayRunner."""

    def _wire_teams_pipeline_runtime(self) -> None:
        """Bind the Teams meeting pipeline runtime to Graph webhook ingress.

        No-op when the msgraph_webhook adapter isn't running or the
        teams_pipeline plugin isn't enabled — lets the gateway start cleanly
        whether or not the user has opted into the pipeline.
        """
        if Platform.MSGRAPH_WEBHOOK not in self.adapters:
            return
        if not _teams_pipeline_plugin_enabled():
            logger.debug("Teams pipeline plugin is disabled; skipping runtime wiring")
            return
        try:
            from plugins.teams_pipeline.runtime import bind_gateway_runtime
        except Exception as exc:
            logger.warning("Teams pipeline runtime import failed: %s", exc)
            return
        try:
            bound = bind_gateway_runtime(self)
        except Exception as exc:
            logger.warning("Teams pipeline runtime wiring failed: %s", exc)
            return
        if bound:
            logger.info("Teams pipeline runtime bound to msgraph webhook ingress")
        elif self._teams_pipeline_runtime_error:
            logger.warning(
                "Teams pipeline runtime unavailable: %s",
                self._teams_pipeline_runtime_error,
            )

    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        """Warn when Docker-backed gateways lack an explicit export mount.

        MEDIA delivery happens in the gateway process, so paths emitted by the model
        must be readable from the host. A plain container-local path like
        `/workspace/report.txt` or `/output/report.txt` often exists only inside
        Docker, so users commonly need a dedicated export mount such as
        `host-dir:/output`.
        """
        if os.getenv("TERMINAL_ENV", "").strip().lower() != "docker":
            return

        connected = self.config.get_connected_platforms()
        messaging_platforms = [p for p in connected if p not in {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}]
        if not messaging_platforms:
            return

        raw_volumes = os.getenv("TERMINAL_DOCKER_VOLUMES", "").strip()
        volumes: List[str] = []
        if raw_volumes:
            try:
                parsed = json.loads(raw_volumes)
                if isinstance(parsed, list):
                    volumes = [str(v) for v in parsed if isinstance(v, str)]
            except Exception:
                logger.debug("Could not parse TERMINAL_DOCKER_VOLUMES for gateway media warning", exc_info=True)

        has_explicit_output_mount = False
        for spec in volumes:
            match = _DOCKER_VOLUME_SPEC_RE.match(spec)
            if not match:
                continue
            container_path = match.group("container")
            if container_path in _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS:
                has_explicit_output_mount = True
                break

        if has_explicit_output_mount:
            return

        logger.warning(
            "Docker backend is enabled for the messaging gateway but no explicit host-visible "
            "output mount (for example '/home/user/.hermes/cache/documents:/output') is configured. "
            "This is fine if the model already emits host-visible paths, but MEDIA file delivery can fail "
            "for container-local paths like '/workspace/...' or '/output/...'."
        )

    def _has_setup_skill(self) -> bool:
        """Check if the hermes-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill("hermes-agent-setup") is not None
        except Exception:
            return False

    def _voice_key(self, platform: Platform, chat_id: str) -> str:
        """Return a platform-namespaced key for voice mode state."""
        return f"{platform.value}:{chat_id}"

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        result = {}
        for chat_id, mode in data.items():
            if mode not in valid_modes:
                continue
            key = str(chat_id)
            # Skip legacy unprefixed keys (warn and skip)
            if ":" not in key:
                logger.warning(
                    "Skipping legacy unprefixed voice mode key %r during migration. "
                    "Re-enable voice mode on that chat to rebuild the prefixed key.",
                    key,
                )
                continue
            result[key] = mode
        return result

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2)
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
            # ``/voice off`` also clears any explicit enable — it's a hard override.
            enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
            if isinstance(enabled_chats, set):
                enabled_chats.discard(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _set_adapter_auto_tts_enabled(self, adapter, chat_id: str, enabled: bool) -> None:
        """Update an adapter's per-chat auto-TTS opt-in set if present.

        Used for ``/voice on``/``/voice tts`` where the user explicitly wants
        auto-TTS even when ``voice.auto_tts`` is False globally.
        """
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(enabled_chats, set):
            return
        if enabled:
            enabled_chats.add(chat_id)
            # An explicit opt-in clears any stale /voice off for this chat.
            disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
            if isinstance(disabled_chats, set):
                disabled_chats.discard(chat_id)
        else:
            enabled_chats.discard(chat_id)

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live platform adapter.

        Populates three fields from config + ``self._voice_mode``:
          - ``_auto_tts_default``: global default from ``voice.auto_tts``
          - ``_auto_tts_enabled_chats``: chats with mode ``voice_only``/``all``
          - ``_auto_tts_disabled_chats``: chats with mode ``off``
        """
        platform = getattr(adapter, "platform", None)
        if not isinstance(platform, Platform):
            return

        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(disabled_chats, set) and not isinstance(enabled_chats, set):
            return

        # Push the global voice.auto_tts default (config.yaml) onto the adapter.
        # Lazy import to avoid adding a module-level dep from gateway → hermes_cli.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _full_cfg = _load_full_config()
            _auto_tts_default = bool(
                (_full_cfg.get("voice") or {}).get("auto_tts", False)
            )
        except Exception:
            _auto_tts_default = False
        if hasattr(adapter, "_auto_tts_default"):
            adapter._auto_tts_default = _auto_tts_default

        prefix = f"{platform.value}:"
        if isinstance(disabled_chats, set):
            disabled_chats.clear()
            disabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode == "off" and key.startswith(prefix)
            )
        if isinstance(enabled_chats, set):
            enabled_chats.clear()
            enabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode in {"voice_only", "all"} and key.startswith(prefix)
            )

    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        """Call adapter.disconnect() defensively, swallowing any error.

        Used when adapter.connect() failed or raised — the adapter may
        have allocated partial resources (aiohttp.ClientSession, poll
        tasks, child subprocesses) that would otherwise leak and surface
        as "Unclosed client session" warnings at process exit.

        Must tolerate partial-init state and never raise, since callers
        use it inside error-handling blocks.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        try:
            if timeout <= 0:
                await adapter.disconnect()
            else:
                await asyncio.wait_for(adapter.disconnect(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %.1fs while disconnecting %s adapter; continuing shutdown",
                timeout,
                platform.value if platform is not None else "adapter",
            )
        except Exception as e:
            logger.debug(
                "Defensive %s disconnect after failed connect raised: %s",
                platform.value if platform is not None else "adapter",
                e,
            )

    async def _bounded_adapter_teardown(
        self, adapter, platform, *, profile: Optional[str] = None
    ) -> None:
        """Tear down one adapter on the shutdown path with bounded awaits.

        Both ``cancel_background_tasks()`` and ``disconnect()`` can block
        indefinitely when a platform's network state is half-dead (e.g. a
        wedged Feishu/Lark WebSocket thread waiting on I/O). An unbounded
        await here stalls the entire shutdown sequence past systemd's
        ``TimeoutStopSec``; the resulting SIGKILL skips ``atexit`` PID-file
        cleanup, so the next start dies with "PID file race lost" (#14128).

        Each await is wrapped in the existing per-adapter timeout budget
        (``HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT``). On timeout we log
        and force forward progress; the loop never hangs regardless of any
        adapter's internal behavior. Never raises.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        suffix = f" (profile: {profile})" if profile else ""
        started_at = time.monotonic()
        try:
            if timeout <= 0:
                await adapter.cancel_background_tasks()
            else:
                await asyncio.wait_for(
                    adapter.cancel_background_tasks(), timeout=timeout
                )
        except asyncio.TimeoutError:
            logger.warning(
                "✗ %s background-task cancel timed out after %.1fs - forcing continue%s",
                platform.value, timeout, suffix,
            )
        except Exception as e:
            logger.debug("✗ %s background-task cancel error%s: %s", platform.value, suffix, e)
        try:
            if timeout <= 0:
                await adapter.disconnect()
            else:
                await asyncio.wait_for(adapter.disconnect(), timeout=timeout)
            logger.info(
                "✓ %s disconnected (%.2fs)%s",
                platform.value, time.monotonic() - started_at, suffix,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "✗ %s disconnect timed out after %.1fs - forcing continue%s",
                platform.value, timeout, suffix,
            )
        except Exception as e:
            logger.error(
                "✗ %s disconnect error after %.2fs%s: %s",
                platform.value, time.monotonic() - started_at, suffix, e,
            )

    def _adapter_disconnect_timeout_secs(self) -> float:
        """Return the per-adapter disconnect timeout used during shutdown."""
        raw = os.getenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    def _platform_connect_timeout_secs(self) -> float:
        """Return the per-platform connect timeout used during startup/retry."""
        raw = os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def _connect_adapter_with_timeout(
        self, adapter, platform, *, is_reconnect: bool = False
    ) -> bool:
        """Connect an adapter without allowing one platform to block others.

        ``is_reconnect`` is forwarded to ``adapter.connect()`` so platform
        adapters can distinguish a cold first boot (drop any stale
        server-side queue) from a watcher reconnect after a prolonged outage
        (preserve the queue so messages sent during the outage are delivered
        rather than silently dropped — #46621).
        """
        timeout = self._platform_connect_timeout_secs()
        if timeout <= 0:
            return await adapter.connect(is_reconnect=is_reconnect)
        try:
            return await asyncio.wait_for(
                adapter.connect(is_reconnect=is_reconnect), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"{platform.value} connect timed out after {timeout:g}s"
            ) from exc

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.
        """
        # Snapshot the current owner of this platform slot before doing
        # anything else. If it's neither this adapter nor empty, a different
        # adapter has already taken over (e.g. this is a delayed notification
        # from a background retry chain that raced with, and lost to, a
        # reconnect that already succeeded). Acting on a stale notification
        # would overwrite an already-healthy platform's runtime status and
        # incorrectly re-queue it for reconnection, so bail out before any of
        # that happens.
        existing = self.adapters.get(adapter.platform)
        if existing is not None and existing is not adapter:
            logger.debug(
                "Ignoring stale fatal error from a superseded %s adapter instance: %s",
                adapter.platform.value,
                adapter.fatal_error_code or "unknown",
            )
            return

        logger.error(
            "Fatal %s adapter error (%s): %s",
            adapter.platform.value,
            adapter.fatal_error_code or "unknown",
            adapter.fatal_error_message or "unknown error",
        )
        # Phase 7 Unit 7d-B: a relay credential revoked by opt-out is not an
        # error to retry — render it as a clean "disabled" state, not red
        # "fatal"/"retrying". (The code is set non-retryable, so it also drops
        # out of the reconnect queue below.)
        if adapter.fatal_error_code == "relay_disabled":
            platform_state = "disabled"
        elif adapter.fatal_error_retryable:
            platform_state = "retrying"
        else:
            platform_state = "fatal"
        self._update_platform_runtime_status(
            adapter.platform.value,
            platform_state=platform_state,
            error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )

        if existing is adapter:
            # Claim this adapter for teardown before awaiting disconnect() —
            # a second fatal-error notification for the same adapter (e.g.
            # from a concurrent recovery path) would otherwise still see
            # itself as "existing" during the await below and disconnect()
            # the same object twice.
            self.adapters.pop(adapter.platform, None)
            self.delivery_router.adapters = self.adapters
            await adapter.disconnect()

        # Queue retryable failures for background reconnection
        if adapter.fatal_error_retryable:
            platform_config = self.config.platforms.get(adapter.platform)
            if platform_config and adapter.platform not in self._failed_platforms:
                self._failed_platforms[adapter.platform] = {
                    "config": platform_config,
                    "attempts": 0,
                    "next_retry": time.monotonic(),
                }
                logger.info(
                    "%s queued for background reconnection",
                    adapter.platform.value,
                )

        if not self.adapters and not self._failed_platforms:
            self._exit_reason = adapter.fatal_error_message or "All messaging adapters disconnected"
            if adapter.fatal_error_retryable:
                self._exit_with_failure = True
                logger.error("No connected messaging platforms remain. Shutting down gateway for service restart.")
            else:
                logger.error("No connected messaging platforms remain. Shutting down gateway cleanly.")
            await self.stop()
        elif not self.adapters and self._failed_platforms:
            # All platforms are down and queued for background reconnection.
            # Keep the gateway alive so:
            #   • cron jobs still run
            #   • the reconnect watcher can recover platforms when the
            #     underlying problem clears (proxy comes back, user runs
            #     `hermes whatsapp`, etc.)
            # We used to exit-with-failure here to trigger systemd restart,
            # but that converted a transient outage into a restart loop and
            # killed in-process state every time. The reconnect watcher
            # already handles long-running recovery — let it do its job.
            logger.warning(
                "No connected messaging platforms remain, but %d platform(s) "
                "queued for reconnection — gateway staying alive, watcher will "
                "retry in background.",
                len(self._failed_platforms),
            )

    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def _scale_to_zero_has_live_background_work(self) -> bool:
        """Live background work that must block a suspend (D3/F7).

        Backgrounded delegate_task / kanban / terminal(background=true) are NOT
        counted by _running_agent_count(), but suspending mid-flight loses them.
        Checks the runner's own tracked tasks + the process registry's running
        processes + any pending process-completion watchers.
        """
        if any(not t.done() for t in self._background_tasks):
            return True
        try:
            from tools.async_delegation import active_count

            if active_count() > 0:
                return True
        except Exception:  # noqa: BLE001 - never let the idle check raise
            logger.debug("scale-to-zero async-delegation check failed", exc_info=True)
        try:
            from tools.process_registry import process_registry

            if process_registry.has_any_active():
                return True
            if process_registry.pending_watchers:
                return True
        except Exception:  # noqa: BLE001 - never let the idle check raise
            logger.debug("scale-to-zero bg-work check failed", exc_info=True)
        return False

    def _scale_to_zero_idle_timeout_seconds(self) -> float:
        from gateway.scale_to_zero import parse_idle_timeout_seconds

        raw = None
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
            stz = gw.get("scale_to_zero") if isinstance(gw, dict) else None
            if isinstance(stz, dict):
                raw = stz.get("idle_timeout_minutes")
        except Exception:  # noqa: BLE001
            raw = None
        return parse_idle_timeout_seconds(raw)

    def _restart_loop_guard_config(self) -> tuple:
        """Return ``(max_restarts, window_seconds)`` for the auto-resume
        restart-loop breaker (#30719, defense-3), read from
        ``gateway.restart_loop_guard`` in config.yaml with the module defaults
        as fallback. ``max_restarts <= 0`` disables the breaker.
        """
        from gateway import restart_loop_guard as _rlg

        max_restarts = _rlg.DEFAULT_MAX_RESTARTS
        window_seconds = _rlg.DEFAULT_WINDOW_SECONDS
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
            rlg = gw.get("restart_loop_guard") if isinstance(gw, dict) else None
            if isinstance(rlg, dict):
                if isinstance(rlg.get("max_restarts"), int):
                    max_restarts = rlg["max_restarts"]
                if isinstance(rlg.get("window_seconds"), int) and rlg["window_seconds"] > 0:
                    window_seconds = rlg["window_seconds"]
        except Exception:  # noqa: BLE001
            pass
        return max_restarts, window_seconds

    def _scale_to_zero_should_arm(self) -> bool:
        """Whether to start the idle watcher (D1/D11/§3.4(1))."""
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
            should_arm,
        )

        try:
            # Only ENABLED platforms count. `config.platforms` is pre-seeded with a
            # disabled placeholder PlatformConfig for every KNOWN platform (telegram,
            # discord, slack, …), so `.keys()` is the full ~20-entry catalog regardless
            # of what this instance actually runs. Passing the bare keys made
            # `messaging_is_relay_only_or_absent` see those placeholders as live
            # direct-socket platforms and return False, so scale-to-zero NEVER armed on
            # a real relay-only instance. Mirror the connect loop, which already gates on
            # `platform_config.enabled` (see the `if not platform_config.enabled: continue`
            # in the adapter-connect loop) — arm off the same notion of "active platform."
            platforms = (
                [p for p, pc in self.config.platforms.items() if getattr(pc, "enabled", False)]
                if self.config
                else []
            )
        except Exception:  # noqa: BLE001
            platforms = []
        try:
            wake_url = relay_wake_url()
        except Exception:  # noqa: BLE001
            wake_url = None
        return should_arm(
            enabled=scale_to_zero_enabled(),
            relay_only_or_absent=messaging_is_relay_only_or_absent(platforms),
            wake_url=wake_url,
        )

    def _log_scale_to_zero_not_armed_reason(self) -> None:
        """Log why the idle watcher did NOT arm — but only for an OPTED-IN instance.

        A non-opted instance (no HERMES_SCALE_TO_ZERO stamp) not arming is the normal
        case and must stay silent. When the Labs stamp IS set but the watcher still
        didn't arm, that's the surprising case worth one INFO line so "why won't it
        suspend/wake?" is a log grep, not a box-dive.
        """
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
        )

        try:
            enabled = scale_to_zero_enabled()
            if not enabled:
                return  # not opted in — normal, stay quiet
            try:
                active = (
                    [
                        getattr(p, "value", p)
                        for p, pc in self.config.platforms.items()
                        if getattr(pc, "enabled", False)
                    ]
                    if self.config
                    else []
                )
            except Exception:  # noqa: BLE001
                active = []
            relay_only = messaging_is_relay_only_or_absent(active)
            try:
                wake_url = relay_wake_url()
            except Exception:  # noqa: BLE001
                wake_url = None
            logger.info(
                "scale-to-zero: NOT armed despite opt-in — "
                "relay_only_or_absent=%s (enabled platforms=%s), wake_url=%s. "
                "Need relay-only messaging + a registered wake URL.",
                relay_only,
                active or "none",
                "set" if wake_url else "MISSING",
            )
        except Exception:  # noqa: BLE001 - diagnostics must never block startup
            logger.debug("scale-to-zero: not-armed reason logging failed", exc_info=True)

    def _scale_to_zero_is_idle(self) -> bool:
        from gateway.scale_to_zero import is_idle

        return is_idle(
            running_agent_count=self._running_agent_count(),
            seconds_since_last_inbound=time.time() - self._last_inbound_at,
            idle_timeout_seconds=self._scale_to_zero_idle_timeout_seconds(),
            has_live_background_work=self._scale_to_zero_has_live_background_work(),
        )

    def _scale_to_zero_note_real_inbound(self) -> None:
        """Stamp real inbound and restore lifecycle after a dormant wake.

        The watcher marks runtime status `draining` as it quiesces the relay, but
        dormancy is not the stop/restart drain path: the process remains alive and
        should present as running once real traffic wakes it and re-enters the
        gateway. Internal completion/replay events intentionally do not call this
        helper, so they do not keep an otherwise idle gateway awake.
        """
        self._last_inbound_at = time.time()
        if getattr(self, "_scale_to_zero_cooldown_until", 0.0) > 0:
            try:
                self._update_runtime_status("running")
            except Exception:  # noqa: BLE001 - status restoration is best-effort
                logger.debug("scale-to-zero: status restore failed", exc_info=True)
            self._scale_to_zero_cooldown_until = 0.0

    def _relay_adapter_for_dormancy(self):
        """Return the connected RELAY adapter, if any (the one go_dormant targets)."""
        try:
            from gateway.platforms.base import Platform
        except Exception:  # noqa: BLE001
            return None
        return self.adapters.get(Platform.RELAY)

    async def _scale_to_zero_watcher(self, interval: float = 30.0) -> None:
        """Watch for idle and drive the relay dormant so the platform can suspend.

        Started ONLY when _scale_to_zero_should_arm() (opted in via the Labs
        HERMES_SCALE_TO_ZERO stamp + relay-only/absent messaging + a wakeUrl).
        On a sustained idle window it runs the DORMANT sequence (D12/F12/F14):
          - mark runtime status `draining` (composes with the existing state
            machine, §3.4(6); does NOT set _running=False),
          - relay adapter.go_dormant() — going_idle->ack + supervisor-preserving
            socket close (NOT disconnect(), NOT the run.py stop path),
          - deliberately NO mark_resume_pending (D13 — suspend preserves RAM).
        The process stays alive; the platform (Fly autostop:"suspend") suspends
        the now-traffic-idle machine and autostart wakes it on the wakeUrl poke,
        at which point the preserved reconnect supervisor re-dials and the
        connector drains the buffered backlog. After driving dormant we set a
        re-arm cooldown so a wake's drained backlog isn't immediately re-quiesced.
        """
        await asyncio.sleep(min(interval, 30.0))  # let startup settle
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    return
                if time.time() < self._scale_to_zero_cooldown_until:
                    continue
                if not self._scale_to_zero_is_idle():
                    continue
                adapter = self._relay_adapter_for_dormancy()
                if adapter is None:
                    continue
                go_dormant = getattr(adapter, "go_dormant", None)
                if not callable(go_dormant):
                    continue
                logger.info(
                    "scale-to-zero: gateway idle for >= %.0fs — going dormant "
                    "(relay buffered, socket closed, awaiting platform suspend)",
                    self._scale_to_zero_idle_timeout_seconds(),
                )
                try:
                    self._update_runtime_status("draining")
                except Exception:  # noqa: BLE001 - status is best-effort
                    logger.debug("scale-to-zero: status mark failed", exc_info=True)
                try:
                    result = go_dormant()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 - dormancy is best-effort
                    logger.debug("scale-to-zero: go_dormant failed", exc_info=True)
                # 0.F: after a wake the drained inbound updates _last_inbound_at,
                # but give it a window so we don't immediately re-go-dormant on the
                # same idle reading before traffic lands.
                self._scale_to_zero_cooldown_until = time.time() + max(interval, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
                logger.debug("scale-to-zero watcher iteration error", exc_info=True)

    def _status_action_label(self) -> str:
        return "restart" if self._restart_requested else "shutdown"

    def _status_action_gerund(self) -> str:
        return "restarting" if self._restart_requested else "shutting down"

    def _queue_during_drain_enabled(self) -> bool:
        # Both "queue" and "steer" modes imply the user doesn't want messages
        # to be lost during restart — queue them for the newly-spawned gateway
        # process to pick up.  "interrupt" mode drops them (current behaviour).
        return self._restart_requested and self._busy_input_mode in {"queue", "steer"}

    def _enqueue_fifo(self, session_key: str, queued_event: "MessageEvent", adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        if adapter is None:
            return
        pending_slot = getattr(adapter, "_pending_messages", None)
        if pending_slot is None:
            return
        queued_events = getattr(self, "_queued_events", None)
        if queued_events is None:
            queued_events = {}
            self._queued_events = queued_events
        if session_key in pending_slot:
            queued_events.setdefault(session_key, []).append(queued_event)
        else:
            pending_slot[session_key] = queued_event

    def _promote_queued_event(
        self,
        session_key: str,
        adapter: Any,
        pending_event: Optional["MessageEvent"],
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot was drained.

        Called at the drain site after _dequeue_pending_event consumed
        (or failed to consume) the slot.  If there's an overflow item:
          - When pending_event is None (slot was empty), return the
            overflow head as the new pending_event.
          - When pending_event already exists (slot was populated by an
            interrupt follow-up or similar), stage the overflow head in
            the slot so the NEXT recursion picks it up.
        Returns the (possibly updated) pending_event for drain to use.
        """
        queued_events = getattr(self, "_queued_events", None)
        if not queued_events:
            return pending_event
        overflow = queued_events.get(session_key)
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        if not overflow:
            queued_events.pop(session_key, None)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = next_queued
        else:
            # No adapter — push back so we don't silently drop the item.
            queued_events.setdefault(session_key, []).insert(0, next_queued)
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        queued_events = getattr(self, "_queued_events", None) or {}
        depth = len(queued_events.get(session_key, []))
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """Return True for synthetic /goal continuation turns.

        Goal continuations are normal queued user-role events, so pause/clear
        must distinguish them from real user /queue messages before removing or
        suppressing them.
        """
        text = getattr(event_or_text, "text", event_or_text) or ""
        return str(text).startswith("[Continuing toward your standing goal]\nGoal:")

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session.

        User-issued /goal pause/clear can race with a continuation already
        queued by the judge.  Remove only synthetic goal continuations while
        preserving normal /queue and user follow-up events.
        """
        removed = 0
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1

        queued_events = getattr(self, "_queued_events", None)
        if isinstance(queued_events, dict):
            overflow = queued_events.get(session_key) or []
            if overflow:
                kept = []
                for queued_event in overflow:
                    if self._is_goal_continuation_event(queued_event):
                        removed += 1
                    else:
                        kept.append(queued_event)
                if kept:
                    queued_events[session_key] = kept
                else:
                    queued_events.pop(session_key, None)
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug("goal continuation: active-state recheck failed: %s", exc)
            return False

    def _update_runtime_status(self, gateway_state: Optional[str] = None, exit_reason: Optional[str] = None) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(
                gateway_state=gateway_state,
                exit_reason=exit_reason,
                restart_requested=self._restart_requested,
                active_agents=self._running_agent_count(),
            )
        except Exception:
            pass

    def _persist_active_agents(self) -> None:
        """Persist the live in-flight agent count to ``gateway_state.json``.

        Called at every turn boundary (a running-agent slot is claimed or
        released) so the dashboard ``/api/status`` readout reflects in-flight
        gateway turns in near-real-time.  Without this the file is only
        rewritten on lifecycle transitions, so any ``active_agents`` read
        between transitions is stale (a turn could start and finish without the
        file ever moving).

        Deliberately passes ONLY ``active_agents`` — ``gateway_state`` and the
        other fields stay ``_UNSET`` so ``write_runtime_status``'s
        read-merge-write preserves the current lifecycle state (``running`` /
        ``draining`` / …).  Passing ``gateway_state=None`` here would clobber it.
        Best-effort: a failed status write must never disrupt a turn.
        """
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(active_agents=self._running_agent_count())
        except Exception:
            pass

    def _enter_external_drain(self) -> None:
        """Begin external drain: stop accepting new turns, flip state.

        Idempotent — re-entering while already draining is a no-op beyond a
        best-effort status re-write. In-flight turns are NOT interrupted (the
        whole point is to let them finish); only NEW turns are refused.
        """
        if self._external_drain_active:
            return
        self._external_drain_active = True
        logger.info(
            "External drain ENGAGED (.drain_request.json present) — refusing "
            "new turns; %d in-flight turn(s) will finish. Process stays up.",
            self._running_agent_count(),
        )
        # Flip the persisted lifecycle state so /api/status.gateway_busy /
        # gateway_drainable track the drain. Preserve active_agents (the
        # read-merge keeps the live count); only the state changes.
        self._update_runtime_status("draining")

    def _exit_external_drain(self) -> None:
        """Cancel external drain: revert state, re-accept new turns.

        Idempotent. Only reverts to ``running`` when we are actually mid-drain
        AND not also shutting down (a real shutdown ``_draining`` must win —
        never resurrect a stopping gateway to ``running``).
        """
        if not self._external_drain_active:
            return
        self._external_drain_active = False
        if self._draining or not self._running:
            # A shutdown drain is in progress / the loop has stopped — do not
            # clobber the terminal state back to running.
            logger.info(
                "External drain marker cleared during shutdown — not reverting "
                "to running (shutdown takes precedence)."
            )
            return
        logger.info(
            "External drain RELEASED (.drain_request.json removed) — "
            "re-accepting new turns; gateway_state -> running."
        )
        self._update_runtime_status("running")

    async def _drain_control_watcher(self, interval: float = 1.0) -> None:
        """Background task: reconcile gateway accept-state with the drain marker.

        Polls ``.drain_request.json`` (presence-based contract,
        gateway/drain_control.py). Marker present -> ``_enter_external_drain``;
        marker absent -> ``_exit_external_drain``. The 1s cadence bounds the
        observe-the-marker latency the live-validation gate checks (point a).
        Reconciles once at startup. A marker stamped with a PRIOR
        instantiation epoch (one that survived a machine restart on the durable
        HERMES_HOME volume — NS-570) is treated as absent by ``drain_requested``
        and is NOT honoured; only a marker from the current instantiation flips
        the gateway into drain. Best-effort: any tick error is logged and the
        loop continues (a transient stat() failure must not wedge the gateway).
        """
        from gateway.drain_control import drain_requested

        while self._running:
            try:
                if drain_requested():
                    self._enter_external_drain()
                else:
                    self._exit_external_drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Drain-control watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    def _update_platform_runtime_status(
        self,
        platform: str,
        *,
        platform_state: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(
                platform=platform,
                platform_state=platform_state,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            pass

    def _pause_failed_platform(self, platform, *, reason: str = "") -> None:
        """Mark a queued platform as paused — keep it in ``_failed_platforms``
        but stop the reconnect watcher from hammering it.

        Used by ``/platform pause <name>`` for manual operator intervention.
        Paused platforms are surfaced in ``/platform list`` and resumed with
        ``/platform resume <name>``.  Note: the reconnect watcher does NOT
        auto-pause — retryable (network/DNS) failures keep retrying at the
        backoff cap indefinitely so a transient outage self-heals without
        manual intervention.
        """
        info = getattr(self, "_failed_platforms", {}).get(platform)
        if info is None:
            return
        if info.get("paused"):
            return
        info["paused"] = True
        info["pause_reason"] = reason or "auto-paused after repeated failures"
        # Push next_retry far enough out that even if "paused" is missed
        # by a stale code path, the watcher won't fire on it.
        info["next_retry"] = float("inf")
        try:
            self._update_platform_runtime_status(
                platform.value,
                platform_state="paused",
                error_code=None,
                error_message=info["pause_reason"],
            )
        except Exception:
            pass
        logger.warning(
            "%s paused after %d consecutive failures (%s) — "
            "fix the underlying issue then run `/platform resume %s` "
            "to retry, or `hermes gateway restart` to restart the gateway.",
            platform.value, info.get("attempts", 0),
            info["pause_reason"], platform.value,
        )

    def _resume_paused_platform(self, platform) -> bool:
        """Unpause a platform — reset its attempt counter and schedule an
        immediate retry.  Returns True if the platform was paused and is
        now queued; False if it wasn't paused (or wasn't in the queue).
        """
        info = getattr(self, "_failed_platforms", {}).get(platform)
        if info is None:
            return False
        if not info.get("paused"):
            return False
        info["paused"] = False
        info.pop("pause_reason", None)
        info["attempts"] = 0
        info["next_retry"] = time.monotonic()  # retry on next watcher tick
        try:
            self._update_platform_runtime_status(
                platform.value,
                platform_state="retrying",
                error_code=None,
                error_message=None,
            )
        except Exception:
            pass
        logger.info("%s resumed — retrying on next watcher tick", platform.value)
        return True

    async def _launch_detached_restart_command(self) -> None:
        import shutil
        import subprocess

        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            logger.error("Could not locate hermes binary for detached /restart")
            return
        if self._detached_restart_helper_started:
            return
        self._detached_restart_helper_started = True

        current_pid = os.getpid()
        restart_after_s = max(float(getattr(self, "_restart_drain_timeout", 0.0) or 0.0) + 5.0, 5.0)

        # On Windows there's no bash/setsid chain — spawn a tiny Python
        # watcher directly via sys.executable instead.  The watcher polls
        # current_pid, waits for our exit, then runs `hermes gateway
        # restart` with detach flags so the respawn survives the CLI
        # that triggered the /restart command closing its console.
        if sys.platform == "win32":
            import textwrap
            from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

            cmd_argv = [*hermes_cmd, "gateway", "restart"]
            watcher = textwrap.dedent(
                """
                import os, subprocess, sys, time
                from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway
                pid = int(sys.argv[1])
                restart_after_s = float(sys.argv[2])
                cmd = sys.argv[3:]
                deadline = time.monotonic() + restart_after_s

                def _alive(p):
                    # On Windows, os.kill(pid, 0) is NOT a no-op — it maps to
                    # GenerateConsoleCtrlEvent(0, pid) (bpo-14484). Use the
                    # Win32 handle-based existence check instead.
                    if os.name == 'nt':
                        import ctypes
                        k32 = ctypes.windll.kernel32
                        k32.OpenProcess.restype = ctypes.c_void_p
                        k32.WaitForSingleObject.restype = ctypes.c_uint
                        k32.GetLastError.restype = ctypes.c_uint
                        h = k32.OpenProcess(0x1000 | 0x100000, False, int(p))
                        if not h:
                            return k32.GetLastError() != 87
                        try:
                            return k32.WaitForSingleObject(h, 0) == 0x102
                        finally:
                            k32.CloseHandle(h)
                    try:
                        os.kill(int(p), 0)
                        return True
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        return True
                    except OSError:
                        return False

                while time.monotonic() < deadline:
                    if not _alive(pid):
                        break
                    time.sleep(0.2)
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=windows_detach_flags_without_breakaway(),
                )
                """
            ).strip()
            watcher_env = os.environ.copy()
            # This watcher is intentionally outside the running gateway. If it
            # inherits the gateway marker, `hermes gateway restart` refuses to
            # run as a self-restart loop guard and the gateway stays stopped.
            watcher_env.pop("_HERMES_GATEWAY", None)
            project_root = Path(__file__).resolve().parent.parent
            watcher_python = sys.executable
            try:
                # Prefer a real GUI-subsystem interpreter for the watcher
                # itself.  With uv venvs, ``python.exe`` can re-exec the base
                # console interpreter and flash even when the Popen carries
                # CREATE_NO_WINDOW; pythonw.exe avoids console allocation.
                from hermes_cli.gateway_windows import _resolve_detached_python

                watcher_python, _watcher_venv_dir, _watcher_site_packages = (
                    _resolve_detached_python(sys.executable)
                )
            except Exception:
                watcher_python = sys.executable
            venv_dir = Path(watcher_env.get("VIRTUAL_ENV") or project_root / "venv")
            site_packages = venv_dir / "Lib" / "site-packages"
            if site_packages.exists():
                watcher_env["VIRTUAL_ENV"] = str(venv_dir)
                pythonpath = [str(project_root), str(site_packages)]
                if watcher_env.get("PYTHONPATH"):
                    pythonpath.append(watcher_env["PYTHONPATH"])
                watcher_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
            subprocess.Popen(
                [watcher_python, "-c", watcher, str(current_pid), str(restart_after_s), *cmd_argv],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=watcher_env,
                **windows_detach_popen_kwargs(),
            )
            return

        cmd = " ".join(shlex.quote(part) for part in hermes_cmd)
        shell_cmd = (
            f"deadline=$(( $(date +%s) + {int(restart_after_s)} )); "
            f"while kill -0 {current_pid} 2>/dev/null && [ $(date +%s) -lt $deadline ]; do sleep 0.2; done; "
            f"{cmd} gateway restart"
        )
        # Same marker scrub as the Windows watcher above: this watcher runs
        # `hermes gateway restart` from outside the gateway, but it inherits
        # _HERMES_GATEWAY=1 from us, and the CLI's self-restart loop guard
        # refuses to run when that marker is set — silently (DEVNULL), so the
        # gateway stops and never comes back.
        watcher_env = os.environ.copy()
        watcher_env.pop("_HERMES_GATEWAY", None)
        setsid_bin = shutil.which("setsid")
        if setsid_bin:
            subprocess.Popen(
                [setsid_bin, "bash", "-lc", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=watcher_env,
                start_new_session=True,
            )
        else:
            subprocess.Popen(
                ["bash", "-lc", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=watcher_env,
                start_new_session=True,
            )

    def _launch_systemd_restart_shortcut(self) -> None:
        """Best-effort helper to bypass systemd's automatic restart delay.

        For planned in-chat restarts, the gateway exits cleanly so systemd does
        not record a failure.  However, units with RestartSteps still count
        automatic restarts and can delay repeated /restart tests.  A transient
        user service survives our cgroup teardown and explicitly starts the
        gateway as soon as this PID exits, while the unit keeps its normal
        backoff for real crash loops.
        """
        if sys.platform != "linux" or not os.environ.get("INVOCATION_ID"):
            return

        try:
            import shutil
            import subprocess

            systemd_run = shutil.which("systemd-run")
            systemctl = shutil.which("systemctl")
            if not systemd_run or not systemctl:
                return

            try:
                from hermes_cli.gateway import get_service_name

                service_name = get_service_name()
            except Exception:
                service_name = "hermes-gateway"

            current_pid = os.getpid()

            # Detect whether the gateway unit is registered as a system or
            # user service.  Daemon-style deployments are typically system
            # units (e.g. /etc/systemd/system/hermes-gateway.service), while
            # `hermes setup` under a non-root account may register a user
            # unit.  Hard-coding ``--user`` broke system-unit deployments:
            # systemctl returned an empty MainPID, the PID-equality check
            # below failed, and the planned-restart helper was never
            # launched — leaving the gateway dead until a manual reboot.
            def _query_pid(scope_flags):
                try:
                    out = subprocess.run(
                        [systemctl, *scope_flags, "show", service_name,
                         "--property=MainPID", "--value"],
                        capture_output=True, text=True, timeout=2,
                    )
                    return (out.stdout or "").strip()
                except Exception:
                    return ""

            system_pid = _query_pid([])
            user_pid = _query_pid(["--user"])
            if str(current_pid) == system_pid:
                scope_flags = []
                systemctl_scope = "systemctl"
            elif str(current_pid) == user_pid:
                scope_flags = ["--user"]
                systemctl_scope = "systemctl --user"
            else:
                # MainPID does not match in either scope — likely invoked
                # outside of systemd or the unit was renamed.  Bail out
                # rather than restart the wrong unit.
                return

            service_arg = shlex.quote(service_name)
            shell_cmd = (
                f"while kill -0 {current_pid} 2>/dev/null; do sleep 0.2; done; "
                f"{systemctl_scope} reset-failed {service_arg}; "
                f"{systemctl_scope} restart {service_arg}"
            )
            unit_name = f"{service_name}-planned-restart-{current_pid}".replace(".", "-")
            subprocess.Popen(
                [
                    systemd_run,
                    *scope_flags,
                    "--collect",
                    "--unit",
                    unit_name,
                    "/bin/sh",
                    "-lc",
                    shell_cmd,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(
                "Launched systemd planned-restart helper for %s (pid=%s, scope=%s)",
                service_name,
                current_pid,
                "user" if scope_flags else "system",
            )
        except Exception as e:
            logger.debug("Failed to launch systemd planned-restart helper: %s", e)

    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        if self._restart_task_started:
            return False
        self._restart_requested = True
        self._restart_detached = detached
        self._restart_via_service = via_service
        self._restart_task_started = True

        async def _run_restart() -> None:
            if detached:
                try:
                    await self._launch_detached_restart_command()
                except Exception as e:
                    logger.error("Failed to launch detached gateway restart helper: %s", e)
            await asyncio.sleep(0.05)
            await self.stop(restart=True, detached_restart=detached, service_restart=via_service)

        # _run_restart is a short-lived self-terminating task (calls stop()
        # then returns).  Don't add it to _background_tasks — _stop_impl
        # cancels all entries in that set, which would cancel _run_restart
        # while it's awaiting _stop_task, propagating CancelledError into
        # _stop_impl and preventing _shutdown_event.set() / _exit_code = 75.
        # See #12875.
        #
        # We still hold a strong reference in self._restart_task: a bare
        # asyncio.create_task() keeps only a weak reference, so the event
        # loop may garbage-collect a still-pending task mid-flight.  The
        # cancel loop in _stop_impl explicitly skips _restart_task for the
        # same reason it skips _stop_task.
        self._restart_task = asyncio.create_task(_run_restart())
        return True

    async def _handoff_watcher(self, interval: float = 2.0) -> None:
        """Background task that processes pending CLI→gateway session handoffs.

        Polls ``state.db`` for sessions in ``handoff_state='pending'`` and,
        for each one:

        1. Atomically claims it (pending → running).
        2. Resolves the destination platform's configured home channel.
        3. Re-binds the gateway's session_key for that home channel to the
           CLI's existing session_id via ``session_store.switch_session`` so
           the full role-aware transcript replays on the next agent turn.
        4. Forges a synthetic ``MessageEvent`` (``internal=True``) with a
           handoff-notice text and dispatches through the normal gateway
           message pipeline so the agent runs and replies on the platform.
        5. Marks the row ``completed`` (or ``failed`` with ``handoff_error``).

        The CLI process is poll-blocked on the row's terminal state and
        prints the result to the user.
        """
        # Initial delay so the gateway is fully connected to its platforms
        # before we try to dispatch handoffs through them.
        await asyncio.sleep(5)
        while self._running:
            try:
                if self._session_db is None:
                    await asyncio.sleep(interval)
                    continue
                pending = await self._session_db.list_pending_handoffs()
                for row in pending:
                    session_id = row.get("id")
                    if not session_id:
                        continue
                    if not await self._session_db.claim_handoff(session_id):
                        # Another tick or another gateway already claimed it.
                        continue
                    try:
                        await self._process_handoff(row)
                        await self._session_db.complete_handoff(session_id)
                    except Exception as exc:
                        logger.warning(
                            "Handoff for session %s failed: %s",
                            session_id, exc, exc_info=True,
                        )
                        await self._session_db.fail_handoff(session_id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Handoff watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    async def _process_handoff(self, row: Dict[str, Any]) -> None:
        """Execute one handoff row. Raises on failure (caller marks failed)."""
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent

        cli_session_id = row["id"]
        platform_name = (row.get("handoff_platform") or "").strip().lower()
        if not platform_name:
            raise RuntimeError("handoff_platform is empty")

        # Resolve platform enum
        try:
            platform = Platform(platform_name)
        except (ValueError, KeyError):
            raise RuntimeError(f"unknown platform '{platform_name}'")

        # Adapter must be live
        adapter = self.adapters.get(platform)
        if not adapter:
            raise RuntimeError(
                f"platform '{platform_name}' is not active in this gateway"
            )

        # Home channel must be configured
        home = self.config.get_home_channel(platform)
        if not home or not home.chat_id:
            raise RuntimeError(
                f"no home channel configured for {platform_name}; "
                f"run /sethome on the desired chat first"
            )

        cli_title = row.get("title") or cli_session_id[:8]

        # Try to create a fresh thread on the destination so the handoff
        # has its own scrollback. Adapter returns None if threading isn't
        # supported (Matrix/WhatsApp/Signal/SMS) or if creation failed
        # (no permission, topics-mode off, parent is a DM, etc.). When
        # None we fall through to using the home channel directly — the
        # synthetic turn still lands; just without thread isolation.
        thread_name = f"Hermes — {cli_title}"
        try:
            new_thread_id = await adapter.create_handoff_thread(
                str(home.chat_id), thread_name,
            )
        except Exception as exc:
            logger.debug(
                "Handoff: create_handoff_thread raised on %s: %s",
                platform_name, exc, exc_info=True,
            )
            new_thread_id = None

        # Use the new thread if the adapter created one; otherwise fall
        # back to whatever thread (if any) the home channel was configured
        # with.
        effective_thread_id = new_thread_id or (
            str(home.thread_id) if home.thread_id else None
        )

        # Determine chat_type/user_id for the destination source.
        #
        # Telegram private-chat DM topics are represented differently from
        # group/forum threads by the inbound adapter. A handoff-created topic
        # in a positive Telegram chat_id must therefore use the same DM-topic
        # source shape as the user's next real message; otherwise the synthetic
        # handoff turn binds a generic `thread` session key while real replies
        # arrive on a `dm` session key.
        home_chat_id = str(home.chat_id)
        is_telegram_private_chat = (
            platform == Platform.TELEGRAM
            and looks_like_telegram_private_chat_id(home_chat_id)
        )

        if new_thread_id and not is_telegram_private_chat:
            dest_chat_type = "thread"
            dest_user_id = "system:handoff"
        else:
            # No thread — assume DM-style for the home channel. For Telegram
            # private-chat topics, use the real user id (same as chat_id) so
            # topic-mode checks and binding persistence see the same identity as
            # subsequent inbound user messages.
            dest_chat_type = "dm"
            dest_user_id = home_chat_id if is_telegram_private_chat else "system:handoff"

        dest_source = SessionSource(
            platform=platform,
            chat_id=home_chat_id,
            chat_name=home.name,
            chat_type=dest_chat_type,
            user_id=dest_user_id,
            user_name="Handoff",
            thread_id=effective_thread_id,
        )

        # Compute the gateway's session_key for that destination using the
        # same rules its adapters use, so switch_session targets the right
        # entry. For thread destinations build_session_key keys without
        # user_id (thread_sessions_per_user defaults to False) — so the
        # next real user message in the thread shares this same session.
        platform_cfg = self.config.platforms.get(platform)
        extra = platform_cfg.extra if platform_cfg else {}
        session_key = build_session_key(
            dest_source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )

        # Make sure there's an entry in the session_store for this key. If
        # the home channel has never been used, get_or_create_session
        # creates one; switch_session then re-points it.
        self.session_store.get_or_create_session(dest_source)

        # Re-bind the destination key to the CLI session_id. switch_session
        # ends the prior session in SQLite and reopens the CLI session under
        # the new key. The CLI's transcript becomes the active one for the
        # gateway from this moment on.
        switched = self.session_store.switch_session(session_key, cli_session_id)
        if switched is None:
            raise RuntimeError(
                f"could not switch session key {session_key} → {cli_session_id}"
            )

        # Evict any cached AIAgent for this session_key so the next dispatch
        # rebuilds it against the CLI session_id (mirrors /resume / /branch).
        self._evict_cached_agent(session_key)

        # Cancel any in-flight running-agent state for the destination key
        # so the synthetic turn isn't queued behind a stale running flag.
        self._release_running_agent_state(session_key)

        synthetic_text = (
            f"[Session was just handed off from CLI (\"{cli_title}\") to this "
            f"channel. The full prior conversation history is loaded above. "
            f"Briefly confirm you're working here and summarize what we were "
            f"working on, so the user can continue from this device.]"
        )

        synthetic_event = MessageEvent(
            text=synthetic_text,
            source=dest_source,
            internal=True,
        )

        logger.info(
            "Handoff: dispatching synthetic turn for CLI session %s → %s "
            "(home=%s, thread=%s, session_key=%s)",
            cli_session_id, platform_name, home.chat_id, effective_thread_id,
            session_key,
        )

        # Dispatch through the runner directly. Going through
        # adapter.handle_message would spawn a background task and we'd
        # lose synchronous error visibility; calling _handle_message inline
        # keeps the success/failure path observable for the watcher.
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            # Streaming may have already delivered the response inline.
            # Either way, agent ran without raising — count as success.
            return

        # Send the agent's reply to the destination. Route to the new
        # thread if we created one; otherwise the configured home channel
        # (which may itself carry a thread_id).
        send_metadata: Dict[str, Any] = {}
        if effective_thread_id:
            send_metadata["thread_id"] = effective_thread_id
        try:
            result = await adapter.send(
                chat_id=str(home.chat_id),
                content=response_text,
                metadata=send_metadata or None,
            )
        except Exception as exc:
            raise RuntimeError(f"adapter.send failed: {exc}") from exc

        if not getattr(result, "success", True):
            err = getattr(result, "error", "send returned success=False")
            raise RuntimeError(f"adapter.send failed: {err}")

    async def _session_expiry_watcher(self, interval: int = 300):
        """Background task that finalizes expired sessions.

        Runs every ``interval`` seconds (default 5 min).  For each session
        whose reset policy has expired, invokes ``on_session_finalize``
        hooks, cleans up the cached AIAgent's tool resources, evicts the
        cache entry so it can be garbage-collected, and marks the session
        so it won't be finalized again.
        """
        await asyncio.sleep(60)  # initial delay — let the gateway fully start
        _finalize_failures: dict[str, int] = {}  # session_id -> consecutive failure count
        _MAX_FINALIZE_RETRIES = 3
        while self._running:
            try:
                self.session_store._ensure_loaded()
                # Collect expired sessions first, then log a single summary.
                _expired_entries = []
                for key, entry in list(self.session_store._entries.items()):
                    if entry.expiry_finalized:
                        continue
                    if not self.session_store._is_session_expired(entry):
                        continue
                    _expired_entries.append((key, entry))

                if _expired_entries:
                    # Extract platform names from session keys for a compact summary.
                    # Keys look like "agent:main:telegram:dm:12345" — platform is field [2].
                    _platforms: dict[str, int] = {}
                    for _k, _e in _expired_entries:
                        _parts = _k.split(":")
                        _plat = _parts[2] if len(_parts) > 2 else "unknown"
                        _platforms[_plat] = _platforms.get(_plat, 0) + 1
                    _plat_summary = ", ".join(
                        f"{p}:{c}" for p, c in sorted(_platforms.items())
                    )
                    logger.info(
                        "Session expiry: %d sessions to finalize (%s)",
                        len(_expired_entries), _plat_summary,
                    )

                for key, entry in _expired_entries:
                    try:
                        try:
                            from hermes_cli.plugins import invoke_hook as _invoke_hook
                            _parts = key.split(":")
                            _platform = _parts[2] if len(_parts) > 2 else ""
                            _invoke_hook(
                                "on_session_finalize",
                                session_id=entry.session_id,
                                platform=_platform,
                                reason="session_expired",
                            )
                        except Exception:
                            pass
                        # Shut down memory provider and close tool resources
                        # on the cached agent.  Idle agents live in
                        # _agent_cache (not _running_agents), so look there.
                        _cached_agent = None
                        _cache_lock = getattr(self, "_agent_cache_lock", None)
                        if _cache_lock is not None:
                            with _cache_lock:
                                _cached = self._agent_cache.get(key)
                                _cached_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
                        # Fall back to _running_agents in case the agent is
                        # still mid-turn when the expiry fires.
                        if _cached_agent is None:
                            _cached_agent = self._running_agents.get(key)
                        if _cached_agent and _cached_agent is not _AGENT_PENDING_SENTINEL:
                            await self._cleanup_agent_resources_off_loop(
                                _cached_agent, context="session expiry"
                            )
                        # Drop the cache entry so the AIAgent (and its LLM
                        # clients, tool schemas, memory provider refs) can
                        # be garbage-collected.  Otherwise the cache grows
                        # unbounded across the gateway's lifetime.
                        self._evict_cached_agent(key)
                        # Permanently finalizing this session — drop its
                        # per-session control state so the dicts don't grow
                        # unbounded across the gateway's lifetime. (Idle
                        # agent-cache eviction must NOT prune these: the
                        # session is still alive and a resumed turn rebuilds
                        # its agent from these overrides. Only true session
                        # finalization, /new, and /reset clear them.)
                        self._session_model_overrides.pop(key, None)
                        self._set_session_reasoning_override(key, None)
                        if hasattr(self, "_pending_model_notes"):
                            self._pending_model_notes.pop(key, None)
                        # Clear per-session model cache so a resumed turn
                        # resolves from current config, not a stale fallback
                        # cached before the session went idle (mirrors /new
                        # and the compression-exhausted auto-reset, #58403).
                        _lrm = getattr(self, "_last_resolved_model", None)
                        if _lrm is not None:
                            _lrm.pop(key, None)
                        _pending_approvals = getattr(self, "_pending_approvals", None)
                        if isinstance(_pending_approvals, dict):
                            _pending_approvals.pop(key, None)
                        _update_prompt_pending = getattr(self, "_update_prompt_pending", None)
                        if isinstance(_update_prompt_pending, dict):
                            _update_prompt_pending.pop(key, None)
                        # Persist the finalized flag to sessions.json AND
                        # state.db (single write-path, #9006) — also drops
                        # the persisted /model override, since finalization
                        # is a conversation boundary.
                        self.session_store.set_expiry_finalized(entry)
                        logger.debug(
                            "Session expiry finalized for %s",
                            entry.session_id,
                        )
                        _finalize_failures.pop(entry.session_id, None)
                    except Exception as e:
                        failures = _finalize_failures.get(entry.session_id, 0) + 1
                        _finalize_failures[entry.session_id] = failures
                        if failures >= _MAX_FINALIZE_RETRIES:
                            logger.warning(
                                "Session finalize gave up after %d attempts for %s: %s. "
                                "Marking as finalized to prevent infinite retry loop.",
                                failures, entry.session_id, e,
                            )
                            self.session_store.set_expiry_finalized(
                                entry, clear_model_override=False
                            )
                            _finalize_failures.pop(entry.session_id, None)
                        else:
                            logger.debug(
                                "Session finalize failed (%d/%d) for %s: %s",
                                failures, _MAX_FINALIZE_RETRIES, entry.session_id, e,
                            )

                if _expired_entries:
                    _done = sum(
                        1 for _, e in _expired_entries if e.expiry_finalized
                    )
                    _failed = len(_expired_entries) - _done
                    if _failed:
                        logger.info(
                            "Session expiry done: %d finalized, %d pending retry",
                            _done, _failed,
                        )
                    else:
                        logger.info(
                            "Session expiry done: %d finalized", _done,
                        )

                # Sweep agents that have been idle beyond the TTL regardless
                # of session reset policy.  This catches sessions with very
                # long / "never" reset windows, whose cached AIAgents would
                # otherwise pin memory for the gateway's entire lifetime.
                try:
                    _idle_evicted = self._sweep_idle_cached_agents()
                    if _idle_evicted:
                        logger.info(
                            "Agent cache idle sweep: evicted %d agent(s)",
                            _idle_evicted,
                        )
                except Exception as _e:
                    logger.debug("Idle agent sweep failed: %s", _e)

                # Periodically prune stale SessionStore entries.  The
                # in-memory dict (and sessions.json) would otherwise grow
                # unbounded in gateways serving many rotating chats /
                # threads / users over long time windows.  Pruning is
                # invisible to users — a resumed session just gets a
                # fresh session_id, exactly as if the reset policy fired.
                _last_prune_ts = getattr(self, "_last_session_store_prune_ts", 0.0)
                _prune_interval = 3600.0  # once per hour
                if time.time() - _last_prune_ts > _prune_interval:
                    try:
                        _max_age = int(
                            getattr(self.config, "session_store_max_age_days", 0) or 0
                        )
                        if _max_age > 0:
                            _pruned = self.session_store.prune_old_entries(_max_age)
                            if _pruned:
                                logger.info(
                                    "SessionStore prune: dropped %d stale entries",
                                    _pruned,
                                )
                    except Exception as _e:
                        logger.debug("SessionStore prune failed: %s", _e)
                    self._last_session_store_prune_ts = time.time()
            except Exception as e:
                logger.debug("Session expiry watcher error: %s", e)
            # Sleep in small increments so we can stop quickly
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"

    async def _platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms.

        Uses exponential backoff: 30s → 60s → 120s → 240s → 300s (cap).
        Retryable failures (network/DNS blips) keep retrying at the backoff
        cap indefinitely — they self-heal once connectivity returns, so a
        transient outage never requires manual intervention. Non-retryable
        failures (bad auth, etc.) drop out of the queue immediately. The
        circuit breaker (``_pause_failed_platform`` / ``/platform pause``)
        remains available for manual operator control via ``/platform list``
        and ``/platform resume <name>``, but is no longer triggered
        automatically — auto-pausing a recovered platform was the cause of
        bots silently staying dead after a transient DNS failure.
        """
        _BACKOFF_CAP = 300  # 5 minutes max between retries

        await asyncio.sleep(10)  # initial delay — let startup finish
        while self._running:
            if not self._failed_platforms:
                # Nothing to reconnect — sleep and check again
                for _ in range(30):
                    if not self._running:
                        return
                    if self._failed_platforms:
                        break
                    await asyncio.sleep(1)
                continue

            now = time.monotonic()
            for platform in list(self._failed_platforms.keys()):
                if not self._running:
                    return
                info = self._failed_platforms[platform]
                # Skip paused platforms entirely — they need explicit
                # /platform resume to come back.
                if info.get("paused"):
                    continue
                if now < info["next_retry"]:
                    continue  # not time yet

                platform_config = info["config"]
                attempt = info["attempts"] + 1
                logger.info(
                    "Reconnecting %s (attempt %d)...",
                    platform.value, attempt,
                )

                adapter = None
                try:
                    adapter = self._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning(
                            "Reconnect %s: adapter creation returned None, removing from retry queue",
                            platform.value,
                        )
                        del self._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(self._handle_message)
                    adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
                    adapter.set_session_store(self.session_store)
                    adapter.set_busy_session_handler(self._handle_active_session_busy_message)
                    adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
                    adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
                    adapter._busy_text_mode = self._busy_text_mode

                    # Reconnect after an outage: preserve the platform's
                    # server-side update queue so messages sent while the bot
                    # was offline are delivered rather than dropped (#46621).
                    success = await self._connect_adapter_with_timeout(
                        adapter, platform, is_reconnect=True
                    )
                    if success:
                        self.adapters[platform] = adapter
                        self._sync_voice_mode_state_to_adapter(adapter)
                        self.delivery_router.adapters = self.adapters
                        del self._failed_platforms[platform]
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="connected",
                            error_code=None,
                            error_message=None,
                        )
                        logger.info("✓ %s reconnected successfully", platform.value)

                        # Rebuild channel directory with the new adapter
                        try:
                            from gateway.channel_directory import build_channel_directory
                            await build_channel_directory(self.adapters)
                        except Exception:
                            pass

                        # A platform that was offline at gateway startup never
                        # got its restart-interrupted sessions auto-resumed —
                        # the startup pass skips sessions whose adapter isn't
                        # connected yet. Now that it's back, retry the
                        # auto-resume scoped to this platform so recovery
                        # doesn't silently wait for a manual user message.
                        try:
                            self._schedule_resume_pending_sessions(platform=platform)
                        except Exception:
                            logger.debug(
                                "resume-pending reschedule after %s reconnect failed",
                                platform.value,
                                exc_info=True,
                            )
                    # Check if the failure is non-retryable
                    elif adapter.has_fatal_error and not adapter.fatal_error_retryable:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="fatal",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message,
                        )
                        logger.warning(
                            "Reconnect %s: non-retryable error (%s), removing from retry queue",
                            platform.value, adapter.fatal_error_message,
                        )
                        # The adapter is about to be dropped from the queue
                        # without ever being installed on self.adapters, so
                        # nothing else will call disconnect() on it. We must
                        # dispose it here, otherwise the resource owners it
                        # constructed in __init__ (ResponseStore for
                        # APIServerAdapter, etc.) leak 2 fds each. The
                        # gateway hits the 2560-fd limit after ~12h of
                        # failed reconnects at the 300s backoff cap (#37011).
                        await _dispose_unused_adapter(adapter)
                        del self._failed_platforms[platform]
                    else:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message or "failed to reconnect",
                        )
                        backoff = min(30 * (2 ** (attempt - 1)), _BACKOFF_CAP)
                        info["attempts"] = attempt
                        info["next_retry"] = time.monotonic() + backoff
                        logger.info(
                            "Reconnect %s failed, next retry in %ds",
                            platform.value, backoff,
                        )
                        # Same fd-leak concern as the non-retryable branch
                        # above: the adapter failed to connect and is being
                        # thrown away. Without an explicit dispose call, the
                        # resources it opened in __init__ stay open until
                        # the next GC pass — and aiohttp/SQLite handles
                        # don't get GC'd promptly, so 2 fds/retry leak at
                        # 300s backoff cap = ~12 fds/hour (#37011).
                        await _dispose_unused_adapter(adapter)
                        # Retryable failures (network/DNS blips) keep retrying
                        # at the backoff cap indefinitely — they self-heal once
                        # connectivity returns. We do NOT auto-pause them: a
                        # transient outage must never require manual `/platform
                        # resume` to recover. Non-retryable failures (bad auth,
                        # etc.) already drop out of the queue via the
                        # `not fatal_error_retryable` branch above, so anything
                        # reaching here is by definition retryable.
                except Exception as e:
                    if adapter is not None:
                        # An exception escaping the connect call path
                        # (DNS timeout, aiohttp server.start() crash, etc.)
                        # leaves the adapter in the same unowned state as
                        # the two branches above. Dispose so __init__
                        # resources don't accumulate while the watcher
                        # keeps retrying.
                        await _dispose_unused_adapter(adapter)
                    self._update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        error_code=None,
                        error_message=str(e),
                    )
                    backoff = min(30 * (2 ** (attempt - 1)), _BACKOFF_CAP)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning(
                        "Reconnect %s error: %s, next retry in %ds",
                        platform.value, e, backoff,
                    )
                    # A raised exception during reconnect (connect timeout, DNS
                    # resolution failure, etc.) is inherently transient — keep
                    # retrying at the backoff cap rather than auto-pausing.

            # Check every 10 seconds for platforms that need reconnection
            for _ in range(10):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _start_secondary_profile_adapters(self) -> int:
        """Bring up adapters for every non-active profile this gateway serves.

        Returns the number of secondary adapters that connected. No-op (returns
        0) unless ``gateway.multiplex_profiles`` is on.

        Each profile's adapters are created and connected under that profile's
        HERMES_HOME + secret scope (``_profile_runtime_scope``), stored in
        ``self._profile_adapters[profile]``, and given a message handler that
        stamps ``source.profile`` before delegating to the shared
        ``_handle_message`` — so the agent turn resolves that profile's config,
        skills, and credentials. Same-platform credential collisions (two
        profiles polling the same bot token) are detected and refused here, the
        only point that sees every profile's resolved credentials together.
        """
        if not getattr(self.config, "multiplex_profiles", False):
            return 0

        try:
            from hermes_cli.profiles import profiles_to_serve, get_active_profile_name
        except Exception:
            return 0

        active = get_active_profile_name() or "default"
        connected = 0
        # (platform, token-fingerprint) -> profile that claimed it. Detects two
        # profiles trying to poll the same bot credential (impossible to do
        # concurrently). Seed with the active profile's adapters.
        claimed: Dict[tuple, str] = {}
        for _plat, _ad in self.adapters.items():
            fp = self._adapter_credential_fingerprint(_ad)
            if fp is not None:
                claimed[(_plat, fp)] = active

        for profile_name, profile_home in profiles_to_serve(multiplex=True):
            if profile_name == active:
                continue  # handled by the primary startup loop
            try:
                connected += await self._start_one_profile_adapters(
                    profile_name, profile_home, claimed
                )
            except MultiplexConfigError:
                # Config error (e.g. a secondary profile binding a port) is not
                # transient — propagate so startup aborts cleanly instead of
                # limping along with a half-configured multiplexer.
                raise
            except Exception as e:
                logger.error(
                    "Failed to start adapters for profile '%s': %s",
                    profile_name, e, exc_info=True,
                )

        # Record served profiles in runtime status for `hermes status`.
        try:
            from gateway.status import write_runtime_status
            served = [active] + sorted(self._profile_adapters.keys())
            # Per-profile PairingStores so authz_mixin can route pairing
            # checks to the right whitelist. The active profile gets a store
            # at its HERMES_HOME; additional served profiles get one under
            # profiles/<name>/pairing/. See gateway.pairing.PairingStore.
            for name in served:
                if name and name not in self.pairing_stores:
                    self.pairing_stores[name] = PairingStore(profile=name)
            write_runtime_status(served_profiles=served)
        except Exception:
            logger.debug("could not record served_profiles", exc_info=True)

        return connected

    async def _start_one_profile_adapters(
        self, profile_name: str, profile_home: "Path", claimed: Dict[tuple, str]
    ) -> int:
        """Create+connect one profile's adapters under its runtime scope."""
        from gateway.config import load_gateway_config

        with _profile_runtime_scope(profile_home):
            profile_cfg = load_gateway_config()
            violation = _own_policy_open_startup_violation(profile_cfg)
        if violation:
            raise MultiplexConfigError(
                f"Profile '{profile_name}' enables {violation}. "
                "Enable GATEWAY_ALLOW_ALL_USERS or the platform allow-all flag "
                "for that profile, or change dm_policy/group_policy away from "
                "'open'."
            )

        profile_map = self._profile_adapters.setdefault(profile_name, {})
        connected = 0
        for platform, platform_config in profile_cfg.platforms.items():
            if not platform_config.enabled:
                continue
            # A secondary profile must NOT enable a port-binding platform: the
            # default profile's listener already serves every profile via the
            # /p/<profile>/ prefix, so a second bind can only collide. This is a
            # config error, not a transient failure — fail fast and loud.
            if platform.value in _PORT_BINDING_PLATFORM_VALUES:
                raise MultiplexConfigError(
                    f"Profile '{profile_name}' enables the port-binding platform "
                    f"'{platform.value}', but gateway.multiplex_profiles is on. The "
                    f"default profile owns the single shared HTTP listener and "
                    f"serves every profile through the /p/{profile_name}/ URL "
                    f"prefix — a secondary profile cannot bind its own port. "
                    f"Remove platforms.{platform.value} from profile "
                    f"'{profile_name}'s config.yaml (configure it only on the "
                    f"default profile)."
                )
            with _profile_runtime_scope(profile_home):
                adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                continue

            # Same-token conflict detection — refuse a duplicate poll.
            fp = self._adapter_credential_fingerprint(adapter)
            if fp is not None:
                owner = claimed.get((platform, fp))
                if owner is not None:
                    logger.error(
                        "Profile '%s' and '%s' both configure %s with the same "
                        "credential — refusing to start the duplicate (a single "
                        "bot token cannot be polled twice). Give each profile its "
                        "own %s credential.",
                        owner, profile_name, platform.value, platform.value,
                    )
                    await self._safe_adapter_disconnect(adapter, platform)
                    continue
                claimed[(platform, fp)] = profile_name

            # Stamp every inbound event from this adapter with its profile so
            # the agent turn (and session key) resolve to the right home.
            adapter.set_message_handler(
                self._make_profile_message_handler(profile_name)
            )
            adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
            adapter.set_session_store(self.session_store)
            adapter.set_busy_session_handler(self._handle_active_session_busy_message)
            adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
            adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
            adapter._busy_text_mode = self._busy_text_mode

            try:
                with _profile_runtime_scope(profile_home):
                    success = await self._connect_adapter_with_timeout(adapter, platform)
                if success:
                    profile_map[platform] = adapter
                    connected += 1
                    logger.info("✓ %s connected (profile: %s)", platform.value, profile_name)
                else:
                    logger.warning("✗ %s failed to connect (profile: %s)", platform.value, profile_name)
                    await self._safe_adapter_disconnect(adapter, platform)
            except Exception as e:
                logger.error("✗ %s error (profile: %s): %s", platform.value, profile_name, e)
                await self._safe_adapter_disconnect(adapter, platform)
        return connected

    def _make_profile_message_handler(self, profile_name: str):
        """Return a message handler that stamps source.profile then delegates."""
        async def _handler(event):
            try:
                if getattr(event, "source", None) is not None and not event.source.profile:
                    event.source.profile = profile_name
            except Exception:
                pass
            return await self._handle_message(event)
        return _handler

    @staticmethod
    def _adapter_credential_fingerprint(adapter: Any) -> Optional[str]:
        """Return a stable, log-safe fingerprint of an adapter's credential.

        Used only to detect two profiles claiming the same bot token. Returns a
        salted hash (never the token itself) of the adapter's primary
        credential, or None when no credential is discoverable (in which case
        we don't attempt conflict detection for it).
        """
        token = None
        for attr in ("token", "bot_token", "_token", "api_token", "_bot_token"):
            val = getattr(adapter, attr, None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
                break
        if not token:
            config = getattr(adapter, "config", None)
            val = getattr(config, "token", None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
        if not token:
            return None
        import hashlib
        return hashlib.sha256(("hermes-mux:" + token).encode("utf-8")).hexdigest()[:16]

    def _create_adapter(
        self, 
        platform: Platform, 
        config: Any
    ) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform.

        Checks the platform_registry first (plugin adapters), then falls
        through to the built-in if/elif chain for core platforms.
        """
        if hasattr(config, "extra") and isinstance(config.extra, dict):
            config.extra.setdefault(
                "group_sessions_per_user",
                self.config.group_sessions_per_user,
            )
            config.extra.setdefault(
                "thread_sessions_per_user",
                getattr(self.config, "thread_sessions_per_user", False),
            )

        # ── Plugin-registered platforms (checked first) ───────────────────
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(platform.value):
                adapter = platform_registry.create_adapter(platform.value, config)
                if adapter is not None:
                    # Adapters that need a back-reference to the gateway runner
                    # (e.g. for cross-platform admin alerts) declare a
                    # ``gateway_runner`` attribute. Inject it after creation so
                    # plugin adapters don't need a custom factory signature.
                    if hasattr(adapter, "gateway_runner"):
                        adapter.gateway_runner = self
                    return adapter
                # Registered but failed to instantiate — don't silently fall
                # through to built-ins (there are none for plugin platforms).
                logger.error(
                    "Platform '%s' is registered but adapter creation failed "
                    "(check dependencies and config)",
                    platform.value,
                )
                return None
        except Exception as e:
            logger.debug("Platform registry lookup for '%s' failed: %s", platform.value, e)
        # Fall through to built-in adapters below

        if platform == Platform.WHATSAPP_CLOUD:
            from gateway.platforms.whatsapp_cloud import (
                WhatsAppCloudAdapter,
                check_whatsapp_cloud_requirements,
            )
            if not check_whatsapp_cloud_requirements():
                logger.warning(
                    "WhatsApp Cloud: aiohttp/httpx missing — reinstall hermes-agent"
                )
                return None
            return WhatsAppCloudAdapter(config)
        
        elif platform == Platform.SIGNAL:
            from gateway.platforms.signal import SignalAdapter, check_signal_requirements
            if not check_signal_requirements():
                logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
                return None
            return SignalAdapter(config)

        elif platform == Platform.WEIXIN:
            from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
            if not check_weixin_requirements():
                logger.warning("Weixin: aiohttp/cryptography not installed")
                return None
            return WeixinAdapter(config)

        elif platform == Platform.API_SERVER:
            from gateway.platforms.api_server import APIServerAdapter, check_api_server_requirements
            if not check_api_server_requirements():
                logger.warning("API Server: aiohttp not installed")
                return None
            return APIServerAdapter(config)

        elif platform == Platform.WEBHOOK:
            from gateway.platforms.webhook import WebhookAdapter, check_webhook_requirements
            if not check_webhook_requirements():
                logger.warning("Webhook: aiohttp not installed")
                return None
            adapter = WebhookAdapter(config)
            adapter.gateway_runner = self  # For cross-platform delivery
            return adapter

        elif platform == Platform.MSGRAPH_WEBHOOK:
            from gateway.platforms.msgraph_webhook import (
                MSGraphWebhookAdapter,
                check_msgraph_webhook_requirements,
            )
            if not check_msgraph_webhook_requirements():
                logger.warning("MSGraph webhook: aiohttp not installed")
                return None
            return MSGraphWebhookAdapter(config)

        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
                return None
            return BlueBubblesAdapter(config)

        elif platform == Platform.QQBOT:
            from gateway.platforms.qqbot import QQAdapter, check_qq_requirements
            if not check_qq_requirements():
                logger.warning("QQBot: aiohttp/httpx missing or QQ_APP_ID/QQ_CLIENT_SECRET not configured")
                return None
            return QQAdapter(config)

            return YuanbaoAdapter(config)
        return None

    def _make_adapter_auth_check(
        self,
        platform: Platform,
    ) -> Callable[[str, Optional[str], Optional[str]], bool]:
        """Build a platform-bound auth callback for adapter use.

        Adapters that fetch external context (e.g. Slack
        ``conversations.replies``) call this through
        ``BasePlatformAdapter._is_sender_authorized`` to mark non-allowlisted
        senders as unverified in LLM context, mitigating indirect prompt
        injection from third parties in shared threads/channels.

        The returned callback delegates to :meth:`_is_user_authorized` so the
        full auth chain — platform allowlists, group allowlists, pairing
        store, allow-all flags — stays the single source of truth.
        """
        def check(
            user_id: str,
            chat_type: Optional[str] = None,
            chat_id: Optional[str] = None,
        ) -> bool:
            if not user_id:
                return False
            source = SessionSource(
                platform=platform,
                chat_id=chat_id or "",
                chat_type=chat_type or "group",
                user_id=user_id,
            )
            return self._is_user_authorized(source)
        return check

    async def _deliver_platform_notice(self, source, content: str) -> None:
        """Deliver a setup/operational notice using platform-specific privacy rules."""
        adapter = self._adapter_for_source(source)
        if not adapter:
            return

        config = getattr(self, "config", None)
        notice_delivery = "public"
        if config and hasattr(config, "get_notice_delivery"):
            notice_delivery = config.get_notice_delivery(source.platform)

        metadata = self._thread_metadata_for_source(source)
        if notice_delivery == "private" and getattr(source, "user_id", None):
            try:
                result = await adapter.send_private_notice(
                    source.chat_id,
                    source.user_id,
                    content,
                    metadata=metadata,
                )
                if getattr(result, "success", False):
                    return
            except Exception:
                logger.debug(
                    "[%s] send_private_notice failed, falling back to public",
                    getattr(source, "platform", "?"),
                    exc_info=True,
                )

        await adapter.send(source.chat_id, content, metadata=metadata)

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[str] = None,
        persist_user_timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Profile-scoping wrapper around the agent run.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole turn inside ``_profile_runtime_scope`` so config/skills/
        memory resolve to that profile's home AND credentials resolve from that
        profile's secret scope (never the process-global ``os.environ``). When
        multiplexing is off this is a transparent pass-through — zero behavior
        change for single-profile gateways.
        """
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key, run_generation=run_generation,
                _interrupt_depth=_interrupt_depth, event_message_id=event_message_id,
                channel_prompt=channel_prompt, moa_config=moa_config,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
            )

        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key, run_generation=run_generation,
                _interrupt_depth=_interrupt_depth, event_message_id=event_message_id,
                channel_prompt=channel_prompt, moa_config=moa_config,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
            )

    async def _run_agent_inner(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[str] = None,
        persist_user_timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.
        
        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool
        
        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        # ---- Proxy mode: delegate to remote API server ----
        if self._get_proxy_url():
            return await self._run_agent_via_proxy(
                message=message,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=event_message_id,
            )

        from run_agent import AIAgent
        import queue

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)
        
        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)

        from hermes_cli.tools_config import _get_platform_tools
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        agent_cfg_local = user_config.get("agent") or {}
        disabled_toolsets = agent_cfg_local.get("disabled_toolsets") or None

        display_config = user_config.get("display", {})
        if not isinstance(display_config, dict):
            display_config = {}

        # Per-platform display settings — resolve via display_config module
        # which checks display.platforms.<platform>.<key> first, then
        # display.<key> global, then built-in platform defaults.
        from gateway.display_config import resolve_display_setting

        # Apply tool preview length config (0 = no limit)
        try:
            from agent.display import set_tool_preview_max_len
            _tpl = resolve_display_setting(user_config, platform_key, "tool_preview_length", 0)
            set_tool_preview_max_len(int(_tpl) if _tpl else 0)
        except Exception:
            pass

        # Apply friendly tool labels config (default on) — per-platform aware
        try:
            from agent.display import set_friendly_tool_labels
            _ftl = resolve_display_setting(user_config, platform_key, "friendly_tool_labels", True)
            set_friendly_tool_labels(bool(_ftl))
        except Exception:
            pass

        # Tool progress mode — resolved per-platform with env var fallback
        _resolved_tp = resolve_display_setting(user_config, platform_key, "tool_progress")
        _env_tp = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        _display_cfg = display_config if isinstance(display_config, dict) else {}
        _platforms_cfg = _display_cfg.get("platforms") or {}
        _platform_cfg = _platforms_cfg.get(platform_key) or {}
        _legacy_tp_overrides = _display_cfg.get("tool_progress_overrides") or {}
        _tool_progress_configured = (
            "tool_progress" in _display_cfg
            or (
                isinstance(_platform_cfg, dict)
                and "tool_progress" in _platform_cfg
            )
            or (
                isinstance(_legacy_tp_overrides, dict)
                and platform_key in _legacy_tp_overrides
            )
        )
        progress_mode = (
            _env_tp
            if _env_tp and not _tool_progress_configured
            else (_resolved_tp or _env_tp or "all")
        )
        # Tool progress grouping: "accumulate" (edit one bubble) or "separate" (one msg per tool)
        progress_grouping = resolve_display_setting(user_config, platform_key, "tool_progress_grouping") or "accumulate"
        from gateway.status_phrases import choose_status_phrase, resolve_status_phrase_catalog
        _generic_status_recent: List[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(
            setting: str,
            *,
            default: bool = False,
            require_platform_override_for: set[Any] | None = None,
            allow_generic: bool = False,
        ) -> str:
            """Return off|raw|generic for a gateway visibility surface."""
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {
                    _gateway_platform_value(item)
                    for item in require_platform_override_for
                }
                if (
                    current_platform in platform_only
                    and not _has_platform_display_override(user_config, platform_key, setting)
                ):
                    return "off"
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str) and value.strip().lower() == "generic":
                return "generic" if allow_generic else "off"
            return "raw" if bool(value) else "off"

        def _generic_status_phrase(kind: str, *, tool_name: str | None = None, preview: str | None = None, args: Any = None) -> str:
            try:
                return choose_status_phrase(
                    kind,
                    tool_name=tool_name,
                    preview=preview,
                    args=args,
                    recent=_generic_status_recent,
                    catalog=_generic_status_catalog,
                )
            except Exception as _phrase_err:
                logger.debug("generic status phrase selection failed: %s", _phrase_err)
                return "still on it" if kind in {"heartbeat", "waiting", "long_running", "status"} else "one sec"
        # Disable tool progress for webhooks - they don't support message editing,
        # so each progress line would be sent as a separate message.
        from gateway.config import Platform
        tool_progress_enabled = progress_mode not in {"off", "log"} and source.platform != Platform.WEBHOOK
        # "log" mode: tool calls are written to ~/.hermes/logs/tool_calls.log
        # instead of the chat (#3459 / #3458). Gateway-only by design.
        log_mode_enabled = progress_mode == "log" and source.platform != Platform.WEBHOOK
        log_queue: "queue.Queue | None" = queue.Queue() if log_mode_enabled else None
        # Natural assistant status messages are intentionally independent from
        # tool progress and token streaming. Users can keep tool_progress quiet
        # in chat platforms while opting into concise mid-turn updates.
        interim_assistant_messages_mode = _display_surface_mode(
            "interim_assistant_messages",
            default=True,
            require_platform_override_for=set(),
        )
        interim_assistant_messages_enabled = (
            source.platform != Platform.WEBHOOK
            and interim_assistant_messages_mode != "off"
        )
        # thinking_progress is independent — if enabled, we need the progress
        # queue even when tool_progress is off (thinking relay uses same infra).
        _thinking_mode = _display_surface_mode(
            "thinking_progress",
            default=False,
            require_platform_override_for=set(),
        )
        _thinking_enabled = _thinking_mode != "off"
        needs_progress_queue = tool_progress_enabled or _thinking_enabled


        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if needs_progress_queue else None
        last_tool = [None]  # Mutable container for tracking in closure
        last_progress_msg = [None]  # Track last message for dedup
        repeat_count = [0]  # How many times the same message repeated
        # True when the previously enqueued progress line was a terminal
        # fenced code block — consecutive terminal calls then drop the
        # repeated "💻 terminal" header and render back-to-back blocks.
        last_was_terminal_block = [False]

        # ── Discord voice "verbal ack before tool calls" ────────────────
        # When the bot is in a voice channel with the continuous mixer
        # installed (discord.voice_fx.enabled), speak a short phrase ("let me
        # look into that") over the ambient idle bed on the FIRST tool call of
        # the turn.  Fires from tool_start_callback (independent of the
        # tool-progress text gate), at most once per turn.  No-op on every
        # other platform / when not in a voice channel.
        _voice_ack_fired = [False]
        _voice_ack_guild: List[Optional[int]] = [None]
        if source.platform == Platform.DISCORD:
            _va = self.adapters.get(Platform.DISCORD)
            # source.chat_id is the linked text channel; resolve the guild whose
            # voice connection is bound to it (mirrors DiscordAdapter.play_tts).
            _vtc = getattr(_va, "_voice_text_channels", None)
            if isinstance(_vtc, dict) and hasattr(_va, "voice_mixer_active"):
                for _gid, _tc in _vtc.items():
                    if str(_tc) == str(source.chat_id) and _va.voice_mixer_active(_gid):
                        _voice_ack_guild[0] = _gid
                        break
        _voice_ack_loop = asyncio.get_running_loop()

        def voice_ack_callback(call_id, tool_name, args):
            """tool_start_callback: speak a one-time ack in the voice channel."""
            if _voice_ack_fired[0] or _voice_ack_guild[0] is None:
                return
            if not _run_still_current():
                return
            _voice_ack_fired[0] = True
            _adapter = self.adapters.get(Platform.DISCORD)
            if _adapter is None or not hasattr(_adapter, "play_ack_in_voice"):
                return
            try:
                safe_schedule_threadsafe(
                    _adapter.play_ack_in_voice(_voice_ack_guild[0]),
                    _voice_ack_loop,
                    logger=logger,
                    log_message="voice ack scheduling error",
                )
            except Exception as _ack_err:
                logger.debug("voice ack schedule failed: %s", _ack_err)

        # Auto-cleanup of temporary progress bubbles (Telegram + any adapter
        # that implements ``delete_message``). When enabled via
        # ``display.platforms.<platform>.cleanup_progress: true``, message IDs
        # from the tool-progress / "⏳ Working — N min" / status-callback bubbles
        # are collected here and deleted after the final response lands.
        # Failed runs skip cleanup so the bubbles remain as breadcrumbs.
        _cleanup_progress = bool(
            resolve_display_setting(user_config, platform_key, "cleanup_progress")
        )
        _cleanup_adapter = self._adapter_for_source(source) if _cleanup_progress else None
        if _cleanup_adapter is not None and (
            type(_cleanup_adapter).delete_message is BasePlatformAdapter.delete_message
        ):
            # Adapter doesn't support deletion — silently disable.
            _cleanup_progress = False
            _cleanup_adapter = None
        _cleanup_msg_ids: List[str] = []
        # First-touch onboarding latch: fires at most once per run, even if
        # several tools exceed the threshold.
        long_tool_hint_fired = [False]
        _LONG_TOOL_THRESHOLD_S = 30.0

        def progress_callback(event_type: str, tool_name: str = None, preview: str = None, args: dict = None, **kwargs):
            """Callback invoked by agent on tool lifecycle events."""
            # "log" mode: append tool.started lines to the log queue and stay
            # silent in chat. Handled before the progress_queue guard because
            # log mode runs without a chat progress queue.
            if log_queue is not None:
                if event_type == "tool.started" and tool_name and tool_name != "_thinking":
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    preview_str = f' "{preview}"' if preview else ""
                    log_queue.put(f"{ts}  {tool_name}:{preview_str}".rstrip())
                if not progress_queue:
                    return
            if not progress_queue or not _run_still_current():
                return

            # First-touch onboarding: the first time a tool takes longer than
            # _LONG_TOOL_THRESHOLD_S during a run that's streaming every tool
            # (progress_mode == "all"), append a one-time hint suggesting
            # /verbose.  We only fire when (a) the user hasn't seen the hint
            # before and (b) /verbose is actually usable on this platform
            # (gateway gate must be open).  The CLI has its own trigger.
            if event_type == "tool.completed" and not long_tool_hint_fired[0]:
                try:
                    duration = kwargs.get("duration") or 0
                    if duration >= _LONG_TOOL_THRESHOLD_S and progress_mode == "all":
                        from agent.onboarding import (
                            TOOL_PROGRESS_FLAG,
                            is_seen,
                            mark_seen,
                            tool_progress_hint_gateway,
                        )
                        _cfg = _load_gateway_config()
                        gate_on = is_truthy_value(
                            cfg_get(_cfg, "display", "tool_progress_command"),
                            default=False,
                        )
                        if gate_on and not is_seen(_cfg, TOOL_PROGRESS_FLAG):
                            long_tool_hint_fired[0] = True
                            progress_queue.put(tool_progress_hint_gateway())
                            mark_seen(_hermes_home / "config.yaml", TOOL_PROGRESS_FLAG)
                except Exception as _hint_err:
                    logger.debug("tool-progress onboarding hint failed: %s", _hint_err)
                return

            # "_thinking" is assistant scratch text between tool calls.  It
            # is never ordinary tool progress: only relay it when the platform
            # explicitly opted into thinking_progress.  Handle both legacy
            # callback shapes: ("_thinking", text) and
            # ("reasoning.available", "_thinking", text, ...).
            if event_type == "_thinking" or tool_name == "_thinking":
                if not _thinking_enabled:
                    return
                thinking_text = preview if tool_name == "_thinking" else tool_name
                msg = f"💬 {thinking_text}" if thinking_text else None
                if msg:
                    progress_queue.put(msg)
                return

            # If tool_progress is off, only _thinking passes through (above).
            # Regular tool calls are suppressed.
            if not tool_progress_enabled:
                return

            # Only act on tool.started events (ignore tool.completed, reasoning.available, etc.)
            if event_type not in {"tool.started",}:
                return

            # Suppress tool-progress bubbles once the user has sent `stop`.
            # When the LLM response carries N parallel tool calls, the agent
            # fires N "tool.started" events back-to-back before checking for
            # interrupts — without this guard, a late `stop` still renders
            # all N as 🔍 bubbles, making the interrupt feel ignored.
            # (agent lives in run_sync's scope; agent_holder[0] is the shared
            # handle across nested scopes — see line ~9607.)
            try:
                _agent_for_interrupt = agent_holder[0] if agent_holder else None
                if _agent_for_interrupt is not None and getattr(
                    _agent_for_interrupt, "is_interrupted", False
                ):
                    return
            except Exception:
                pass

            # "new" mode: only report when tool changes
            if progress_mode == "new" and tool_name == last_tool[0]:
                return
            last_tool[0] = tool_name

            # Build progress message with primary argument preview
            from agent.display import get_tool_emoji
            emoji = get_tool_emoji(tool_name, default="⚙️")

            # Markdown-capable platforms render a terminal command as a fenced
            # code block instead of the compact `terminal: "cmd…"` preview.
            # Gated on the adapter's ``supports_code_blocks`` capability so
            # plain-text platforms keep the short line.  No language tag is
            # emitted — Slack mrkdwn renders the tag as a literal first code
            # line ("bash"), and a bare fence renders correctly everywhere
            # that supports blocks.
            #
            # Verbose mode shows the FULL command.  Non-verbose ("all"/"new")
            # modes still wrap in a fence but truncate to a single line capped
            # at ``tool_preview_length`` (default 40) so a long or multi-line
            # command doesn't render as a huge block — matching the budget the
            # non-terminal preview path already applies (#42634).
            _code_block_full = None
            _code_block_short = None
            try:
                _progress_adapter = self._adapter_for_source(source)
            except Exception:
                _progress_adapter = None
            if (
                getattr(_progress_adapter, "supports_code_blocks", False)
                and tool_name == "terminal"
                and isinstance(args, dict)
                and isinstance(args.get("command"), str)
                and args["command"].strip()
            ):
                from agent.display import get_tool_preview_max_len
                _cmd_full = args["command"].rstrip()
                # Consecutive terminal calls: drop the repeated
                # "💻 terminal" header so back-to-back commands render as
                # adjacent code blocks under a single header.
                _block_header = (
                    "" if last_was_terminal_block[0] else f"{emoji} {tool_name}\n"
                )
                _code_block_full = f"{_block_header}```\n{_cmd_full}\n```"
                # Single-line, capped preview for non-verbose modes.
                _pl = get_tool_preview_max_len()
                _cap = _pl if _pl > 0 else 40
                _lines = _cmd_full.splitlines()
                _cmd_short = _lines[0] if _lines else _cmd_full
                _multiline = len(_lines) > 1
                if len(_cmd_short) > _cap:
                    _cmd_short = _cmd_short[:_cap - 3] + "..."
                elif _multiline:
                    _cmd_short = _cmd_short + " ..."
                _code_block_short = f"{_block_header}```\n{_cmd_short}\n```"

            # Verbose mode: show detailed arguments, respects tool_preview_length
            if progress_mode == "verbose":
                if _code_block_full is not None:
                    last_was_terminal_block[0] = True
                    progress_queue.put(_code_block_full)
                    return
                last_was_terminal_block[0] = False
                if args:
                    from agent.display import get_tool_preview_max_len
                    _pl = get_tool_preview_max_len()
                    args_str = json.dumps(args, ensure_ascii=False, default=str)
                    # When tool_preview_length is 0 (default), don't truncate
                    # in verbose mode — the user explicitly asked for full
                    # detail.  Platform message-length limits handle the rest.
                    if _pl > 0 and len(args_str) > _pl:
                        args_str = args_str[:_pl - 3] + "..."
                    msg = f"{emoji} {tool_name}({list(args.keys())})\n{args_str}"
                elif preview:
                    msg = f"{emoji} {tool_name}: \"{preview}\""
                else:
                    msg = f"{emoji} {tool_name}..."
                progress_queue.put(msg)
                return
            
            # "all" / "new" modes: short preview, respects tool_preview_length
            # config (defaults to 40 chars when unset to keep gateway messages
            # compact — unlike CLI spinners, these persist as permanent messages).
            # Terminal commands on markdown platforms get a single-line capped
            # fenced block (built above) instead of the truncated preview.
            if _code_block_short is not None:
                msg = _code_block_short
                last_was_terminal_block[0] = True
            elif preview:
                from agent.display import (
                    get_tool_preview_max_len,
                    get_tool_verb,
                    tool_verb_connector,
                    verb_drops_preview,
                )
                _pl = get_tool_preview_max_len()
                _cap = _pl if _pl > 0 else 40
                if len(preview) > _cap:
                    preview = preview[:_cap - 3] + "..."
                # Friendly labels: render a human-phrased line for built-in
                # tools ("🔍 Searching the web for ...") by prefixing the verb
                # onto the preview the callback already computed (so the
                # command/url/query is preserved).  Custom/plugin/MCP tools
                # have no verb and fall back to the raw "tool_name: ..." form.
                _verb = get_tool_verb(tool_name)
                if _verb:
                    if verb_drops_preview(tool_name):
                        msg = f"{emoji} {_verb}"
                    else:
                        msg = f"{emoji} {_verb}{tool_verb_connector(tool_name)}{preview}"
                else:
                    msg = f"{emoji} {tool_name}: \"{preview}\""
                last_was_terminal_block[0] = False
            else:
                msg = f"{emoji} {tool_name}..."
                last_was_terminal_block[0] = False
            
            # Dedup: collapse consecutive identical progress messages.
            # Common with execute_code where models iterate with the same
            # code (same boilerplate imports → identical previews).
            if msg == last_progress_msg[0]:
                repeat_count[0] += 1
                # Update the last line in progress_lines with a counter
                # via a special "dedup" queue message.
                progress_queue.put(("__dedup__", msg, repeat_count[0]))
                return
            last_progress_msg[0] = msg
            repeat_count[0] = 0
            
            progress_queue.put(msg)
        
        # Background task to send progress messages
        # Accumulates tool lines into a single message that gets edited.
        #
        # Threading metadata is platform-specific:
        # - Slack DM threading needs event_message_id fallback (reply thread)
        # - Telegram forum topics use message_thread_id; Hermes-created private
        #   DM topic lanes require both thread metadata and a reply anchor
        # - Feishu only honors reply_in_thread when sending a reply, so topic
        #   progress uses the triggering event message as the reply target
        # - Other platforms should use explicit source.thread_id only
        _progress_thread_id = _resolve_progress_thread_id(
            source.platform, source.thread_id, event_message_id,
        )
        _progress_metadata = (
            self._thread_metadata_for_source(source, event_message_id)
            if _progress_thread_id == source.thread_id
            else {"thread_id": _progress_thread_id}
        ) if _progress_thread_id else None
        _progress_metadata = _non_conversational_metadata(_progress_metadata, platform=source.platform)
        _progress_reply_to = (
            event_message_id
            if source.platform in (Platform.FEISHU,) and source.thread_id and event_message_id
            else None
        )

        async def write_tool_log():
            """Drain log_queue and append tool-call lines to tool_calls.log.

            Only active when ``display.tool_progress`` is ``log``. Uses a
            RotatingFileHandler (5MB × 3 backups) so the audit log can't grow
            unbounded, and the shared RedactingFormatter so secrets never land
            on disk.
            """
            if log_queue is None:
                return
            from logging.handlers import RotatingFileHandler

            from agent.redact import RedactingFormatter

            log_dir = _hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "tool_calls.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(RedactingFormatter("%(message)s"))
            tool_logger = logging.getLogger(f"hermes.tool_calls.{id(log_queue)}")
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(file_handler)
            try:
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error("write_tool_log error: %s", e)
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                # Drain remaining entries before closing so late tool calls
                # from the final iteration aren't lost.
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        break
                    except Exception:
                        break
                tool_logger.removeHandler(file_handler)
                try:
                    file_handler.flush()
                    file_handler.close()
                except Exception:
                    pass

        async def send_progress_messages():
            if not progress_queue:
                return

            adapter = self._adapter_for_source(source)
            if not adapter:
                return

            # Skip tool progress for platforms that don't support message
            # editing (e.g. iMessage/BlueBubbles) — each progress update
            # would become a separate message bubble, which is noisy.
            if type(adapter).edit_message is BasePlatformAdapter.edit_message:
                while not progress_queue.empty():
                    try:
                        progress_queue.get_nowait()
                    except Exception:
                        break
                return

            progress_lines = []      # Accumulated tool lines for the CURRENT editable bubble
            progress_msg_id = None   # ID of the current progress message to edit
            can_edit = progress_grouping != "separate"  # "separate" = one message per tool (pre-v0.9 behavior)
            _last_edit_ts = 0.0      # Throttle edits to avoid Telegram flood control
            _PROGRESS_EDIT_INTERVAL = 1.5  # Minimum seconds between edits

            _progress_len_fn = (
                adapter.message_len_fn
                if isinstance(adapter, BasePlatformAdapter)
                else len
            )
            try:
                _raw_progress_limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000) or 4000)
            except Exception:
                _raw_progress_limit = 4000
            # Leave a little room for platform quirks / formatting.  For tiny
            # test adapters keep the limit usable instead of clamping to 500+.
            _PROGRESS_TEXT_LIMIT = max(
                1,
                _raw_progress_limit - (64 if _raw_progress_limit > 128 else 0),
            )

            # Detect whether the adapter's edit_message accepts metadata so
            # overflow edits preserve Telegram topic/thread routing (#27487).
            _edit_accepts_metadata = False
            if _progress_metadata:
                try:
                    _edit_params = inspect.signature(adapter.edit_message).parameters
                    _edit_accepts_metadata = (
                        "metadata" in _edit_params
                        or any(
                            param.kind is inspect.Parameter.VAR_KEYWORD
                            for param in _edit_params.values()
                        )
                    )
                except (TypeError, ValueError):
                    _edit_accepts_metadata = False

            async def _edit_progress_message(message_id: str, content: str):
                kwargs = {
                    "chat_id": source.chat_id,
                    "message_id": message_id,
                    "content": content,
                }
                if getattr(adapter, "REQUIRES_EDIT_FINALIZE", False):
                    kwargs["finalize"] = True
                if _edit_accepts_metadata:
                    kwargs["metadata"] = _progress_metadata
                return await adapter.edit_message(**kwargs)

            def _progress_text(lines: list) -> str:
                return "\n".join(str(line) for line in lines)

            def _split_progress_groups(lines: list) -> list[list]:
                """Partition progress lines into platform-sized editable bubbles."""
                groups: list[list] = []
                current: list = []
                for line in lines:
                    candidate = current + [line]
                    if current and _progress_len_fn(_progress_text(candidate)) > _PROGRESS_TEXT_LIMIT:
                        groups.append(current)
                        current = [line]
                    else:
                        current = candidate
                if current:
                    groups.append(current)
                return groups

            def _track_progress_result(result) -> None:
                if (
                    _cleanup_progress
                    and getattr(result, "success", False)
                    and getattr(result, "message_id", None)
                ):
                    _cleanup_msg_ids.append(str(result.message_id))

            async def _send_progress_text(text: str):
                result = await adapter.send(
                    chat_id=source.chat_id,
                    content=text,
                    reply_to=_progress_reply_to,
                    metadata=_progress_metadata,
                )
                _track_progress_result(result)
                return result

            async def _roll_progress_overflow_if_needed() -> bool:
                """Start fresh editable progress bubbles before a bubble exceeds limit.

                Returns True when it delivered/split the current buffer and the
                caller should skip the normal send/edit path for this tick.
                """
                nonlocal progress_msg_id, progress_lines, can_edit
                if not progress_lines or not can_edit:
                    return False
                groups = _split_progress_groups(progress_lines)
                if len(groups) <= 1:
                    return False

                first_text = _progress_text(groups[0])
                if progress_msg_id is not None:
                    result = await _edit_progress_message(progress_msg_id, first_text)
                    if not result.success:
                        can_edit = False
                        # Fall back to the existing non-edit behavior below.
                        return False
                else:
                    result = await _send_progress_text(first_text)
                    if result.success and result.message_id:
                        progress_msg_id = result.message_id

                for group in groups[1:]:
                    result = await _send_progress_text(_progress_text(group))
                    if result.success and result.message_id:
                        progress_msg_id = result.message_id

                # The newest continuation is now the only mutable bubble.  Keep
                # just its lines so subsequent edits update it instead of
                # replaying the full historical transcript into new messages.
                progress_lines = groups[-1]
                return True

            while True:
                try:
                    if not _run_still_current():
                        while not progress_queue.empty():
                            try:
                                progress_queue.get_nowait()
                            except Exception:
                                break
                        return

                    raw = progress_queue.get_nowait()

                    # Drain silently when interrupted: events queued in the
                    # window between tool parse and interrupt processing
                    # should not render as bubbles.  The "⚡ Interrupting
                    # current task" message is sent separately and is the
                    # last progress-flavored bubble the user should see.
                    try:
                        _agent_for_interrupt = agent_holder[0] if agent_holder else None
                        if _agent_for_interrupt is not None and getattr(
                            _agent_for_interrupt, "is_interrupted", False
                        ):
                            # Drop this event and continue draining.
                            await asyncio.sleep(0)
                            continue
                    except Exception:
                        pass

                    # Handle dedup messages: update last line with repeat counter
                    if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
                        _, base_msg, count = raw
                        if progress_lines:
                            progress_lines[-1] = f"{base_msg} (×{count + 1})"
                        msg = progress_lines[-1] if progress_lines else base_msg
                    elif isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__":
                        # Content bubble just landed on the platform — close off
                        # the current tool-progress bubble so the next tool
                        # starts a fresh bubble below the content. Without this,
                        # tool lines keep editing the ORIGINAL progress message
                        # above the new content, making the chat appear out of
                        # order. Mirrors GatewayStreamConsumer.on_segment_break
                        # on the content side. (Issue: tool + content
                        # linearization regression after PR #7885.)
                        progress_msg_id = None
                        progress_lines = []
                        last_progress_msg[0] = None
                        repeat_count[0] = 0
                        continue
                    else:
                        msg = raw
                        progress_lines.append(msg)

                    if await _roll_progress_overflow_if_needed():
                        _last_edit_ts = time.monotonic()
                        await asyncio.sleep(0.3)
                        if _run_still_current():
                            await adapter.send_typing(source.chat_id, metadata=_progress_metadata)
                        continue

                    # Throttle edits: batch rapid tool updates into fewer
                    # API calls to avoid hitting Telegram flood control.
                    # (grammY auto-retry pattern: proactively rate-limit
                    # instead of reacting to 429s.)
                    _now = time.monotonic()
                    _remaining = _PROGRESS_EDIT_INTERVAL - (_now - _last_edit_ts)
                    if _remaining > 0:
                        # Wait out the throttle interval, then loop back to
                        # drain any additional queued messages before sending
                        # a single batched edit.
                        await asyncio.sleep(_remaining)
                        continue

                    if not _run_still_current():
                        return

                    if can_edit and progress_msg_id is not None:
                        # Try to edit the existing progress message
                        full_text = "\n".join(progress_lines)
                        result = await _edit_progress_message(progress_msg_id, full_text)
                        if not result.success:
                            _err = (getattr(result, "error", "") or "").lower()
                            # Transient network errors (ConnectError, timeouts)
                            # must not permanently disable progress-message
                            # editing — the next cycle can catch up.  Only
                            # permanent failures (flood control, message not
                            # found, permissions) should set can_edit = False.
                            if getattr(result, "retryable", False):
                                logger.debug(
                                    "[%s] Transient edit failure — keeping can_edit=True",
                                    adapter.name,
                                )
                                continue
                            if "flood" in _err or "retry after" in _err:
                                # Flood control hit — backoff but keep editing.
                                # Only disable edits for non-recoverable errors.
                                logger.info(
                                    "[%s] Progress edit flood control, backing off",
                                    adapter.name,
                                )
                                _last_edit_ts = time.monotonic()
                            else:
                                can_edit = False
                            _flood_result = await adapter.send(
                                chat_id=source.chat_id,
                                content=msg,
                                reply_to=_progress_reply_to,
                                metadata=_progress_metadata,
                            )
                            if (
                                _cleanup_progress
                                and getattr(_flood_result, "success", False)
                                and getattr(_flood_result, "message_id", None)
                            ):
                                _cleanup_msg_ids.append(str(_flood_result.message_id))
                    else:
                        if can_edit:
                            # First tool: send all accumulated text as new message
                            full_text = "\n".join(progress_lines)
                            result = await adapter.send(
                                chat_id=source.chat_id,
                                content=full_text,
                                reply_to=_progress_reply_to,
                                metadata=_progress_metadata,
                            )
                        else:
                            # Editing unsupported: send just this line
                            result = await adapter.send(
                                chat_id=source.chat_id,
                                content=msg,
                                reply_to=_progress_reply_to,
                                metadata=_progress_metadata,
                            )
                        if result.success and result.message_id:
                            progress_msg_id = result.message_id
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(str(result.message_id))

                    _last_edit_ts = time.monotonic()

                    # Restore typing indicator
                    await asyncio.sleep(0.3)
                    if _run_still_current():
                        await adapter.send_typing(source.chat_id, metadata=_progress_metadata)

                except queue.Empty:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    # Drain remaining queued messages
                    while not progress_queue.empty():
                        try:
                            raw = progress_queue.get_nowait()
                            if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
                                _, base_msg, count = raw
                                if progress_lines:
                                    progress_lines[-1] = f"{base_msg} (×{count + 1})"
                                    await _roll_progress_overflow_if_needed()
                            elif isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__":
                                # Content-bubble marker during drain: close off
                                # the current progress bubble and start a fresh
                                # one for any tool lines that arrived after.
                                await _roll_progress_overflow_if_needed()
                                if can_edit and progress_lines and progress_msg_id:
                                    _pending_text = _progress_text(progress_lines)
                                    try:
                                        await _edit_progress_message(progress_msg_id, _pending_text)
                                    except Exception:
                                        pass
                                progress_msg_id = None
                                progress_lines = []
                                last_progress_msg[0] = None
                                repeat_count[0] = 0
                            else:
                                progress_lines.append(raw)
                                await _roll_progress_overflow_if_needed()
                        except Exception:
                            break
                    # Final edit with all remaining tools (only if editing works)
                    if can_edit and progress_lines and progress_msg_id:
                        await _roll_progress_overflow_if_needed()
                    if can_edit and progress_lines and progress_msg_id:
                        full_text = _progress_text(progress_lines)
                        try:
                            await _edit_progress_message(progress_msg_id, full_text)
                        except Exception:
                            pass
                    return
                except Exception as e:
                    logger.error("Progress message error: %s", e)
                    await asyncio.sleep(1)
        
        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        stream_consumer_holder = [None]  # Mutable container for stream consumer
        
        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_running_loop()
        _hooks_ref = self.hooks

        def _step_callback_sync(iteration: int, prev_tools: list) -> None:
            if not _run_still_current():
                return
            # prev_tools may be list[str] or list[dict] with "name"/"result"
            # keys.  Normalise to keep "tool_names" backward-compatible for
            # user-authored hooks that do ', '.join(tool_names)'.
            _names: list[str] = []
            for _t in (prev_tools or []):
                if isinstance(_t, dict):
                    _names.append(_t.get("name") or "")
                else:
                    _names.append(str(_t))
            safe_schedule_threadsafe(
                _hooks_ref.emit("agent:step", {
                    "platform": source.platform.value if source.platform else "",
                    "user_id": source.user_id,
                    "session_id": session_id,
                    "iteration": iteration,
                    "tool_names": _names,
                    "tools": prev_tools,
                }),
                _loop_for_step,
                logger=logger,
                log_message="agent:step hook scheduling error",
            )

        # Bridge sync event_callback → async hooks.emit for lifecycle events
        # (e.g. session:compress fires after context compression splits a session)
        def _event_callback_sync(event_type: str, context: dict) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    _hooks_ref.emit(event_type, context),
                    _loop_for_step,
                )
            except Exception as _e:
                logger.debug("event_callback hook error: %s", _e)

        # Bridge sync status_callback → async adapter.send for context pressure
        _status_adapter = self._adapter_for_source(source)
        _status_chat_id = source.chat_id
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            # Feishu topics only keep messages inside the topic when they are
            # sent via the reply API with reply_in_thread=true. Status/interim,
            # approval, and stream-consumer paths usually only receive metadata,
            # so carry the triggering message id as a Feishu-specific fallback.
            _status_thread_metadata: Optional[Dict[str, Any]] = {
                "thread_id": _progress_thread_id,
                "reply_to_message_id": event_message_id,
            }
        else:
            _status_thread_metadata = self._thread_metadata_for_source(source, event_message_id) if _progress_thread_id else None

        def _status_callback_sync(event_type: str, message: str) -> None:
            if not _status_adapter or not _run_still_current():
                return
            prepared_message = _prepare_gateway_status_message(
                source.platform,
                event_type,
                message,
            )
            if prepared_message is None:
                logger.debug(
                    "status_callback suppressed for %s/%s: %s",
                    source.platform.value if source.platform else "unknown",
                    event_type,
                    _redact_gateway_user_facing_secrets(str(message or ""))[:160],
                )
                return
            _fut = safe_schedule_threadsafe(
                _send_or_update_status_coro(_status_adapter, _status_chat_id, event_type, prepared_message, _status_thread_metadata),
                _loop_for_step,
                logger=logger,
                log_message=f"status_callback ({event_type}) scheduling error",
            )
            if _fut is None:
                return
            if _cleanup_progress:
                def _track_status_id(fut) -> None:
                    try:
                        res = fut.result()
                    except Exception:
                        return
                    mid = getattr(res, "message_id", None)
                    if getattr(res, "success", False) and mid:
                        _cleanup_msg_ids.append(str(mid))
                _fut.add_done_callback(_track_status_id)

        def run_sync():
            # The conditional re-assignment of `message` further below
            # (prepending model-switch notes) makes Python treat it as a
            # local variable in the entire function.  `nonlocal` lets us
            # read *and* reassign the outer `_run_agent` parameter without
            # triggering an UnboundLocalError on the earlier read at
            # `_resolve_turn_agent_config(message, …)`.
            nonlocal message

            # session_key is propagated via contextvars in _set_session_env()
            # (_SESSION_KEY) and via set_current_session_key() (_approval_session_key)
            # below — both concurrency-safe and inherited by tool worker threads.
            # We deliberately do NOT write os.environ["HERMES_SESSION_KEY"] here:
            # os.environ is process-global, so concurrent gateway sessions (e.g.
            # two Discord threads) would clobber each other's value, and a tool
            # thread whose contextvar is unset would fall back to os.environ and
            # read the wrong session key — misrouting command-approval prompts to
            # the wrong thread (#24100). The non-gateway surfaces don't depend on
            # this write: CLI and cron bind the session via contextvars
            # (set_current_session_key / session context), and only the TUI
            # slash-worker *subprocess* exports HERMES_SESSION_KEY (from its own
            # --session-key argv, a separate process) — so removing this in-process
            # gateway write does not affect any of them.

            # Map platform enum to the platform hint key the agent understands.
            # Platform.LOCAL ("local") maps to "cli"; others pass through as-is.
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            
            # Combine platform context, YAML channel_prompts hint for this chat,
            # channel_overrides system_prompt (or global ephemeral), and gateway
            # ephemeral prompt from _get_system_prompt_for_channel.
            combined_ephemeral = context_prompt or ""
            event_channel_prompt = (channel_prompt or "").strip()
            if event_channel_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + event_channel_prompt).strip()
            cfg_channel_prompt = self._get_system_prompt_for_channel(
                source.platform,
                source.chat_id or "",
                thread_id=getattr(source, "thread_id", None),
                parent_id=getattr(source, "parent_chat_id", None),
            )
            if cfg_channel_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + cfg_channel_prompt).strip()

            max_iterations = _current_max_iterations()

            try:
                model, runtime_kwargs = self._resolve_session_agent_runtime(
                    source=source,
                    session_key=session_key,
                    user_config=user_config,
                )
                logger.debug(
                    "run_agent resolved: model=%s provider=%s session=%s",
                    model, runtime_kwargs.get("provider"), session_key or "",
                )
            except Exception as exc:
                return {
                    "final_response": f"⚠️ Provider authentication failed: {exc}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }

            pr = self._provider_routing
            reasoning_config = self._resolve_session_reasoning_config(
                source=source,
                session_key=session_key,
            )
            self._reasoning_config = reasoning_config
            self._service_tier = self._load_service_tier()
            # Set up stream consumer for token streaming or interim commentary.
            _stream_consumer = None
            _stream_delta_cb = None
            _scfg = getattr(getattr(self, 'config', None), 'streaming', None)
            if _scfg is None:
                from gateway.config import StreamingConfig
                _scfg = StreamingConfig()

            # Per-platform streaming gate: display.platforms.<plat>.streaming
            # can disable streaming for specific platforms even when the global
            # streaming config is enabled.
            _plat_streaming = resolve_display_setting(
                user_config, platform_key, "streaming"
            )
            # None = no per-platform override → follow global config
            _streaming_enabled = (
                _scfg.enabled and _scfg.transport != "off"
                if _plat_streaming is None
                else bool(_plat_streaming)
            )
            _want_stream_deltas = _streaming_enabled
            _want_interim_messages = interim_assistant_messages_enabled
            _want_interim_consumer = _want_interim_messages
            if _want_stream_deltas or _want_interim_consumer:
                try:
                    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
                    _adapter = self._adapter_for_source(source)
                    if _adapter:
                        _pause_typing_before_finalize = None
                        if source.platform == Platform.TELEGRAM and hasattr(_adapter, "pause_typing_for_chat"):
                            def _pause_typing_before_finalize(
                                _adapter=_adapter,
                                _chat_id=source.chat_id,
                            ) -> None:
                                _adapter.pause_typing_for_chat(_chat_id)
                        # Platforms that don't support editing sent messages
                        # (e.g. QQ, WeChat) should skip streaming entirely —
                        # without edit support, the consumer sends a partial
                        # first message that can never be updated, resulting in
                        # duplicate messages (partial + final).
                        _adapter_supports_edit = getattr(_adapter, "SUPPORTS_MESSAGE_EDITING", True)
                        if not _adapter_supports_edit:
                            raise RuntimeError("skip streaming for non-editable platform")
                        _effective_cursor = _scfg.cursor
                        # Some Matrix clients render the streaming cursor
                        # as a visible tofu/white-box artifact.  Keep
                        # streaming text on Matrix, but suppress the cursor.
                        _buffer_only = False
                        if source.platform == Platform.MATRIX:
                            _effective_cursor = ""
                            _buffer_only = True
                        # Fresh-final applies to Telegram only — other
                        # platforms either edit in place cheaply or don't
                        # have the edit-timestamp-stays-stale problem.
                        # (Ported from openclaw/openclaw#72038.)
                        _fresh_final_secs = (
                            float(getattr(_scfg, "fresh_final_after_seconds", 0.0) or 0.0)
                            if source.platform == Platform.TELEGRAM
                            else 0.0
                        )
                        _consumer_cfg = StreamConsumerConfig(
                            edit_interval=_scfg.edit_interval,
                            buffer_threshold=_scfg.buffer_threshold,
                            cursor=_effective_cursor,
                            buffer_only=_buffer_only,
                            fresh_final_after_seconds=_fresh_final_secs,
                            transport=_scfg.transport or "edit",
                            chat_type=getattr(source, "chat_type", "") or "",
                        )
                        _stream_consumer = GatewayStreamConsumer(
                            adapter=_adapter,
                            chat_id=source.chat_id,
                            config=_consumer_cfg,
                            metadata=_status_thread_metadata,
                            on_new_message=(
                                (lambda: progress_queue.put(("__reset__",)))
                                if progress_queue is not None
                                else None
                            ),
                            on_before_finalize=_pause_typing_before_finalize,
                            initial_reply_to_id=event_message_id,
                            run_still_current=_run_still_current,
                        )
                        if _want_stream_deltas:
                            def _stream_delta_cb(text: str) -> None:
                                if _run_still_current():
                                    _stream_consumer.on_delta(text)
                        stream_consumer_holder[0] = _stream_consumer
                except Exception as _sc_err:
                    logger.debug("Could not set up stream consumer: %s", _sc_err)

            def _interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
                if not _run_still_current():
                    return
                display_text = text
                if _stream_consumer is not None:
                    if already_streamed:
                        _stream_consumer.on_segment_break()
                    else:
                        _stream_consumer.on_commentary(display_text)
                    return
                if already_streamed or not _status_adapter or not str(display_text or "").strip():
                    return
                safe_schedule_threadsafe(
                    _status_adapter.send(
                        _status_chat_id,
                        display_text,
                        metadata=_status_thread_metadata,
                    ),
                    _loop_for_step,
                    logger=logger,
                    log_message="interim_assistant_callback scheduling error",
                )

            turn_route = self._resolve_turn_agent_config(message, model, runtime_kwargs)

            # Check agent cache — reuse the AIAgent from the previous message
            # in this session to preserve the frozen system prompt and tool
            # schemas for prompt cache hits.
            _sig = self._agent_config_signature(
                turn_route["model"],
                turn_route["runtime"],
                enabled_toolsets,
                combined_ephemeral,
                cache_keys=self._extract_cache_busting_config(user_config),
                user_id=getattr(source, "user_id", None),
                user_id_alt=getattr(source, "user_id_alt", None),
            )
            agent = None
            reused_cached_agent = False
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)

            # Detect cross-process writes: when another process (e.g. hermes
            # dashboard) appends to the same session in the shared SessionDB,
            # the cached agent's in-memory transcript becomes stale.  Compare
            # the session's current message_count against the count recorded
            # when the agent was cached; on mismatch, invalidate the cache
            # so a fresh agent re-reads from disk. (#45966)
            _current_msg_count = None
            if self._session_db is not None and session_id:
                try:
                    # run_sync is off-loop (executor); sync DB is fine.
                    _sess_row = self._session_db._db.get_session(session_id)
                    if _sess_row:
                        _current_msg_count = _sess_row.get("message_count", 0)
                except Exception:
                    pass

            _xproc_evicted_agent = None
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached = _cache.get(session_key)
                    if cached and cached[1] == _sig:
                        # cached[2] is the message_count at cache time;
                        # stale when a second process appended rows.
                        # cached[3] (when present) is the session_id the
                        # snapshot was taken for — used to skip the guard
                        # when the active session_id differs (#54947).
                        _cached_mc = cached[2] if len(cached) > 2 else None
                        _cached_sid = cached[3] if len(cached) > 3 else None
                        # If the snapshot belongs to a different session_id
                        # (same session_key, different conversation), the
                        # message_count comparison is meaningless — the
                        # counts track DIFFERENT DB rows.  REUSE the cached
                        # agent rather than rebuild and bust the prompt cache
                        # on every session switch (#54947).
                        _session_id_mismatch = (
                            _cached_sid is not None
                            and session_id is not None
                            and _cached_sid != session_id
                        )
                        if (
                            not _session_id_mismatch
                            and _cached_mc is not None
                            and _current_msg_count is not None
                            and _current_msg_count != _cached_mc
                        ):
                            # Cross-process write detected — discard stale
                            # agent so it rebuilds from fresh DB transcript.
                            logger.info(
                                "Agent cache invalidated for session %s: "
                                "message_count changed (%s -> %s), "
                                "possible cross-process write",
                                session_key, _cached_mc, _current_msg_count,
                            )
                            evicted = self._agent_cache.pop(session_key, None)
                            _ev_agent = evicted[0] if isinstance(evicted, tuple) and evicted else None
                            if _ev_agent and _ev_agent is not _AGENT_PENDING_SENTINEL:
                                # Defer cleanup until AFTER the lock is
                                # released — _cleanup_agent_resources /
                                # release_clients can block on memory-provider
                                # shutdown and socket teardown, and running it
                                # here would stall the gateway event loop while
                                # _sweep_idle_cached_agents (session-expiry
                                # watcher) waits on the same lock, blocking
                                # Discord heartbeats (#52197).  The same session
                                # rebuilds a fresh agent immediately below, so
                                # use the SOFT release that preserves the
                                # session's terminal sandbox / browser / bg
                                # processes for the rebuilt agent to inherit —
                                # mirrors _evict_cached_agent / idle-sweep.
                                _xproc_evicted_agent = _ev_agent
                        else:
                            agent = cached[0]
                            # Refresh LRU order so the cap enforcement evicts
                            # truly-oldest entries, not the one we just used.
                            if hasattr(_cache, "move_to_end"):
                                try:
                                    _cache.move_to_end(session_key)
                                except KeyError:
                                    pass
                            self._init_cached_agent_for_turn(agent, _interrupt_depth)
                            # Refresh agent max_iterations from current config
                            # (cached agent may have been created with old config)
                            agent.max_iterations = max_iterations
                            logger.debug("Reusing cached agent for session %s", session_key)
                            reused_cached_agent = True

            # Lock released — now schedule cleanup of any cross-process-evicted
            # agent on a daemon thread so memory-provider shutdown / socket
            # teardown never blocks the gateway event loop or the cache lock
            # the session-expiry watcher needs (#52197).
            if _xproc_evicted_agent is not None:
                try:
                    threading.Thread(
                        target=self._release_evicted_agent_soft,
                        args=(_xproc_evicted_agent,),
                        daemon=True,
                        name=f"agent-xproc-evict-{str(session_key)[:24]}",
                    ).start()
                except Exception:
                    # Interpreter shutdown or thread-spawn failure — release
                    # inline as a best-effort fallback.
                    try:
                        self._release_evicted_agent_soft(_xproc_evicted_agent)
                    except Exception:
                        pass

            if agent is None:
                # Config changed or first message — create fresh agent
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    ephemeral_system_prompt=combined_ephemeral or None,
                    prefill_messages=self._prefill_messages or None,
                    reasoning_config=reasoning_config,
                    service_tier=self._service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=session_id,
                    platform=platform_key,
                    user_id=source.user_id,
                    user_id_alt=source.user_id_alt,
                    user_name=source.user_name,
                    chat_id=source.chat_id,
                    chat_name=source.chat_name,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    gateway_session_key=session_key,
                    session_db=getattr(self._session_db, "_db", self._session_db),
                    fallback_model=self._fallback_model,
                )
                if _cache_lock and _cache is not None:
                    with _cache_lock:
                        # Record the session_id the snapshot was taken for
                        # alongside the message_count, so the cross-process
                        # guard can skip the (meaningless) count comparison
                        # when the active session_id later switches under
                        # the same session_key (#54947).
                        _cache[session_key] = (
                            agent, _sig, _current_msg_count, session_id,
                        )
                        self._enforce_agent_cache_cap()
                logger.debug("Created new agent for session %s (sig=%s)", session_key, _sig)

            # Per-message state — callbacks and reasoning config change every
            # turn and must not be baked into the cached agent constructor.
            # Gate on needs_progress_queue (tool_progress OR thinking_progress)
            # rather than tool_progress alone: the progress_callback also relays
            # _thinking assistant scratch text, which is gated on
            # thinking_progress and is intentionally independent of tool
            # progress. With the old `tool_progress_enabled`-only gate, a user
            # who set thinking_progress:true but kept tool_progress:off got a
            # None callback — so _thinking scratch bubbles never relayed even
            # though the progress queue was created for them.
            agent.tool_progress_callback = (
                progress_callback if (needs_progress_queue or log_mode_enabled) else None
            )
            # Discord voice verbal-ack hook (fires once per turn on first tool
            # call; armed only when in a voice channel with the mixer running).
            agent.tool_start_callback = (
                voice_ack_callback if _voice_ack_guild[0] is not None else None
            )
            agent.step_callback = _step_callback_sync if _hooks_ref.loaded_hooks else None
            agent.stream_delta_callback = _stream_delta_cb
            agent.interim_assistant_callback = _interim_assistant_cb if _want_interim_messages else None
            agent.status_callback = _status_callback_sync
            # Credits / out-of-band notices (usage bands, depletion, restored).
            # Messaging has no persistent status bar, so each notice is a
            # standalone push: render to a single plaintext line and deliver via
            # the shared _deliver_platform_notice rail (honors private/public +
            # thread metadata). Fires from the agent's sync worker thread, so we
            # hop onto the gateway loop with safe_schedule_threadsafe - same
            # pattern as _status_callback_sync. The fired-once latch lives on the
            # cached agent and persists across turns, so a band crosses -> one
            # push (no per-turn re-nag). Recovery ("✓ Credit access restored")
            # rides the same show path (it's emitted as a success notice, not a
            # clear). The clear callback is a no-op: a sent platform message
            # can't be cleanly retracted, and the band already fired once.
            def _notice_callback_sync(notice) -> None:
                if not _status_adapter or not _run_still_current():
                    return
                try:
                    line = render_notice_line(notice)
                except Exception:
                    logger.debug("render_notice_line failed", exc_info=True)
                    return
                if not line:
                    return
                safe_schedule_threadsafe(
                    self._deliver_platform_notice(source, line),
                    _loop_for_step,
                    logger=logger,
                    log_message="notice_callback delivery scheduling error",
                )

            agent.notice_callback = _notice_callback_sync
            agent.notice_clear_callback = None
            agent.event_callback = _event_callback_sync
            agent.reasoning_config = reasoning_config
            agent.service_tier = self._service_tier
            agent.request_overrides = turn_route.get("request_overrides") or {}

            _bg_review_release = threading.Event()
            _bg_review_pending: list[str] = []
            _bg_review_pending_lock = threading.Lock()

            def _deliver_bg_review_message(message: str) -> None:
                if not _status_adapter or not _run_still_current():
                    return
                safe_schedule_threadsafe(
                    _status_adapter.send(
                        _status_chat_id,
                        message,
                        metadata=_non_conversational_metadata(_status_thread_metadata, platform=source.platform),
                    ),
                    _loop_for_step,
                    logger=logger,
                    log_message="background_review_callback scheduling error",
                )

            def _release_bg_review_messages() -> None:
                _bg_review_release.set()
                with _bg_review_pending_lock:
                    pending = list(_bg_review_pending)
                    _bg_review_pending.clear()
                for queued in pending:
                    _deliver_bg_review_message(queued)

            # Background review delivery — send "💾 Memory updated" etc. to user
            def _bg_review_send(message: str) -> None:
                if not _status_adapter or not _run_still_current():
                    return
                if not _bg_review_release.is_set():
                    with _bg_review_pending_lock:
                        if not _bg_review_release.is_set():
                            _bg_review_pending.append(message)
                            return
                _deliver_bg_review_message(message)

            agent.background_review_callback = _bg_review_send
            # Register the release hook on the adapter so base.py's finally
            # block can fire it after delivering the main response.
            if _status_adapter and session_key:
                if getattr(type(_status_adapter), "register_post_delivery_callback", None) is not None:
                    _status_adapter.register_post_delivery_callback(
                        session_key,
                        _release_bg_review_messages,
                        generation=run_generation,
                    )
                else:
                    _pdc = getattr(_status_adapter, "_post_delivery_callbacks", None)
                    if _pdc is not None:
                        _pdc[session_key] = _release_bg_review_messages
            # Memory update notifications in chat.  Config: display.memory_notifications
            #   off     — no chat notification (still logged to stdout)
            #   on      — generic "💾 Memory updated" (default)
            #   verbose — content preview: "💾 Memory ➕ Hermes Repo..."
            _mem_notif = user_config.get("display", {}).get("memory_notifications")
            if isinstance(_mem_notif, bool):
                _mem_notif = "on" if _mem_notif else "off"
            agent.memory_notifications = str(_mem_notif).lower() if _mem_notif else "on"

            # ------------------------------------------------------------------
            # Clarify callback: present a clarify prompt and block on a response.
            #
            # Runs on the agent's worker thread (see clarify_tool's synchronous
            # callback contract).  Bridges sync→async by scheduling the
            # adapter's send_clarify on the gateway event loop, then blocks on
            # the clarify primitive's threading.Event with a configurable
            # timeout.  Returns the user's response string, or a sentinel
            # explaining that no response arrived (so the agent can adapt
            # rather than hang forever).
            # ------------------------------------------------------------------
            def _clarify_callback_sync(question: str, choices) -> str:
                from tools import clarify_gateway as _clarify_mod
                import uuid as _uuid

                if not _status_adapter:
                    return ""

                clarify_id = _uuid.uuid4().hex[:10]
                _clarify_mod.register(
                    clarify_id=clarify_id,
                    session_key=session_key or "",
                    question=question,
                    choices=list(choices) if choices else None,
                )

                # Pause typing — like approval, we don't want a "thinking..."
                # status to obscure the prompt or block the user from typing
                # an "Other" response on platforms that disable input while
                # typing is active (Slack Assistant API).
                try:
                    _status_adapter.pause_typing_for_chat(_status_chat_id)
                except Exception:
                    pass

                send_ok = False
                fut = safe_schedule_threadsafe(
                    _status_adapter.send_clarify(
                        chat_id=_status_chat_id,
                        question=question,
                        choices=list(choices) if choices else None,
                        clarify_id=clarify_id,
                        session_key=session_key or "",
                        metadata=_status_thread_metadata,
                    ),
                    _loop_for_step,
                    logger=logger,
                    log_message="Clarify send failed to schedule",
                )
                if fut is None:
                    send_ok = False
                else:
                    try:
                        result = fut.result(timeout=15)
                        send_ok = bool(getattr(result, "success", False))
                    except Exception as exc:
                        logger.warning("Clarify send failed: %s", exc)
                        send_ok = False

                if not send_ok:
                    # Couldn't deliver the prompt — clean up and return
                    # sentinel so the agent can fall back to a sensible
                    # default rather than hanging.
                    _clarify_mod.clear_session(session_key or "")
                    return "[clarify prompt could not be delivered]"

                timeout = _clarify_mod.get_clarify_timeout()
                response = _clarify_mod.wait_for_response(clarify_id, timeout=float(timeout))
                if response is None or response == "":
                    # Timeout or session-boundary cancellation
                    return f"[user did not respond within {int(timeout / 60)}m]"
                return response

            agent.clarify_callback = _clarify_callback_sync

            # Show assistant thinking between tool calls — independent of
            # tool_progress mode. Per-platform opt-in prevents global
            # scratch-text display from leaking into busy public threads.
            agent.thinking_progress = _thinking_enabled
            # Store agent reference for interrupt support
            agent_holder[0] = agent
            # Capture the full tool definitions for transcript logging
            tools_holder[0] = agent.tools if hasattr(agent, 'tools') else None
            
            # Convert history to agent format.
            # Two cases:
            #   1. Normal path (from transcript): simple {role, content, timestamp} dicts
            #      - Strip timestamps, keep role+content
            #   2. Interrupt path (from agent result["messages"]): full agent messages
            #      that may include tool_calls, tool_call_id, reasoning, etc.
            #      - These must be passed through intact so the API sees valid
            #        assistant→tool sequences (dropping tool_calls causes 500 errors)
            #
            # Telegram observed group context is handled structurally here:
            # observed=True transcript rows are withheld from replayable
            # history and attached to the current addressed message as
            # API-only context, so persisted history stores only the real
            # addressed user turn.
            agent_history, observed_group_context = _build_gateway_agent_history(
                history,
                channel_prompt=channel_prompt,
                inject_timestamps=_message_timestamps_enabled(_load_gateway_config()),
            )

            # FTS write-corruption guard (#50502): when message persistence
            # fails silently through corrupt FTS triggers, the reloaded
            # transcript above is stale/empty even though the SAME cached agent
            # still holds the full live conversation in `_session_messages`.
            # Replacing the live transcript with that shorter copy causes
            # immediate same-session amnesia. Only applies when we reused a
            # cached agent bound to this exact session_id.
            if reused_cached_agent and getattr(agent, "session_id", None) == session_id:
                _selected = _select_cached_agent_history(
                    agent_history, getattr(agent, "_session_messages", None)
                )
                if _selected is not agent_history:
                    logger.warning(
                        "Persisted transcript lagged live cached history for "
                        "session %s (disk=%d, memory=%d); preserving live "
                        "conversation context (possible FTS write corruption)",
                        session_key, len(agent_history), len(_selected),
                    )
                    # The live in-memory history bypassed the replay-cleanup
                    # pass inside _build_gateway_agent_history — re-apply the
                    # stale-confirmation expiry (#59607) so a dangerous
                    # confirmation can't slip through this path either.
                    # Idempotent; messages without timestamps are untouched.
                    agent_history = _strip_stale_dangerous_confirmations(
                        _selected, now=time.time()
                    )
            
            # Collect MEDIA paths already in history so we can exclude them
            # from the current turn's extraction. This is compression-safe:
            # even if the message list shrinks, we know which paths are old.
            _history_media_paths: set = _collect_history_media_paths(agent_history)
            
            # Register per-session gateway approval callback so dangerous
            # command approval blocks the agent thread (mirrors CLI input()).
            # The callback bridges sync→async to send the approval request
            # to the user immediately.
            from tools.approval import (
                register_gateway_notify,
                reset_current_session_key,
                set_current_session_key,
                unregister_gateway_notify,
            )

            def _approval_notify_sync(approval_data: dict) -> None:
                """Send the approval request to the user from the agent thread.

                If the adapter supports interactive button-based approvals
                (e.g. Discord's ``send_exec_approval``), use that for a richer
                UX.  Otherwise fall back to a plain text message with
                ``/approve`` instructions.
                """
                # Pause the typing indicator while the agent waits for
                # user approval.  Critical for Slack's Assistant API where
                # assistant_threads_setStatus disables the compose box — the
                # user literally cannot type /approve while "is thinking..."
                # is active.  The approval message send auto-clears the Slack
                # status; pausing prevents _keep_typing from re-setting it.
                # Typing resumes in _handle_approve_command/_handle_deny_command.
                _status_adapter.pause_typing_for_chat(_status_chat_id)

                cmd = approval_data.get("command", "")
                desc = approval_data.get("description", "dangerous command")

                # Redact credentials from the command before displaying it in
                # the approval prompt — Tirith's findings are already redacted,
                # but the raw command string still leaks secrets to the chat
                # platform (#48456). Applied here so BOTH the button-based
                # (send_exec_approval) and plain-text fallback paths below use
                # the redacted value.
                cmd = _redact_approval_command(cmd)

                # Prefer button-based approval when the adapter supports it.
                # Check the *class* for the method, not the instance — avoids
                # false positives from MagicMock auto-attribute creation in tests.
                if getattr(type(_status_adapter), "send_exec_approval", None) is not None:
                    try:
                        _approval_fut = safe_schedule_threadsafe(
                            _status_adapter.send_exec_approval(
                                chat_id=_status_chat_id,
                                command=cmd,
                                session_key=_approval_session_key,
                                description=desc,
                                metadata=_status_thread_metadata,
                            ),
                            _loop_for_step,
                            logger=logger,
                            log_message="send_exec_approval scheduling error",
                        )
                        if _approval_fut is None:
                            raise RuntimeError("send_exec_approval: loop unavailable")
                        _approval_result = _approval_fut.result(timeout=15)
                        if _approval_result.success:
                            return
                        logger.warning(
                            "Button-based approval failed (send returned error), falling back to text: %s",
                            _approval_result.error,
                        )
                    except Exception as _e:
                        logger.warning(
                            "Button-based approval failed, falling back to text: %s", _e
                        )

                # Fallback: plain text approval prompt.  Use the adapter's
                # typed prefix so Slack/Matrix users are told the form they
                # can actually type (`!approve`) — typed "/" is blocked in
                # Slack threads and reserved by Matrix clients.
                _p = getattr(_status_adapter, "typed_command_prefix", "/")
                cmd_preview = cmd[:200] + "..." if len(cmd) > 200 else cmd
                msg = (
                    f"⚠️ **Dangerous command requires approval:**\n"
                    f"```\n{cmd_preview}\n```\n"
                    f"Reason: {desc}\n\n"
                    f"Reply `{_p}approve` to execute, `{_p}approve session` to approve this pattern "
                    f"for the session, `{_p}approve always` to approve permanently, or `{_p}deny` to cancel."
                )
                try:
                    _approval_send_fut = safe_schedule_threadsafe(
                        _status_adapter.send(
                            _status_chat_id,
                            msg,
                            metadata=_status_thread_metadata,
                        ),
                        _loop_for_step,
                        logger=logger,
                        log_message="Approval text-send scheduling error",
                    )
                    if _approval_send_fut is not None:
                        _approval_send_fut.result(timeout=15)
                except Exception as _e:
                    logger.error("Failed to send approval request: %s", _e)

            # Keep real user text separate from API-only recovery guidance.  If
            # an auto-continue note is prepended below, persist the original
            # message so stale guidance never replays as user-authored text.
            _persist_user_message_override: Optional[Any] = persist_user_message
            _persist_user_timestamp_override: Optional[float] = persist_user_timestamp

            # Prepend pending model switch note so the model knows about the switch
            _pending_notes = getattr(self, '_pending_model_notes', {})
            _msn = _pending_notes.pop(session_key, None) if session_key else None
            if _msn:
                message = _msn + "\n\n" + message

            # Auto-continue: if the loaded history ends with a tool result,
            # the previous agent turn was interrupted mid-work (gateway
            # restart, crash, SIGTERM).  Prepend a system note so the model
            # finishes processing the pending tool results before addressing
            # the user's new message.  (#4493)
            #
            # Session-level resume_pending (set on drain-timeout shutdown)
            # escalates the wording — the transcript's last role may be
            # anything (tool, assistant with unfinished work, etc.), so we
            # give a stronger, reason-aware instruction that subsumes the
            # tool-tail case.
            #
            # Freshness gate (#16802): both branches are gated on the age
            # of the last persisted transcript row.  That is the correct
            # "when did we last do anything here" signal for both the
            # resume_pending path (restart watchdog) and the tool-tail
            # path (in-flight tool loop killed).  We read ``history[-1]``
            # here because ``agent_history`` has already stripped the
            # ``timestamp`` field off tool/tool_call rows for API purity
            # (see the `k != "timestamp"` filter above).  Rows without a
            # timestamp (legacy transcripts) are treated as fresh so the
            # historical auto-continue behaviour is preserved.
            _freshness_window = _auto_continue_freshness_window()
            _interruption_is_fresh = _is_fresh_gateway_interruption(
                _last_transcript_timestamp(history),
                window_secs=_freshness_window,
            )

            _resume_entry = None
            if session_key:
                try:
                    _resume_entry = self.session_store._entries.get(session_key)
                except Exception:
                    _resume_entry = None

            # resume_pending freshness uses a SECOND signal in addition to the
            # transcript clock above.  The restart watchdog stamps the session
            # with ``last_resume_marked_at`` at interrupt time — that is the
            # correct "when were we interrupted" signal.  The transcript clock
            # (_interruption_is_fresh) can be far older: an active thread you
            # return to may have its last persisted row hours back, even though
            # the interruption itself just happened.  Gating resume_pending on
            # the transcript clock alone makes the recovery note silently drop,
            # and because the startup auto-resume turn carries empty text
            # (_schedule_resume_pending_sessions), the model then receives a
            # blank user message and replies with confused "the message came
            # through blank" noise.  Treat the marker as fresh when
            # EITHER signal is fresh so the two freshness checks agree.
            _resume_mark_is_fresh = False
            if _resume_entry is not None and getattr(_resume_entry, "resume_pending", False):
                _resume_mark_is_fresh = _is_fresh_gateway_interruption(
                    getattr(_resume_entry, "last_resume_marked_at", None),
                    window_secs=_freshness_window,
                )
            _is_resume_pending = bool(
                _resume_entry is not None
                and getattr(_resume_entry, "resume_pending", False)
                and (_interruption_is_fresh or _resume_mark_is_fresh)
            )
            _has_fresh_tool_tail = bool(
                agent_history
                and agent_history[-1].get("role") == "tool"
                and _interruption_is_fresh
            )

            if _is_resume_pending:
                _reason = getattr(_resume_entry, "resume_reason", None) or "restart_timeout"
                _reason_phrase = (
                    "a gateway restart"
                    if _reason == "restart_timeout"
                    else "a gateway shutdown"
                    if _reason == "shutdown_timeout"
                    else "a gateway interruption"
                )
                _persist_user_message_override = message
                # The empty-message case is the auto-resume startup turn
                # synthesized by _schedule_resume_pending_sessions — there is
                # no NEW user message to address, so tell the model to report
                # recovery instead of the (nonexistent) "new message".
                if message:
                    _resume_guidance = (
                        "Address the user's NEW message below FIRST and focus "
                        "on what the user is asking now."
                    )
                else:
                    _resume_guidance = (
                        "Report to the user that the session was restored "
                        "successfully and ask what they would like to do next."
                    )
                message = (
                    f"[System note: The previous turn was interrupted by "
                    f"{_reason_phrase}; the gateway is now back online. "
                    f"Any restart/shutdown command in the history has already "
                    f"run — do NOT re-execute or verify it. {_resume_guidance} "
                    f"Do NOT re-execute old tool calls — skip any unfinished "
                    f"work from the conversation history.]"
                    + (f"\n\n{message}" if message else "")
                )
            elif _has_fresh_tool_tail:
                _persist_user_message_override = message
                message = (
                    "[System note: A new message has arrived. The conversation "
                    "history contains pending tool outputs from an interrupted turn. "
                    "IGNORE those pending results. Address the user's NEW message "
                    "below FIRST. Do NOT re-execute old tool calls from the history.]\n\n"
                    + message
                )

            # Consume one-shot /reload-skills note (if the user ran
            # /reload-skills since their last turn in this session). Same
            # queue pattern as CLI: prepend to the NEXT user message, then
            # clear. Nothing was written to the transcript out-of-band, so
            # message alternation stays intact.
            _pending_notes = getattr(self, "_pending_skills_reload_notes", None)
            if _pending_notes and session_key and session_key in _pending_notes:
                _srn = _pending_notes.pop(session_key, None)
                if _srn:
                    message = _srn + "\n\n" + message

            # Safety net: a startup auto-resume event carries empty
            # text and relies on the resume_pending branch above to supply the
            # recovery note.  If that branch did not fire for any reason (e.g.
            # both freshness signals disagreed, or the marker was cleared
            # between scheduling and dispatch) we must NOT hand the model a
            # blank user turn — it responds with confused "the message came
            # through blank" noise.  Restricted to resume_pending sessions so
            # legitimately empty user turns (e.g. an image with no caption,
            # wrapped as native content below) are untouched.
            if (
                isinstance(message, str)
                and not message.strip()
                and _resume_entry is not None
                and getattr(_resume_entry, "resume_pending", False)
            ):
                _sn_reason = (
                    getattr(_resume_entry, "resume_reason", None) or "restart_timeout"
                )
                _sn_reason_phrase = (
                    "a gateway restart"
                    if _sn_reason == "restart_timeout"
                    else "a gateway shutdown"
                    if _sn_reason == "shutdown_timeout"
                    else "a gateway interruption"
                )
                message = (
                    f"[System note: The previous turn was interrupted by "
                    f"{_sn_reason_phrase}; the gateway is now back online. "
                    f"Any restart/shutdown command in the history has already "
                    f"run — do NOT re-execute or verify it. Report to the user "
                    f"that the session was restored successfully and ask what "
                    f"they would like to do next. Do NOT re-execute old tool "
                    f"calls — skip any unfinished work from the conversation "
                    f"history.]"
                )

            _approval_session_key = session_key or ""
            _approval_session_token = set_current_session_key(_approval_session_key)
            register_gateway_notify(_approval_session_key, _approval_notify_sync)
            try:
                # If _prepare_inbound_message_text buffered image paths for native
                # attachment, wrap the user turn as an OpenAI-style multimodal
                # content list. Consume-and-clear so subsequent turns on the same
                # runner instance don't re-attach stale images.
                _native_imgs = self._consume_pending_native_image_paths(session_key)
                if _native_imgs:
                    try:
                        from agent.image_routing import build_native_content_parts
                        _parts, _skipped = build_native_content_parts(
                            message,
                            _native_imgs,
                        )
                        if _skipped:
                            logger.warning(
                                "Native image attachment: skipped %d unreadable path(s): %s",
                                len(_skipped), _skipped,
                            )
                        if any(p.get("type") == "image_url" for p in _parts):
                            _run_message: Any = _parts
                        else:
                            # All images failed to read — fall back to plain text.
                            _run_message = message
                    except Exception as _img_exc:
                        logger.warning(
                            "Native image attachment failed, falling back to text: %s",
                            _img_exc,
                        )
                        _run_message = message
                else:
                    _run_message = message

                _api_run_message = _wrap_current_message_with_observed_context(
                    _run_message,
                    observed_group_context,
                )
                _conversation_kwargs = {
                    "conversation_history": agent_history,
                    "task_id": session_id,
                }
                if _persist_user_message_override is not None:
                    _conversation_kwargs["persist_user_message"] = _persist_user_message_override
                elif observed_group_context:
                    _conversation_kwargs["persist_user_message"] = message
                if moa_config is not None:
                    _conversation_kwargs["moa_config"] = moa_config
                if _persist_user_timestamp_override is not None:
                    _conversation_kwargs["persist_user_timestamp"] = _persist_user_timestamp_override
                result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
            finally:
                unregister_gateway_notify(_approval_session_key)
                # Cancel any pending clarify entries so blocked agent
                # threads don't hang past the end of the run (interrupt,
                # completion, gateway shutdown).  Idempotent.
                try:
                    from tools.clarify_gateway import clear_session as _clear_clarify_session
                    _clear_clarify_session(_approval_session_key)
                except Exception:
                    pass
                reset_current_session_key(_approval_session_token)
            result_holder[0] = result

            # Signal the stream consumer that the agent is done
            if _stream_consumer is not None:
                _stream_consumer.finish()
            
            # Return final response, or a message if something went wrong
            final_response = result.get("final_response")

            # Extract actual token counts from the agent instance used for this run
            _last_prompt_toks = 0
            _input_toks = 0
            _output_toks = 0
            _context_length = 0
            _agent = agent_holder[0]
            if _agent and hasattr(_agent, "context_compressor"):
                _last_prompt_toks = getattr(_agent.context_compressor, "last_prompt_tokens", 0)
                _input_toks = getattr(_agent, "session_prompt_tokens", 0)
                _output_toks = getattr(_agent, "session_completion_tokens", 0)
                _context_length = getattr(_agent.context_compressor, "context_length", 0) or 0
            _resolved_model = getattr(_agent, "model", None) if _agent else None

            # Sync session_id immediately after run_conversation(). Compression
            # can rotate before a follow-up model call fails; the failure return
            # below must still point the gateway at the compressed child.
            agent = agent_holder[0]
            _session_was_split = False
            # In-place compaction (compression.in_place / #38763) compacts the
            # transcript WITHOUT rotating the id, so the id-change diff below
            # can't detect it. compress_context() sets this rotation-independent
            # flag on the agent; the gateway uses it to re-baseline transcript
            # handling (history_offset=0 + rewrite the JSONL transcript) the
            # same way a split would, even though the session_id is unchanged.
            _compacted_in_place = bool(getattr(agent, "_last_compaction_in_place", False)) if agent else False
            agent_session_id = getattr(agent, 'session_id', session_id) if agent else session_id
            if agent and session_key and agent_session_id != session_id:
                _session_was_split = True
                logger.info(
                    "Session split detected: %s → %s (compression)",
                    session_id, agent_session_id,
                )
                entry = self.session_store._entries.get(session_key)
                _session_split_entry_persisted = False
                if entry:
                    entry_session_id = getattr(entry, "session_id", None)
                    if not _run_still_current():
                        logger.info(
                            "Skipping session split sync for stale run %s — "
                            "generation %s is no longer current",
                            session_key or "?",
                            run_generation,
                        )
                    elif entry_session_id == agent_session_id:
                        _session_split_entry_persisted = True
                    elif entry_session_id != session_id:
                        logger.info(
                            "Skipping session split sync for %s because the "
                            "session binding moved from %s to %s before "
                            "compression finished",
                            session_key or "?",
                            session_id,
                            entry_session_id,
                        )
                    else:
                        entry.session_id = agent_session_id
                        self.session_store._save()
                        self.session_store._record_gateway_session_peer(
                            agent_session_id,
                            session_key,
                            source,
                        )
                        _session_split_entry_persisted = True

                # If this is a Telegram DM and source.thread_id was lost during
                # the session split (synthetic / recovered event), restore it
                # from the binding so _thread_metadata_for_source produces the
                # correct message_thread_id instead of routing to the General
                # thread.  Failure here is non-fatal — we log and continue;
                # worst case the message lands in General, which is the
                # pre-fix behaviour. Only do this after this run successfully
                # published its session split; a stale /stop→/new predecessor
                # must not mutate routing/binding state for the fresh session.
                if _session_split_entry_persisted and (
                    getattr(source, "platform", None) == Platform.TELEGRAM
                    and getattr(source, "chat_type", None) == "dm"
                    and getattr(source, "thread_id", None) is None
                    and self._session_db is not None
                ):
                    try:
                        # run_sync is off-loop (executor); sync DB is fine.
                        _binding = self._session_db._db.get_telegram_topic_binding_by_session(
                            session_id=agent_session_id,
                        )
                        if _binding and _binding.get("thread_id"):
                            source.thread_id = str(_binding["thread_id"])
                            logger.debug(
                                "Restored source.thread_id=%s from binding after session split %s → %s",
                                source.thread_id,
                                session_id,
                                agent_session_id,
                            )
                    except Exception:
                        logger.debug(
                            "Failed to restore thread_id from binding after session split",
                            exc_info=True,
                        )
                if _session_split_entry_persisted:
                    self._sync_telegram_topic_binding(
                        source, entry, reason="agent-run-compression",
                    )

            effective_session_id = agent_session_id
            self._sync_session_model_from_agent(effective_session_id, agent)
            # history_offset=0 whenever the agent's message list no longer has
            # the original history prefix — i.e. on rotation (split) OR in-place
            # compaction. In both cases the returned `messages` is the compacted
            # set, so the gateway must persist all of it (offset 0), not slice
            # past the pre-compaction length (which would drop everything).
            _effective_history_offset = (
                0 if (_session_was_split or _compacted_in_place) else len(agent_history)
            )

            if not final_response:
                final_response = _normalize_empty_agent_response(
                    result, final_response or "", history_len=len(agent_history),
                )
                final_response = _sanitize_gateway_final_response(source.platform, final_response)
                if not final_response:
                    final_response = f"⚠️ {result['error']}" if result.get("error") else ""
                return {
                    "final_response": final_response,
                    "messages": result.get("messages", []),
                    "api_calls": result.get("api_calls", 0),
                    "failed": result.get("failed", False),
                    "partial": result.get("partial", False),
                    "completed": result.get("completed"),
                    "interrupted": result.get("interrupted", False),
                    "interrupt_message": result.get("interrupt_message"),
                    "error": result.get("error"),
                    "compression_exhausted": result.get("compression_exhausted", False),
                    "tools": tools_holder[0] or [],
                    "history_offset": _effective_history_offset,
                    "compacted_in_place": _compacted_in_place,
                    "session_id": effective_session_id,
                    "last_prompt_tokens": _last_prompt_toks,
                    "input_tokens": _input_toks,
                    "output_tokens": _output_toks,
                    "model": _resolved_model,
                    "context_length": _context_length,
                }
            
            # Scan tool results for MEDIA:<path> tags that need to be delivered
            # as native audio/file attachments.  The TTS tool embeds MEDIA: tags
            # in its JSON response, but the model's final text reply usually
            # doesn't include them.  We collect unique tags from tool results and
            # append any that aren't already present in the final response, so the
            # adapter's extract_media() can find and deliver the files exactly once.
            #
            # Scope the scan to THIS turn's tool results only. ``agent_history``
            # was passed into run_conversation as ``conversation_history``, so the
            # agent's returned ``messages`` list is ``agent_history`` followed by
            # the messages produced this turn. Slicing at ``len(agent_history)``
            # isolates the current turn precisely, so a stale MEDIA: path emitted
            # by a tool several turns earlier (still present in the full message
            # list) can never leak onto a later text-only reply. (Fixes #34608)
            #
            # Path-based deduplication against _history_media_paths (collected
            # before run_conversation) is retained as a secondary guard. It is
            # also the sole guard on the fallback branch taken when mid-run
            # context compression shrinks the message list below the original
            # history length, preserving the compression-safe behaviour of #160.
            if "MEDIA:" not in final_response:
                media_tags, has_voice_directive = _collect_auto_append_media_tags(
                    result.get("messages", []),
                    history_offset=len(agent_history),
                    history_media_paths=_history_media_paths,
                )

                if media_tags:
                    seen = set()
                    unique_tags = []
                    for tag in media_tags:
                        if tag not in seen:
                            seen.add(tag)
                            unique_tags.append(tag)
                    if has_voice_directive:
                        unique_tags.insert(0, "[[audio_as_voice]]")
                    final_response = final_response + "\n" + "\n".join(unique_tags)
            
            # Auto-generate session title after first exchange (non-blocking)
            if final_response and self._session_db:
                try:
                    from agent.title_generator import maybe_auto_title
                    all_msgs = result_holder[0].get("messages", []) if result_holder[0] else []
                    # In Gateway mode, auto-title failures must NOT be
                    # surfaced as user-visible messages (fixes #23246).
                    # Log them at debug level only — they are not actionable
                    # to the end user. CLI mode keeps the existing behaviour
                    # via the agent's _emit_auxiliary_failure path.
                    def _title_failure_cb(task: str, exc: BaseException) -> None:
                        logger.debug(
                            "Gateway auto-title failure suppressed (not user-visible): %s: %s",
                            task, exc,
                        )
                    maybe_auto_title_kwargs = {
                        "failure_callback": _title_failure_cb,
                        "main_runtime": {
                            "model": getattr(agent, "model", None),
                            "provider": getattr(agent, "provider", None),
                            "base_url": getattr(agent, "base_url", None),
                            "api_key": getattr(agent, "api_key", None),
                            "api_mode": getattr(agent, "api_mode", None),
                        } if agent else None,
                    }
                    if self._is_telegram_topic_lane(source):
                        maybe_auto_title_kwargs["title_callback"] = lambda title: self._schedule_telegram_topic_title_rename(
                            source,
                            effective_session_id,
                            title,
                        )
                    elif self._is_discord_auto_thread_lane(source):
                        maybe_auto_title_kwargs["title_callback"] = lambda title: self._schedule_discord_semantic_thread_rename(
                            source,
                            effective_session_id,
                            title,
                        )
                    maybe_auto_title(
                        getattr(self._session_db, "_db", self._session_db),
                        effective_session_id,
                        message,
                        final_response,
                        all_msgs,
                        **maybe_auto_title_kwargs,
                    )
                except Exception:
                    pass

            return {
                "final_response": final_response,
                "last_reasoning": result.get("last_reasoning"),
                "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                "api_calls": result_holder[0].get("api_calls", 0) if result_holder[0] else 0,
                "completed": result_holder[0].get("completed") if result_holder[0] else None,
                "interrupted": result_holder[0].get("interrupted", False) if result_holder[0] else False,
                "partial": result_holder[0].get("partial", False) if result_holder[0] else False,
                "error": result_holder[0].get("error") if result_holder[0] else None,
                "interrupt_message": result_holder[0].get("interrupt_message") if result_holder[0] else None,
                "tools": tools_holder[0] or [],
                "history_offset": _effective_history_offset,
                "compacted_in_place": _compacted_in_place,
                "last_prompt_tokens": _last_prompt_toks,
                "input_tokens": _input_toks,
                "output_tokens": _output_toks,
                "model": _resolved_model,
                "context_length": _context_length,
                "session_id": effective_session_id,
                "response_previewed": result.get("response_previewed", False),
                "response_transformed": result.get("response_transformed", False),
                # Pass through the agent_persisted flag so the persistence block
                # above can correctly determine whether the codex app-server path
                # self-persisted (it didn't — see codex_runtime.py).  Default
                # True preserves the skip-db behaviour for the standard runtime.
                "agent_persisted": (result_holder[0].get("agent_persisted", True) if result_holder[0] else True),
            }
        
        # Start progress message sender if enabled. Gate on needs_progress_queue
        # (tool_progress OR thinking_progress), not tool_progress alone: the
        # sender drains BOTH tool-progress lines and _thinking scratch bubbles.
        # With the old tool_progress-only gate, a thinking_progress:true /
        # tool_progress:off user had the callback queue _thinking messages that
        # no task ever drained — so they silently never appeared.
        progress_task = None
        if needs_progress_queue:
            progress_task = asyncio.create_task(send_progress_messages())

        # Start the tool-call log writer when tool_progress == "log".
        log_task = None
        if log_mode_enabled:
            log_task = asyncio.create_task(write_tool_log())

        # Start stream consumer task — polls for consumer creation since it
        # happens inside run_sync (thread pool) after the agent is constructed.
        stream_task = None

        async def _start_stream_consumer():
            """Wait for the stream consumer to be created, then run it."""
            for _ in range(200):  # Up to 10s wait
                if stream_consumer_holder[0] is not None:
                    await stream_consumer_holder[0].run()
                    return
                await asyncio.sleep(0.05)

        stream_task = asyncio.create_task(_start_stream_consumer())
        
        # Track this agent as running for this session (for interrupt support)
        # We do this in a callback after the agent is created
        async def track_agent():
            # Wait for agent to be created
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if not session_key:
                return
            # Only promote the sentinel to the real agent if this run is still
            # current.  If /stop or /new bumped the generation while we were
            # spinning up, leave the newer run's slot alone — we'll be
            # discarded by the stale-result check in _handle_message_with_agent.
            if run_generation is not None and not self._is_session_run_current(
                session_key, run_generation
            ):
                logger.info(
                    "Skipping stale agent promotion for %s — generation %s is no longer current",
                    session_key or "",
                    run_generation,
                )
                return
            self._running_agents[session_key] = agent_holder[0]
            if self._draining:
                self._update_runtime_status("draining")
        
        tracking_task = asyncio.create_task(track_agent())
        
        # Monitor for interrupts from the adapter (new messages arriving).
        # This is the PRIMARY interrupt path for regular text messages —
        # Level 1 (base.py) catches them before _handle_message() is reached,
        # so the Level 2 running_agent.interrupt() path never fires.
        # The inactivity poll loop below has a BACKUP check in case this
        # task dies (no error handling = silent death = lost interrupts).
        _interrupt_detected = asyncio.Event()  # shared with backup check

        async def monitor_for_interrupt():
            if not session_key:
                return

            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                try:
                    # Re-resolve adapter each iteration so reconnects don't
                    # leave us holding a stale reference.
                    _adapter = self._adapter_for_source(source)
                    if not _adapter:
                        continue
                    # Check if adapter has a pending interrupt for this session.
                    # Must use session_key (build_session_key output) — NOT
                    # source.chat_id — because the adapter stores interrupt events
                    # under the full session key.
                    if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                        agent = agent_holder[0]
                        if agent:
                            # Peek at the pending message text WITHOUT consuming it.
                            # The message must remain in _pending_messages so the
                            # post-run dequeue at _dequeue_pending_event() can
                            # retrieve the full MessageEvent (with media metadata).
                            # If we pop here, a race exists: the agent may finish
                            # before checking _interrupt_requested, and the message
                            # is lost — neither the interrupt path nor the dequeue
                            # path finds it.
                            _peek_event = _adapter._pending_messages.get(session_key)
                            pending_text = None
                            if _peek_event is not None:
                                pending_text = _peek_event.text or ""
                                # Transcribe audio media BEFORE signaling the
                                # agent, so voice messages interrupt with the
                                # real transcript instead of an empty string
                                # (or file-path placeholder). Matches the UX
                                # of fresh voice messages including the
                                # optional 🎙️ echo back to the user.
                                _media_urls = getattr(_peek_event, "media_urls", None) or []
                                _media_types = getattr(_peek_event, "media_types", None) or []
                                _audio_paths = []
                                for _i, _path in enumerate(_media_urls):
                                    _mtype = _media_types[_i] if _i < len(_media_types) else ""
                                    _is_audio = (
                                        _mtype.startswith("audio/")
                                        or getattr(_peek_event, "message_type", None) in (MessageType.VOICE, MessageType.AUDIO)
                                    )
                                    if _is_audio:
                                        _audio_paths.append(_path)
                                if _audio_paths:
                                    try:
                                        _enriched, _transcripts = await self._enrich_message_with_transcription(
                                            pending_text, _audio_paths,
                                        )
                                        pending_text = _enriched
                                        if _transcripts and self._should_echo_stt_transcripts():
                                            _echo_meta = {"thread_id": source.thread_id} if source.thread_id else None
                                            for _tx in _transcripts:
                                                try:
                                                    await _adapter.send(
                                                        source.chat_id,
                                                        f'🎙️ "{_tx}"',
                                                        metadata=_echo_meta,
                                                    )
                                                except Exception as _echo_exc:
                                                    logger.debug(
                                                        "Voice-interrupt echo failed (non-fatal): %s",
                                                        _echo_exc,
                                                    )
                                    except Exception as _trans_exc:
                                        logger.warning(
                                            "Voice-interrupt transcription failed: %s", _trans_exc,
                                        )
                                elif not pending_text and _media_urls:
                                    pending_text = _build_media_placeholder(_peek_event)
                            logger.debug("Interrupt detected from adapter, signaling agent...")
                            agent.interrupt(pending_text)
                            _interrupt_detected.set()
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as _mon_err:
                    logger.debug("monitor_for_interrupt error (will retry): %s", _mon_err)
        
        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())

        # Periodic "still working" notifications for long-running tasks.
        # Fires every N seconds so the user knows the agent hasn't died.
        # Config: agent.gateway_notify_interval in config.yaml, or
        # HERMES_AGENT_NOTIFY_INTERVAL env var.  Default 180s (3 min).
        # 0 = disable notifications.
        _NOTIFY_INTERVAL_RAW = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _NOTIFY_INTERVAL = _NOTIFY_INTERVAL_RAW if _NOTIFY_INTERVAL_RAW > 0 else None
        _long_running_mode = _display_surface_mode(
            "long_running_notifications",
            default=True,
            allow_generic=True,
        )
        if _long_running_mode == "off":
            _NOTIFY_INTERVAL = None
        _notify_start = time.time()

        async def _notify_long_running():
            if _NOTIFY_INTERVAL is None:
                return  # Notifications disabled (gateway_notify_interval: 0)
            _notify_adapter = self._adapter_for_source(source)
            if not _notify_adapter:
                return
            # Track the heartbeat message id so we can edit-in-place on
            # platforms that support it (Telegram, Discord, Slack, etc.)
            # instead of spamming a new "Still working" bubble every
            # interval. Falls back to send-new when edit fails or isn't
            # supported by the adapter.
            _heartbeat_msg_id: Optional[str] = None
            while True:
                await asyncio.sleep(_NOTIFY_INTERVAL)
                # Stop heartbeating once this run no longer owns the session
                # slot or the executor has finished — otherwise a stale
                # "running: delegate_task" bubble can outlive the run that
                # spawned it (#12029). _executor_task is a closure var bound
                # just after this task is scheduled; tolerate the brief window
                # before then (the first wake is _NOTIFY_INTERVAL away anyway).
                try:
                    _exec_ref = _executor_task
                except NameError:
                    _exec_ref = None
                if not self._should_emit_long_running_notification(
                    session_key, agent_holder[0], _exec_ref
                ):
                    break
                _elapsed_mins = int((time.time() - _notify_start) // 60)
                # Include agent activity context if available. Default
                # heartbeat is terse: elapsed + current tool. Verbose
                # iteration counter is gated on busy_ack_detail so users
                # who want it can opt in per platform.
                _agent_ref = agent_holder[0]
                _status_detail = ""
                _want_iteration_detail = bool(
                    resolve_display_setting(
                        user_config,
                        platform_key,
                        "busy_ack_detail",
                        True,
                    )
                )
                if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                    try:
                        _a = _agent_ref.get_activity_summary()
                        _parts = []
                        if _want_iteration_detail:
                            _parts.append(
                                f"iteration {_a['api_call_count']}/{_a['max_iterations']}"
                            )
                        _action = _a.get("current_tool") or _a.get("last_activity_desc")
                        if _action:
                            _parts.append(str(_action))
                        if _parts:
                            _status_detail = " — " + ", ".join(_parts)
                    except Exception:
                        pass
                _heartbeat_text = (
                    _generic_status_phrase("status")
                    if _long_running_mode == "generic"
                    else f"⏳ Working — {_elapsed_mins} min{_status_detail}"
                )
                try:
                    _notify_res = None
                    if _heartbeat_msg_id:
                        try:
                            _notify_res = await _notify_adapter.edit_message(
                                source.chat_id,
                                _heartbeat_msg_id,
                                _heartbeat_text,
                            )
                        except Exception as _ee:
                            logger.debug("Heartbeat edit failed: %s", _ee)
                            _notify_res = None
                    if not (_notify_res and getattr(_notify_res, "success", False)):
                        _notify_res = await _notify_adapter.send(
                            source.chat_id,
                            _heartbeat_text,
                            metadata=_non_conversational_metadata(_status_thread_metadata, platform=source.platform),
                        )
                        if getattr(_notify_res, "success", False) and getattr(
                            _notify_res, "message_id", None
                        ):
                            _heartbeat_msg_id = str(_notify_res.message_id)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
                except Exception as _ne:
                    logger.debug("Long-running notification error: %s", _ne)

        _notify_task = asyncio.create_task(_notify_long_running())

        def _stream_confirmed_final_delivery(
            consumer,
            final_text: str,
            *,
            previewed: bool = False,
        ) -> bool:
            """Return True only when the actual final reply reached the user."""
            if consumer is None:
                return False
            if getattr(consumer, "final_response_sent", False):
                return True
            if previewed:
                has_delivered_text = getattr(consumer, "has_delivered_text", None)
                if callable(has_delivered_text):
                    try:
                        return bool(has_delivered_text(final_text))
                    except Exception:
                        return False
            return False

        try:
            # Run in thread pool to not block.  Use an *inactivity*-based
            # timeout instead of a wall-clock limit: the agent can run for
            # hours if it's actively calling tools / receiving stream tokens,
            # but a hung API call or stuck tool with no activity for the
            # configured duration is caught and killed.  (#4815)
            #
            # Config: agent.gateway_timeout in config.yaml, or
            # HERMES_AGENT_TIMEOUT env var (env var takes precedence).
            # Default 1800s (30 min inactivity).  0 = unlimited.
            _agent_timeout_raw = _float_env("HERMES_AGENT_TIMEOUT", 1800)
            _agent_timeout = _agent_timeout_raw if _agent_timeout_raw > 0 else None
            _agent_warning_raw = _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900)
            _agent_warning = _agent_warning_raw if _agent_warning_raw > 0 else None
            _warning_fired = False
            _executor_task = asyncio.ensure_future(
                self._run_in_executor_with_context(run_sync)
            )

            _inactivity_timeout = False
            _POLL_INTERVAL = 5.0

            if _agent_timeout is None:
                # Unlimited — still poll periodically for backup interrupt
                # detection in case monitor_for_interrupt() silently died.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        response = _executor_task.result()
                        break
                    # Backup interrupt check: if the monitor task died or
                    # missed the interrupt, catch it here.
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
            else:
                # Poll loop: check the agent's built-in activity tracker
                # (updated by _touch_activity() on every tool call, API
                # call, and stream delta) every few seconds.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        response = _executor_task.result()
                        break
                    # Agent still running — check inactivity.
                    _agent_ref = agent_holder[0]
                    _idle_secs = 0.0
                    if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                        try:
                            _act = _agent_ref.get_activity_summary()
                            _idle_secs = _act.get("seconds_since_activity", 0.0)
                        except Exception:
                            pass
                    # Staged warning: fire once before escalating to full timeout.
                    if (not _warning_fired and _agent_warning is not None
                            and _idle_secs >= _agent_warning):
                        _warning_fired = True
                        _warn_adapter = self._adapter_for_source(source)
                        if _warn_adapter:
                            _elapsed_warn = int(_agent_warning // 60) or 1
                            _remaining_mins = int((_agent_timeout - _agent_warning) // 60) or 1
                            try:
                                await _warn_adapter.send(
                                    source.chat_id,
                                    f"⚠️ No activity for {_elapsed_warn} min. "
                                    f"If the agent does not respond soon, it will "
                                    f"be timed out in {_remaining_mins} min. "
                                    f"You can continue waiting or use /reset.",
                                    metadata=_status_thread_metadata,
                                )
                            except Exception as _warn_err:
                                logger.debug("Inactivity warning send error: %s", _warn_err)
                    if _idle_secs >= _agent_timeout:
                        _inactivity_timeout = True
                        break
                    # Backup interrupt check (same as unlimited path).
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()

            if _inactivity_timeout:
                # Build a diagnostic summary from the agent's activity tracker.
                _timed_out_agent = agent_holder[0]
                _activity = {}
                if _timed_out_agent and hasattr(_timed_out_agent, "get_activity_summary"):
                    try:
                        _activity = _timed_out_agent.get_activity_summary()
                    except Exception:
                        pass

                _last_desc = _activity.get("last_activity_desc", "unknown")
                _secs_ago = _activity.get("seconds_since_activity", 0)
                _cur_tool = _activity.get("current_tool")
                _iter_n = _activity.get("api_call_count", 0)
                _iter_max = _activity.get("max_iterations", 0)

                logger.error(
                    "Agent idle for %.0fs (timeout %.0fs) in session %s "
                    "| last_activity=%s | iteration=%s/%s | tool=%s",
                    _secs_ago, _agent_timeout, session_key,
                    _last_desc, _iter_n, _iter_max,
                    _cur_tool or "none",
                )

                # Interrupt the agent if it's still running so the thread
                # pool worker is freed.
                if _timed_out_agent and hasattr(_timed_out_agent, "interrupt"):
                    _timed_out_agent.interrupt(_INTERRUPT_REASON_TIMEOUT)

                _timeout_mins = int(_agent_timeout // 60) or 1

                # Construct a user-facing message with diagnostic context.
                _diag_lines = [
                    f"⏱️ Agent inactive for {_timeout_mins} min — no tool calls "
                    f"or API responses."
                ]
                if _cur_tool:
                    _diag_lines.append(
                        f"The agent appears stuck on tool `{_cur_tool}` "
                        f"({_secs_ago:.0f}s since last activity, "
                        f"iteration {_iter_n}/{_iter_max})."
                    )
                else:
                    _diag_lines.append(
                        f"Last activity: {_last_desc} ({_secs_ago:.0f}s ago, "
                        f"iteration {_iter_n}/{_iter_max}). "
                        "The agent may have been waiting on an API response."
                    )
                _diag_lines.append(
                    "To increase the limit, set agent.gateway_timeout in config.yaml "
                    "(value in seconds, 0 = no limit) and restart the gateway.\n"
                    "Try again, or use /reset to start fresh."
                )

                response = {
                    "final_response": "\n".join(_diag_lines),
                    "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                    "api_calls": _iter_n,
                    "tools": tools_holder[0] or [],
                    "history_offset": 0,
                    "failed": True,
                }

            # Track fallback model state: if the agent switched to a
            # fallback model during this run, persist it so /model shows
            # the actually-active model instead of the config default.
            # Skip eviction when the run failed — evicting a failed agent
            # forces MCP reinit on the next message for no benefit (the
            # same error will recur).  This was the root cause of #7130:
            # a bad model ID triggered fallback → eviction → recreation →
            # MCP reinit → same 400 → loop, burning 91% CPU for hours.
            _agent = agent_holder[0]
            _result_for_fb = result_holder[0]
            _run_failed = _result_for_fb.get("failed") if _result_for_fb else False
            if _agent is not None and hasattr(_agent, 'model') and not _run_failed:
                _cfg_model = _resolve_gateway_model()
                # Normalize _cfg_model the same way AIAgent.__init__ does, so a
                # vendor-prefixed config value (e.g. "deepseek/deepseek-v4-pro")
                # matches the agent's stripped model ("deepseek-v4-pro") on
                # native providers. Without this, _agent.model != _cfg_model is
                # always true for vendor-prefixed config and the cached agent is
                # evicted on every successful turn — destroying prompt caching.
                # Aggregators (openrouter, etc.) keep the vendor/model slug, so
                # they're left untouched.
                try:
                    from hermes_cli.model_normalize import (
                        _AGGREGATOR_PROVIDERS,
                        normalize_model_for_provider,
                    )
                    _agent_provider = getattr(_agent, 'provider', '') or ''
                    if _agent_provider and _agent_provider not in _AGGREGATOR_PROVIDERS:
                        _cfg_model = normalize_model_for_provider(_cfg_model, _agent_provider)
                except Exception:
                    pass
                if _agent.model != _cfg_model and not self._is_intentional_model_switch(session_key, _agent.model):
                    # Fallback activated on a successful run — evict cached
                    # agent so the next message retries the primary model.
                    self._evict_cached_agent(session_key)

            # Check if we were interrupted OR have a queued message (/queue).
            result = result_holder[0]
            adapter = self._adapter_for_source(source)
            
            # Get pending message from adapter.
            # Use session_key (not source.chat_id) to match adapter's storage keys.
            pending_event = None
            pending = None
            if result and adapter and session_key:
                pending_event = _dequeue_pending_event(adapter, session_key)
                # /queue overflow: after consuming the adapter's "next-up"
                # slot, promote the next queued event into it so the
                # recursive run's drain will see it.  This keeps the slot
                # occupied for the full FIFO chain, which (a) preserves
                # order, and (b) causes any mid-chain /queue to correctly
                # route to overflow rather than jumping the queue.
                pending_event = self._promote_queued_event(session_key, adapter, pending_event)
                if result.get("interrupted") and not pending_event and result.get("interrupt_message"):
                    interrupt_message = result.get("interrupt_message")
                    if _is_control_interrupt_message(interrupt_message):
                        logger.info(
                            "Ignoring control interrupt message for session %s: %s",
                            session_key or "?",
                            interrupt_message,
                        )
                    else:
                        pending = interrupt_message
                elif pending_event:
                    # Transcribe audio media on the dequeued event BEFORE it is
                    # handed back as the next user turn, so queued/interrupting
                    # voice messages drain with the real transcript instead of
                    # a file-path placeholder. When configured, echo each
                    # transcript back to the user in the same 🎙️ format as
                    # fresh voice messages.
                    _pending_text = pending_event.text or ""
                    _media_urls = getattr(pending_event, "media_urls", None) or []
                    _media_types = getattr(pending_event, "media_types", None) or []
                    _audio_paths = []
                    for _i, _path in enumerate(_media_urls):
                        _mtype = _media_types[_i] if _i < len(_media_types) else ""
                        _is_audio = (
                            _mtype.startswith("audio/")
                            or getattr(pending_event, "message_type", None) in (MessageType.VOICE, MessageType.AUDIO)
                        )
                        if _is_audio:
                            _audio_paths.append(_path)
                    if _audio_paths:
                        try:
                            _enriched, _transcripts = await self._enrich_message_with_transcription(
                                _pending_text, _audio_paths,
                            )
                            pending = _enriched or None
                            if _transcripts and self._should_echo_stt_transcripts():
                                _echo_meta = {"thread_id": source.thread_id} if source.thread_id else None
                                for _tx in _transcripts:
                                    try:
                                        await adapter.send(
                                            source.chat_id,
                                            f'🎙️ "{_tx}"',
                                            metadata=_echo_meta,
                                        )
                                    except Exception as _echo_exc:
                                        logger.debug(
                                            "Voice-drain echo failed (non-fatal): %s", _echo_exc,
                                        )
                        except Exception as _trans_exc:
                            logger.warning(
                                "Voice-drain transcription failed: %s", _trans_exc,
                            )
                            pending = _pending_text or _build_media_placeholder(pending_event)
                    else:
                        pending = _pending_text or _build_media_placeholder(pending_event)
                    if pending:
                        logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])

            # Leftover /steer: if a steer arrived after the last tool batch
            # (e.g. during the final API call), the agent couldn't inject it
            # and returned it in result["pending_steer"]. Deliver it as the
            # next user turn so it isn't silently dropped.
            if result and not pending and not pending_event:
                _leftover_steer = result.get("pending_steer")
                if _leftover_steer:
                    pending = _leftover_steer
                    logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])

            # Safety net: if the pending text is a slash command (e.g. "/stop",
            # "/new"), discard it — commands should never be passed to the agent
            # as user input.  The primary fix is in base.py (commands bypass the
            # active-session guard), but this catches edge cases where command
            # text leaks through the interrupt_message fallback.
            if pending and pending.strip().startswith("/"):
                _pending_parts = pending.strip().split(None, 1)
                _pending_cmd_word = _pending_parts[0][1:].lower() if _pending_parts else ""
                if _pending_cmd_word:
                    try:
                        from hermes_cli.commands import resolve_command as _rc_pending
                        if _rc_pending(_pending_cmd_word):
                            logger.info(
                                "Discarding command '/%s' from pending queue — "
                                "commands must not be passed as agent input",
                                _pending_cmd_word,
                            )
                            pending_event = None
                            pending = None
                    except Exception:
                        pass

            if self._draining and (pending_event or pending):
                logger.info(
                    "Discarding pending follow-up for session %s during gateway %s",
                    session_key or "?",
                    self._status_action_label(),
                )
                pending_event = None
                pending = None

            if pending_event or pending:
                logger.debug("Processing pending message: '%s...'", pending[:40])

                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and session_key and session_key in adapter._active_sessions:
                    adapter._active_sessions[session_key].clear()

                # Cap recursion depth to prevent resource exhaustion when the
                # user sends multiple messages while the agent keeps failing. (#816)
                if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
                    logger.warning(
                        "Interrupt recursion depth %d reached for session %s — "
                        "queueing message instead of recursing.",
                        _interrupt_depth, session_key,
                    )
                    adapter = self._adapter_for_source(source)
                    if adapter and pending_event:
                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)
                    elif adapter and hasattr(adapter, 'queue_message'):
                        adapter.queue_message(session_key, pending)
                    return result_holder[0] or {"final_response": response, "messages": history}

                was_interrupted = result.get("interrupted")
                if not was_interrupted:
                    # Queued message after normal completion — deliver the first
                    # response before processing the queued follow-up.
                    # Skip if streaming already delivered it.
                    _sc = stream_consumer_holder[0]
                    if _sc and stream_task:
                        try:
                            await asyncio.wait_for(stream_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            stream_task.cancel()
                            try:
                                await stream_task
                            except asyncio.CancelledError:
                                pass
                        except Exception as e:
                            logger.debug("Stream consumer wait before queued message failed: %s", e)
                    _previewed = bool(result.get("response_previewed"))
                    first_response = result.get("final_response", "")
                    _already_streamed = _stream_confirmed_final_delivery(
                        _sc,
                        first_response,
                        previewed=_previewed,
                    )
                    if first_response and not _already_streamed:
                        try:
                            logger.info(
                                "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                                session_key or "?",
                            )
                            await adapter.send(
                                source.chat_id,
                                first_response,
                                metadata=_status_thread_metadata,
                            )
                        except Exception as e:
                            logger.warning("Failed to send first response before queued message: %s", e)
                    elif first_response:
                        logger.info(
                            "Queued follow-up for session %s: skipping resend because final streamed delivery was confirmed.",
                            session_key or "?",
                        )
                    # Release deferred bg-review notifications now that the
                    # first response has been delivered.  Pop from the
                    # adapter's callback dict (prevents double-fire in
                    # base.py's finally block) and call it.
                    if getattr(type(adapter), "pop_post_delivery_callback", None) is not None:
                        _bg_cb = adapter.pop_post_delivery_callback(
                            session_key,
                            generation=run_generation,
                        )
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                    elif adapter and hasattr(adapter, "_post_delivery_callbacks"):
                        _bg_cb = adapter._post_delivery_callbacks.pop(session_key, None)
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                # else: interrupted — discard the interrupted response ("Operation
                # interrupted." is just noise; the user already knows they sent a
                # new message).

                updated_history = result.get("messages", history)
                next_source = source
                next_message = pending
                next_message_id = None
                next_channel_prompt = None
                next_session_key = session_key
                if pending_event is not None:
                    next_source = getattr(pending_event, "source", None) or source
                    if self._is_goal_continuation_event(pending_event) and not self._goal_still_active_for_session(session_id):
                        logger.info(
                            "Discarding stale goal continuation for session %s — goal is no longer active",
                            session_key or "?",
                        )
                        return result
                    # Resolve the follow-up's session key BEFORE preparing the
                    # inbound text: _prepare_inbound_message_text buffers native
                    # image paths under the key it is given, and the recursive
                    # _run_agent below consumes them under next_session_key.
                    # The write and consume keys must match or the images drop.
                    try:
                        next_session_key = self._session_key_for_source(next_source)
                    except Exception:
                        logger.debug(
                            "Queued follow-up session-key resolution failed; reusing %s",
                            session_key or "?",
                            exc_info=True,
                        )
                    next_message = await self._prepare_inbound_message_text(
                        event=pending_event,
                        source=next_source,
                        history=updated_history,
                        session_key=next_session_key,
                    )
                    if next_message is None:
                        return result
                    next_message_id = self._reply_anchor_for_event(pending_event)
                    next_channel_prompt = getattr(pending_event, "channel_prompt", None)

                # Restart typing indicator so the user sees activity while
                # the follow-up turn runs.  The outer _process_message_background
                # typing task is still alive but may be stale.
                _followup_adapter = self._adapter_for_source(source)
                if _followup_adapter:
                    try:
                        await _followup_adapter.send_typing(
                            source.chat_id,
                            metadata=_status_thread_metadata,
                        )
                    except Exception:
                        pass

                # Re-baseline the cached agent's message_count snapshot before
                # recursing into the in-band queued (/queue) follow-up turn.
                # The first turn has completed and flushed its own user +
                # assistant rows to the SessionDB, so the cross-process
                # coherence guard (#45966) — which this recursive _run_agent
                # call re-enters — would otherwise see the grown on-disk count
                # against the stale build-time snapshot and rebuild the agent
                # on THIS process's OWN writes, destroying the prompt-cache
                # prefix #46237 was merged to preserve.  The existing
                # re-baseline in _handle_message_with_agent only runs after the
                # whole _run_agent chain unwinds — too late for the in-band
                # follow-up.  Use the same (session_key, session_id) the
                # recursive call runs under so the snapshot matches exactly
                # what the follow-up's guard will consult.  Fail-safe in helper.
                await self._refresh_agent_cache_message_count(session_key, session_id)

                followup_result = await self._run_agent(
                    message=next_message,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=next_source,
                    session_id=session_id,
                    session_key=next_session_key,
                    run_generation=run_generation,
                    _interrupt_depth=_interrupt_depth + 1,
                    event_message_id=next_message_id,
                    channel_prompt=next_channel_prompt,
                )
                return _preserve_queued_followup_history_offset(result, followup_result)
        finally:
            # Stop progress sender, interrupt monitor, and notification task
            if progress_task:
                progress_task.cancel()
            if log_task:
                log_task.cancel()
            interrupt_monitor.cancel()
            _notify_task.cancel()

            # Wait for stream consumer to finish its final edit
            if stream_task:
                # If the agent never created a stream consumer (e.g. non-
                # streaming code path, or a test stub returning synchronously)
                # there is nothing to flush — cancel immediately instead of
                # waiting out the 5s timeout on a task that's just polling for
                # a consumer that will never arrive.  This was a 5-second
                # cost per non-streaming test run.
                _has_stream_consumer = (
                    stream_consumer_holder
                    and stream_consumer_holder[0] is not None
                )
                if not _has_stream_consumer:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(stream_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stream_task.cancel()
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass
            
            # Clean up tracking
            tracking_task.cancel()
            if session_key:
                # Only release the slot if this run's generation still owns
                # it.  A /stop or /new that bumped the generation while we
                # were unwinding has already installed its own state; this
                # guard prevents an old run from clobbering it on the way
                # out.
                self._release_running_agent_state(
                    session_key, run_generation=run_generation
                )
            if self._draining:
                self._update_runtime_status("draining")
            
            # Wait for cancelled tasks
            for task in [progress_task, log_task, interrupt_monitor, tracking_task, _notify_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        # If streaming already delivered the response, mark it so the
        # caller's send() is skipped (avoiding duplicate messages).
        # BUT: never suppress delivery when the agent failed — the error
        # message is new content the user hasn't seen, and it must reach
        # them even if streaming had sent earlier partial output.
        #
        # Also never suppress when the final response is "(empty)" — this
        # means the model failed to produce content after tool calls (common
        # with mimo-v2-pro, GLM-5, etc.).  The stream consumer may have
        # sent intermediate text ("Let me search for that…") alongside the
        # tool call, setting already_sent=True, but that text is NOT the
        # final answer.  Suppressing delivery here leaves the user staring
        # at silence.  (#10xxx — "agent stops after web search")
        _sc = stream_consumer_holder[0]
        if isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            # response_previewed means the interim_assistant_callback already
            # saw the final text, but only suppress the normal send if that
            # exact final text was delivered. Unrelated commentary/progress
            # must not be mistaken for the final response (#14238).
            _previewed = bool(response.get("response_previewed"))
            _content_delivered = bool(
                _sc and getattr(_sc, "final_content_delivered", False)
            )
            # Plugin hooks (e.g. transform_llm_output) may have appended content
            # after streaming finished — when the response was transformed, always
            # send the final version so the appended content reaches the client.
            _transformed = bool(response.get("response_transformed"))
            # Only suppress the normal send when the actual final reply reached
            # the user: the stream consumer streamed it (final_response_sent /
            # final_content_delivered), or the interim preview delivered that
            # *exact* final text. Unrelated commentary/progress shown during a
            # compression/session split must not be mistaken for the final
            # response (#14238).
            _streamed = _stream_confirmed_final_delivery(
                _sc,
                _final,
                previewed=_previewed,
            )
            if not _is_empty_sentinel and not _transformed and (_streamed or _content_delivered):
                logger.info(
                    "Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).",
                    session_key or "?",
                    _streamed,
                    _previewed,
                    _content_delivered,
                )
                response["already_sent"] = True
            elif not _is_empty_sentinel and _transformed and _sc is not None:
                # Plugin hooks transformed the response after streaming — edit the
                # existing streamed message instead of sending a duplicate.
                _sc_msg_id = _sc.message_id
                if _sc_msg_id:
                    try:
                        await _sc.adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=response["final_response"],
                            finalize=True,
                        )
                        response["already_sent"] = True
                        logger.info(
                            "Edited streamed message %s for session %s to include plugin-transformed content.",
                            _sc_msg_id, session_key or "?",
                        )
                    except Exception as _edit_err:
                        logger.warning(
                            "Failed to edit streamed message for session %s: %s",
                            session_key or "?", _edit_err,
                        )

        # Schedule deletion of tracked temporary progress bubbles after the
        # final response lands. Failed runs skip this so bubbles remain as
        # breadcrumbs for the user to see what work happened. Only fires on
        # adapters that support ``delete_message`` (see init above); failures
        # are swallowed — deletion is best-effort.
        if (
            _cleanup_progress
            and _cleanup_adapter is not None
            and _cleanup_msg_ids
            and session_key
            and isinstance(response, dict)
            and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            _ids_snapshot = list(_cleanup_msg_ids)
            _chat_id_snapshot = source.chat_id
            _adapter_snapshot = _cleanup_adapter
            _loop_snapshot = asyncio.get_running_loop()

            def _cleanup_temp_bubbles() -> None:
                async def _delete_all() -> None:
                    for _mid in _ids_snapshot:
                        try:
                            await _adapter_snapshot.delete_message(
                                _chat_id_snapshot, _mid
                            )
                        except Exception:
                            pass
                try:
                    safe_schedule_threadsafe(
                        _delete_all(), _loop_snapshot,
                        logger=logger,
                        log_message="Temp bubble cleanup scheduling error",
                    )
                except Exception:
                    pass

            try:
                _cleanup_adapter.register_post_delivery_callback(
                    session_key,
                    _cleanup_temp_bubbles,
                    generation=run_generation,
                )
            except Exception as _rpe:
                logger.debug("Post-delivery cleanup registration failed: %s", _rpe)

        return response

    def _resolve_profile_home_for_source(self, source: SessionSource) -> "Path":
        """Resolve which profile's HERMES_HOME should serve this inbound source.

        Prefers the profile the source was routed to (``source.profile`` — set
        by the /p/<profile>/ URL prefix or a per-credential adapter), falling
        back to the active profile (the multiplexer's own home).
        """
        from hermes_cli.profiles import get_active_profile_name, get_profile_dir
        try:
            name = (source.profile or "").strip() or get_active_profile_name() or "default"
            return get_profile_dir(name)
        except Exception:
            from hermes_constants import get_hermes_home
            return get_hermes_home()

    def _get_proxy_url(self) -> Optional[str]:
        """Return the proxy URL if proxy mode is configured, else None.

        Checks GATEWAY_PROXY_URL env var first (convenient for Docker),
        then ``gateway.proxy_url`` in config.yaml.
        """
        url = os.getenv("GATEWAY_PROXY_URL", "").strip()
        if url:
            return url.rstrip("/")
        cfg = _load_gateway_config()
        url = (cfg.get("gateway") or {}).get("proxy_url", "").strip()
        if url:
            return url.rstrip("/")
        return None

    async def _run_agent_via_proxy(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: "SessionSource",
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        event_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of
        running a local AIAgent.

        When ``GATEWAY_PROXY_URL`` (or ``gateway.proxy_url`` in config.yaml)
        is set, the gateway becomes a thin relay: it handles platform I/O
        (encryption, threading, media) and delegates all agent work to the
        remote server via ``POST /v1/chat/completions`` with SSE streaming.

        This lets a Docker container handle Matrix E2EE while the actual
        agent runs on the host with full access to local files, memory,
        skills, and a unified session store.
        """
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return {
                "final_response": "⚠️ Proxy mode requires aiohttp. Install with: pip install aiohttp",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return {
                "final_response": "⚠️ Proxy URL not configured (GATEWAY_PROXY_URL or gateway.proxy_url)",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)

        # Build messages in OpenAI chat format --------------------------
        #
        # The remote api_server can maintain session continuity via
        # X-Hermes-Session-Id, so it loads its own history.  We only
        # need to send the current user message.  If the remote has
        # no history for this session yet, include what we have locally
        # so the first exchange has context.
        #
        # We always include the current message.  For history, send a
        # compact version (text-only user/assistant turns) — the remote
        # handles tool replay and system prompts.
        api_messages: List[Dict[str, str]] = []

        if context_prompt:
            api_messages.append({"role": "system", "content": context_prompt})

        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                api_messages.append({"role": role, "content": content})

        api_messages.append({"role": "user", "content": message})

        # HTTP headers ---------------------------------------------------
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if proxy_key:
            headers["Authorization"] = f"Bearer {proxy_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        body = {
            "model": "hermes-agent",
            "messages": api_messages,
            "stream": True,
        }

        # Set up platform streaming if available -------------------------
        _stream_consumer = None
        _scfg = getattr(getattr(self, "config", None), "streaming", None)
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()

        platform_key = _platform_config_key(source.platform)
        user_config = _load_gateway_config()
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(
            user_config, platform_key, "streaming"
        )
        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )

        _thread_metadata: Optional[Dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)

        if _streaming_enabled:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
                _adapter = self._adapter_for_source(source)
                if _adapter:
                    _pause_typing_before_finalize = None
                    if source.platform == Platform.TELEGRAM and hasattr(_adapter, "pause_typing_for_chat"):
                        def _pause_typing_before_finalize(
                            _adapter=_adapter,
                            _chat_id=source.chat_id,
                        ) -> None:
                            _adapter.pause_typing_for_chat(_chat_id)
                    _adapter_supports_edit = getattr(_adapter, "SUPPORTS_MESSAGE_EDITING", True)
                    _effective_cursor = _scfg.cursor if _adapter_supports_edit else ""
                    _buffer_only = False
                    if source.platform == Platform.MATRIX:
                        _effective_cursor = ""
                        _buffer_only = True
                    # Fresh-final applies to Telegram only — other
                    # platforms either edit in place cheaply (Discord,
                    # Slack) or don't have the timestamp-on-edit
                    # problem.  (Ported from openclaw/openclaw#72038.)
                    _fresh_final_secs = (
                        float(getattr(_scfg, "fresh_final_after_seconds", 0.0) or 0.0)
                        if source.platform == Platform.TELEGRAM
                        else 0.0
                    )
                    _consumer_cfg = StreamConsumerConfig(
                        edit_interval=_scfg.edit_interval,
                        buffer_threshold=_scfg.buffer_threshold,
                        cursor=_effective_cursor,
                        buffer_only=_buffer_only,
                        fresh_final_after_seconds=_fresh_final_secs,
                        transport=_scfg.transport or "edit",
                        chat_type=getattr(source, "chat_type", "") or "",
                    )
                    _stream_consumer = GatewayStreamConsumer(
                        adapter=_adapter,
                        chat_id=source.chat_id,
                        config=_consumer_cfg,
                        metadata=_thread_metadata,
                        on_before_finalize=_pause_typing_before_finalize,
                        initial_reply_to_id=event_message_id,
                        run_still_current=_run_still_current,
                    )
            except Exception as _sc_err:
                logger.debug("Proxy: could not set up stream consumer: %s", _sc_err)

        # Run the stream consumer task in the background
        stream_task = None
        if _stream_consumer:
            stream_task = asyncio.create_task(_stream_consumer.run())

        # Send typing indicator
        _adapter = self._adapter_for_source(source)
        if _adapter:
            try:
                await _adapter.send_typing(source.chat_id, metadata=_thread_metadata)
            except Exception:
                pass

        # Make the HTTP request with SSE streaming -----------------------
        full_response = ""
        _start = time.time()

        try:
            _timeout = ClientTimeout(total=0, sock_read=1800)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(
                    f"{proxy_url}/v1/chat/completions",
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(
                            "Proxy error (%d) from %s: %s",
                            resp.status, proxy_url, error_text[:500],
                        )
                        return {
                            "final_response": f"⚠️ Proxy error ({resp.status}): {error_text[:300]}",
                            "messages": [],
                            "api_calls": 0,
                            "tools": [],
                        }

                    # Parse SSE stream
                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        if not _run_still_current():
                            logger.info(
                                "Discarding stale proxy stream for %s — generation %d is no longer current",
                                session_key or "?",
                                run_generation or 0,
                            )
                            return {
                                "final_response": "",
                                "messages": [],
                                "api_calls": 0,
                                "tools": [],
                                "history_offset": len(history),
                                "session_id": session_id,
                                "response_previewed": False,
                            }
                        text = chunk.decode("utf-8", errors="replace")
                        buffer += text

                        # Process complete SSE lines
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line[6:]
                                if data.strip() == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(data)
                                    choices = obj.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            full_response += content
                                            if _stream_consumer:
                                                _stream_consumer.on_delta(content)
                                except json.JSONDecodeError:
                                    pass
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError(
                                "Proxy SSE stream exceeded max buffer size without a line boundary"
                            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Proxy connection error to %s: %s", proxy_url, e)
            if not full_response:
                return {
                    "final_response": f"⚠️ Proxy connection error: {e}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }
            # Partial response — return what we got
        finally:
            # Finalize stream consumer
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()

        _elapsed = time.time() - _start
        if not _run_still_current():
            logger.info(
                "Discarding stale proxy result for %s — generation %d is no longer current",
                session_key or "?",
                run_generation or 0,
            )
            return {
                "final_response": "",
                "messages": [],
                "api_calls": 0,
                "tools": [],
                "history_offset": len(history),
                "session_id": session_id,
                "response_previewed": False,
            }
        logger.info(
            "proxy response: url=%s session=%s time=%.1fs response=%d chars",
            proxy_url, (session_id or "")[:20], _elapsed, len(full_response),
        )

        return {
            "final_response": full_response or "(No response from remote agent)",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_response},
            ],
            "api_calls": 1,
            "tools": [],
            "history_offset": len(history),
            "session_id": session_id,
            "response_previewed": _stream_consumer is not None and bool(full_response),
        }

