"""LLM Secrets (``scrt4``) secret source for Hermes.

LLM Secrets is a passkey-protected encrypted vault for API keys and
credentials, designed for AI coding agent environments. Secrets are
encrypted with AES-256-GCM and unlocked via FIDO2/WebAuthn (phone,
laptop, security key).

This source connects to the ``scrt4-daemon`` Unix socket at startup
and pulls all unlocked secrets into the process environment.

Prerequisites (user runs once):
    1. Install ``scrt4``: curl -fsSL https://install.llmsecrets.com/native | sh
    2. ``scrt4 setup --agent`` (or ``scrt4 setup`` for interactive)
    3. ``scrt4 unlock`` (starts a 20-hour session)
    4. ``scrt4 import .env`` (import existing credentials)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource

logger = logging.getLogger(__name__)

# Default paths scrt4 uses for its daemon socket
_XDG_RUNTIME_DIR_VAR = "XDG_RUNTIME_DIR"
_DEFAULT_SOCKET_PATH = "/run/user/1000/scrt4.sock"

# How long to wait for daemon responses, in seconds
_DAEMON_TIMEOUT = 5

# How long to wait for daemon startup
_DAEMON_START_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Low-level daemon IPC
# ---------------------------------------------------------------------------


def _socket_path() -> Path:
    """Return the scrt4 daemon socket path."""
    runtime_dir = os.environ.get(_XDG_RUNTIME_DIR_VAR)
    if runtime_dir:
        return Path(runtime_dir) / "scrt4.sock"
    return Path(_DEFAULT_SOCKET_PATH)


def _daemon_request(method: str, params: object = None) -> Optional[dict]:
    """Send a JSON request to the scrt4 daemon and return the parsed response.

    The protocol uses ``{"method": "<method>", "params": <params>}`` JSON
    messages over a Unix stream socket, one per connection.

    Returns ``None`` when the daemon is unreachable or returns an error.
    """
    payload = {"method": method}
    if params is not None:
        payload["params"] = params

    path = _socket_path()
    if not path.exists():
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_DAEMON_TIMEOUT)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload).encode() + b"\n")

        # Read response (newline-delimited JSON)
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                line, _ = buf.split(b"\n", 1)
                sock.close()
                resp = json.loads(line)
                if resp.get("success"):
                    return resp.get("data")
                return None
        sock.close()
        return None
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout, json.JSONDecodeError) as exc:
        logger.debug("scrt4 daemon IPC error: %s", exc)
        return None


def _daemon_is_reachable() -> bool:
    """Return True when the scrt4 daemon socket exists and responds."""
    return _daemon_request("status") is not None


def _daemon_is_active() -> bool:
    """Return True when the daemon has an active unlocked session."""
    data = _daemon_request("status")
    if data is None:
        return False
    return bool(data.get("active", False))


def _start_daemon() -> bool:
    """Try to start the scrt4 daemon. Returns True if started successfully."""
    binary = _find_scrt4()
    if not binary:
        return False
    daemon_binary = binary.parent / "scrt4-daemon"
    if not daemon_binary.exists():
        daemon_binary = Path(str(binary) + "-daemon")
        if not daemon_binary.exists():
            return False

    try:
        subprocess.Popen(
            [str(daemon_binary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the socket to appear
        deadline = time.time() + _DAEMON_START_TIMEOUT
        while time.time() < deadline:
            if _daemon_is_reachable():
                return True
            time.sleep(0.5)
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_scrt4() -> Optional[Path]:
    """Locate the ``scrt4`` binary on PATH or common install locations."""
    import shutil

    binary = shutil.which("scrt4")
    if binary:
        return Path(binary)

    # Common install locations
    for candidate in [
        Path.home() / ".local" / "bin" / "scrt4",
        Path("/usr/local/bin/scrt4"),
    ]:
        if candidate.exists():
            return candidate

    return None


def _list_secret_names() -> Optional[List[str]]:
    """Return secret names from the daemon, or None if unavailable."""
    data = _daemon_request("list")
    if data is None:
        return None
    return data.get("names")


def _reveal_secret(name: str) -> Optional[str]:
    """Reveal a single secret value from the daemon.

    Returns ``None`` if the secret doesn't exist or the daemon can't
    reveal it (e.g. WebAuthn 2FA is required).
    """
    data = _daemon_request("reveal", {"name": name})
    if data is None:
        return None
    return data.get("value")


def _reveal_all_secrets() -> Optional[Dict[str, str]]:
    """Reveal all secrets from the daemon in one call.

    This may trigger a WebAuthn 2FA challenge if configured. Returns
    ``None`` if the daemon can't fulfil the request.
    """
    data = _daemon_request("reveal_all")
    if data is None:
        return None
    return data.get("secrets")


# ---------------------------------------------------------------------------
# SecretSource adapter
# ---------------------------------------------------------------------------


def _is_available() -> bool:
    """Return True when scrt4 is installed and the daemon is reachable."""
    binary = _find_scrt4()
    if not binary:
        return False
    # If the daemon isn't reachable, try to start it
    if not _daemon_is_reachable():
        return _start_daemon()
    return True


class LLMSecretsSource(SecretSource):
    """LLM Secrets (scrt4) as a registered secret source.

    Connects to the running ``scrt4-daemon`` Unix socket. The user must
    have an active session (``scrt4 unlock``) for secrets to be available.
    """

    name = "llm_secrets"
    label = "LLM Secrets (scrt4)"
    shape = "bulk"  # Injects every secret in the vault

    def is_enabled(self, cfg: dict) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("enabled"))

    def override_existing(self, cfg: dict) -> bool:
        # Default True: the vault is the source of truth.
        return bool(isinstance(cfg, dict) and cfg.get("override_existing", True))

    def config_schema(self) -> dict:
        return {
            "enabled": {
                "description": "Master switch",
                "default": False,
            },
            "cache_ttl_seconds": {
                "description": "How often to re-fetch secrets from the daemon",
                "default": 300,
            },
            "override_existing": {
                "description": "Vault values overwrite .env/shell values",
                "default": True,
            },
            "binary_path": {
                "description": "Absolute path to scrt4 binary (auto-detected if empty)",
                "default": "",
            },
        }

    def protected_env_vars(self, cfg: dict) -> frozenset:
        # scrt4 doesn't use environment variables for its own auth
        return frozenset()

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        binary = _find_scrt4()
        if not binary:
            result.error = (
                "scrt4 binary not found. Install with: "
                "curl -fsSL https://install.llmsecrets.com/native | sh"
            )
            result.error_kind = ErrorKind.BINARY_MISSING
            return result

        # Ensure the daemon is running
        if not _daemon_is_reachable():
            if not _start_daemon():
                result.error = (
                    "scrt4 daemon is not running and could not be started. "
                    "Run: scrt4-daemon &"
                )
                result.error_kind = ErrorKind.NOT_CONFIGURED
                return result

        # Check for an active session
        if not _daemon_is_active():
            result.error = (
                "scrt4 daemon is running but no session is active. "
                "Run: scrt4 unlock"
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        # Fetch secrets — try reveal_all first (single call), fall back
        # to iterative reveal.
        secrets = _reveal_all_secrets()

        if secrets is None:
            # reveal_all may be gated by WebAuthn 2FA — try individual reveals
            names = _list_secret_names()
            if not names:
                result.error = "No secrets found in the active scrt4 session."
                result.error_kind = ErrorKind.NOT_CONFIGURED
                return result

            secrets = {}
            for name in names:
                value = _reveal_secret(name)
                if value is not None:
                    secrets[name] = value

            if not secrets:
                result.error = (
                    "Could not reveal any secrets. "
                    "Run `scrt4 view` to approve the reveal."
                )
                result.error_kind = ErrorKind.NOT_CONFIGURED
                return result

        result.secrets = secrets
        return result
