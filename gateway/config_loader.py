"""
GatewayConfigLoaderMixin — config_loader methods for GatewayRunner.

Extracted from gateway/run.py to reduce the 20k-line god file.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class GatewayConfigLoaderMixin:
    """GatewayConfigLoaderMixin — mixed into GatewayRunner via multiple inheritance."""

        def _load_provider_routing() -> dict:
            """Load OpenRouter provider routing preferences from config.yaml."""
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    return cfg.get("provider_routing", {}) or {}
            except Exception:
                pass
            return {}

        def _resolve_model_for_channel(
            self,
            platform: Platform,
            chat_id: str,
            *,
            user_config: Optional[dict] = None,
            thread_id: Optional[str] = None,
            parent_id: Optional[str] = None,
        ) -> str:
            """Resolve model for this channel: channel_overrides else global default."""
            config = getattr(self, "config", None)
            if config:
                override = _get_channel_override(
                    config,
                    platform,
                    chat_id,
                    thread_id=thread_id,
                    parent_id=parent_id,
                )
                if override and override.model:
                    return override.model
            return _resolve_gateway_model(user_config)

        def _load_background_notifications_mode() -> str:
            """Load background process notification mode from config or env var.

            Modes:
              - ``all``    — push running-output updates *and* the final message (default)
              - ``result`` — only the final completion message (regardless of exit code)
              - ``error``  — only the final message when exit code is non-zero
              - ``off``    — no watcher messages at all
            """
            mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
            if not mode:
                cfg = _load_gateway_runtime_config()
                raw = cfg_get(cfg, "display", "background_process_notifications")
                if raw is False:
                    mode = "off"
                elif raw not in {None, ""}:
                    mode = str(raw)
            mode = (mode or "all").strip().lower()
            valid = {"all", "result", "error", "off"}
            if mode not in valid:
                logger.warning(
                    "Unknown background_process_notifications '%s', defaulting to 'all'",
                    mode,
                )
                return "all"
            return mode

        def _load_show_reasoning() -> bool:
            """Load show_reasoning toggle from config.yaml display section."""
            cfg = _load_gateway_runtime_config()
            return is_truthy_value(
                cfg_get(cfg, "display", "show_reasoning"),
                default=False,
            )

        def _resolve_session_reasoning_config(
            self,
            *,
            source: Optional[SessionSource] = None,
            session_key: Optional[str] = None,
        ) -> dict | None:
            """Resolve reasoning effort for a session, honoring session overrides."""
            resolved_session_key = session_key
            if not resolved_session_key and source is not None:
                try:
                    resolved_session_key = self._session_key_for_source(source)
                except Exception:
                    resolved_session_key = None

            overrides = getattr(self, "_session_reasoning_overrides", {}) or {}
            if resolved_session_key and resolved_session_key in overrides:
                return overrides[resolved_session_key]
            return self._load_reasoning_config()

        def _load_restart_drain_timeout() -> float:
            """Load graceful gateway restart/stop drain timeout in seconds."""
            raw = os.getenv("HERMES_RESTART_DRAIN_TIMEOUT", "").strip()
            if not raw:
                cfg = _load_gateway_runtime_config()
                raw = str(cfg_get(cfg, "agent", "restart_drain_timeout", default="") or "").strip()
            value = parse_restart_drain_timeout(raw)
            if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
                try:
                    float(raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid restart_drain_timeout '%s', using default %.0fs",
                        raw,
                        DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
                    )
            return value

        def _set_session_reasoning_override(
            self,
            session_key: str,
            reasoning_config: Optional[dict],
        ) -> None:
            """Set or clear the session-scoped reasoning override."""
            if not session_key:
                return
            if not hasattr(self, "_session_reasoning_overrides"):
                self._session_reasoning_overrides = {}
            if reasoning_config is None:
                self._session_reasoning_overrides.pop(session_key, None)
            else:
                self._session_reasoning_overrides[session_key] = dict(reasoning_config)

        def _load_busy_text_mode() -> str:
            """Resolve normal busy TEXT follow-up behavior.

            ``busy_input_mode`` is the single source of truth (default
            ``interrupt``). The legacy ``busy_text_mode`` knob is honored only
            when a user explicitly set it, so existing queue setups keep
            working; new installs follow ``busy_input_mode``. Returns one of
            ``interrupt`` | ``queue`` (``steer`` is handled upstream by
            ``busy_input_mode`` and maps to non-queue text handling here).
            """
            # Legacy explicit override wins for backward compat.
            legacy = os.getenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "").strip().lower()
            if not legacy:
                cfg = _load_gateway_runtime_config()
                legacy = str(cfg_get(cfg, "display", "busy_text_mode", default="") or "").strip().lower()
            if legacy == "interrupt":
                return "interrupt"
            if legacy == "queue":
                return "queue"
            # No explicit legacy knob → follow busy_input_mode.
            input_mode = GatewayRunner._load_busy_input_mode()
            return "queue" if input_mode == "queue" else "interrupt"

        def _load_reasoning_config() -> dict | None:
            """Load reasoning effort from config.yaml.

            Reads agent.reasoning_effort from config.yaml. Valid: "none",
            "minimal", "low", "medium", "high", "xhigh". Returns None to use
            default (medium).
            """
            from hermes_constants import parse_reasoning_effort
            cfg = _load_gateway_runtime_config()
            # Keep the raw value — coercing with ``or ""`` turns a YAML boolean
            # False (``reasoning_effort: false``/``off``/``no``) into "", silently
            # re-enabling thinking for users who explicitly disabled it.
            effort = cfg_get(cfg, "agent", "reasoning_effort", default="")
            result = parse_reasoning_effort(effort)
            if effort and str(effort).strip() and result is None:
                logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
            return result

        def _load_ephemeral_system_prompt() -> str:
            """Load ephemeral system prompt from config or env var.
            
            Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then falls back to
            agent.system_prompt in ~/.hermes/config.yaml.
            """
            prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
            if prompt:
                return prompt
            cfg = _load_gateway_runtime_config()
            return str(cfg_get(cfg, "agent", "system_prompt", default="") or "").strip()

        def _load_fallback_model() -> list | None:
            """Load fallback provider chain from config.yaml.

            Returns the merged effective chain from ``fallback_providers`` plus any
            legacy ``fallback_model`` entries. ``fallback_providers`` stays first
            when both keys are present.
            """
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    fb = get_fallback_chain(cfg)
                    if fb:
                        return fb
            except Exception:
                pass
            return None

        def _load_prefill_messages() -> List[Dict[str, Any]]:
            """Load ephemeral prefill messages from config or env var.
            
            Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
            the top-level prefill_messages_file key in ~/.hermes/config.yaml.
            agent.prefill_messages_file is accepted as a legacy fallback.
            Relative paths are resolved from ~/.hermes/.
            """
            file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
            if not file_path:
                cfg = _load_gateway_runtime_config()
                file_path = str(cfg.get("prefill_messages_file", "") or "")
                if not file_path:
                    file_path = str(cfg_get(cfg, "agent", "prefill_messages_file", default="") or "")
            if not file_path:
                return []
            path = Path(file_path).expanduser()
            if not path.is_absolute():
                path = _hermes_home / path
            if not path.exists():
                logger.warning("Prefill messages file not found: %s", path)
                return []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    logger.warning("Prefill messages file must contain a JSON array: %s", path)
                    return []
                return data
            except Exception as e:
                logger.warning("Failed to load prefill messages from %s: %s", path, e)
                return []

        def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
            """Parse `/reasoning` args into `(value, persist_global)`.

            `/reasoning <level>` is session-scoped by default. `--global` may be
            supplied in any position to persist the change to config.yaml.
            """
            import shlex

            text = str(raw_args or "").strip().replace("—", "--")
            if not text:
                return "", False
            try:
                tokens = shlex.split(text)
            except ValueError:
                tokens = text.split()

            persist_global = False
            value_tokens = []
            for token in tokens:
                if token == "--global":
                    persist_global = True
                else:
                    value_tokens.append(token)
            return " ".join(value_tokens).strip().lower(), persist_global

        def _get_system_prompt_for_channel(
            self,
            platform: Platform,
            chat_id: str,
            *,
            thread_id: Optional[str] = None,
            parent_id: Optional[str] = None,
        ) -> str:
            """Ephemeral system prompt for this channel/thread.

            Uses ``channel_overrides`` when set, else the global gateway prompt.
            Legacy ``channel_prompts`` are applied separately via ``event.channel_prompt``
            in ``run_sync`` (adapter ``resolve_channel_prompt``), so they are not
            duplicated here.
            """
            config = getattr(self, "config", None)
            if config:
                override = _get_channel_override(
                    config,
                    platform,
                    chat_id,
                    thread_id=thread_id,
                    parent_id=parent_id,
                )
                if override and override.system_prompt:
                    return (override.system_prompt or "").strip()
            return getattr(self, "_ephemeral_system_prompt", None) or ""

        def _load_service_tier() -> str | None:
            """Load Priority Processing setting from config.yaml.

            Reads agent.service_tier from config.yaml. Accepted values mirror the CLI:
            "fast"/"priority"/"on" => "priority", while "normal"/"off" disables it.
            Returns None when unset or unsupported.
            """
            cfg = _load_gateway_runtime_config()
            raw = str(cfg_get(cfg, "agent", "service_tier", default="") or "").strip()

            value = raw.lower()
            if not value or value in {"normal", "default", "standard", "off", "none"}:
                return None
            if value in {"fast", "priority", "on"}:
                return "priority"
            logger.warning("Unknown service_tier '%s', ignoring", raw)
            return None

        def _load_busy_input_mode() -> str:
            """Load gateway drain-time busy-input behavior from config/env."""
            mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
            if not mode:
                cfg = _load_gateway_runtime_config()
                mode = str(cfg_get(cfg, "display", "busy_input_mode", default="") or "").strip().lower()
            if mode == "queue":
                return "queue"
            if mode == "steer":
                return "steer"
            return "interrupt"

