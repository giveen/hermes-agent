"""GNOME Keyring / libsecret secret source for Hermes.

Uses the Secret Service D-Bus API (org.freedesktop.secrets) via the
``secretstorage`` Python library to store and retrieve credentials
in the system keyring — no cloud, no extra daemon, no passkeys.

The keyring is unlocked automatically on desktop login and is the same
keyring Chrome, VS Code, and other desktop apps use.

Secrets are stored with attributes ``{service: "hermes", key: "ENV_VAR"}``
and can be managed with ``secret-tool`` or the Hermes CLI.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource

logger = logging.getLogger(__name__)

# The service label used to namespace Hermes secrets in the keyring
_SERVICE = "hermes"

# Attributes used to tag every Hermes secret in the keyring
_HERMES_ATTRS = {
    "service": _SERVICE,
}


# ---------------------------------------------------------------------------
# Low-level keyring operations via secretstorage
# ---------------------------------------------------------------------------


def _get_connection():
    """Open a D-Bus connection to the Secret Service."""
    import secretstorage

    return secretstorage.dbus_init()


def _get_collection(connection):
    """Return the default collection (unlocked on desktop login)."""
    import secretstorage

    return secretstorage.get_default_collection(connection)


def _store_secret(env_var: str, value: str, label: str = "") -> bool:
    """Store ``value`` for ``env_var`` in the keyring.

    Returns True on success.
    """
    try:
        conn = _get_connection()
        collection = _get_collection(conn)
        attrs = {**_HERMES_ATTRS, "key": env_var}
        display_name = label or f"Hermes: {env_var}"
        collection.create_item(display_name, attrs, value, replace=True)
        conn.close()
        return True
    except Exception as exc:
        logger.warning("Failed to store %s in keyring: %s", env_var, exc)
        return False


def _lookup_secret(env_var: str) -> Optional[str]:
    """Look up ``env_var`` in the keyring.

    Returns the secret value, or None if not found.
    """
    conn = None
    try:
        conn = _get_connection()
        collection = _get_collection(conn)
        if collection.is_locked():
            logger.debug("Keyring collection is locked — cannot look up %s", env_var)
            return None

        attrs = {**_HERMES_ATTRS, "key": env_var}
        items = list(collection.search_items(attrs))

        if not items:
            return None

        # Read secret BEFORE closing the connection (Item proxy goes invalid)
        return items[0].get_secret().decode("utf-8")
    except Exception as exc:
        logger.debug("Keyring lookup failed for %s: %s", env_var, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def _list_all() -> Optional[Dict[str, str]]:
    """Return ALL Hermes secrets from the keyring as a dict.

    Returns None if the keyring is unavailable or locked.
    """
    conn = None
    try:
        conn = _get_connection()
        collection = _get_collection(conn)
        if collection.is_locked():
            return None

        attrs = {**_HERMES_ATTRS}
        items = list(collection.search_items(attrs))

        # Read secrets BEFORE closing the connection (Item proxy goes invalid)
        secrets = {}
        for item in items:
            try:
                key = item.get_attributes().get("key", "")
                value = item.get_secret().decode("utf-8")
                if key:
                    secrets[key] = value
            except Exception:
                continue

        return secrets
    except Exception as exc:
        logger.debug("Keyring list failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _collection_is_locked() -> Optional[bool]:
    """Return True if the keyring collection is locked, False if unlocked, None if unreachable."""
    try:
        conn = _get_connection()
        collection = _get_collection(conn)
        locked = collection.is_locked()
        conn.close()
        return locked
    except Exception:
        return None


def _lock_collection() -> bool:
    """Lock the keyring collection. Returns True on success."""
    try:
        conn = _get_connection()
        collection = _get_collection(conn)
        collection.lock()
        conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI helpers for setup wizard
# ---------------------------------------------------------------------------


def store_env_var(env_var: str, value: str) -> bool:
    """Store a single env-var credential in the keyring.

    Called from the setup wizard when the user enters a credential.
    """
    return _store_secret(env_var, value, label=f"Hermes: {env_var}")


def lookup_env_var(env_var: str) -> Optional[str]:
    """Look up a single env-var credential from the keyring."""
    return _lookup_secret(env_var)


def check_available() -> tuple[bool, str]:
    """Check if the keyring is available and unlocked.

    Returns (available: bool, message: str).
    """
    try:
        locked = _collection_is_locked()
        if locked is None:
            return False, "Could not connect to the Secret Service (D-Bus)."
        if locked:
            return False, "Keyring is locked. Unlock it from your desktop session."
        return True, "Keyring is available and unlocked."
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# SecretSource adapter
# ---------------------------------------------------------------------------


class LibsecretSource(SecretSource):
    """GNOME Keyring / libsecret as a registered secret source.

    Reads credentials from the system keyring via the Secret Service D-Bus API.
    No passkeys, no cloud, no extra daemon — just the keyring your desktop
    already manages.
    """

    name = "libsecret"
    label = "System Keyring (libsecret)"
    shape = "bulk"  # Injects all Hermes-labelled secrets from the keyring

    def is_enabled(self, cfg: dict) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("enabled"))

    def override_existing(self, cfg: dict) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("override_existing", True))

    def config_schema(self) -> dict:
        return {
            "enabled": {
                "description": "Master switch",
                "default": False,
            },
            "override_existing": {
                "description": "Keyring values overwrite .env/shell values",
                "default": True,
            },
        }

    def protected_env_vars(self, cfg: dict) -> frozenset:
        return frozenset()

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        locked = _collection_is_locked()
        if locked is None:
            result.error = (
                "Could not connect to the Secret Service (D-Bus). "
                "The keyring is only available in a desktop session."
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result
        if locked:
            result.error = (
                "Keyring is locked. Unlock it from your desktop session."
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        secrets = _list_all()
        if not secrets:
            result.error = (
                "No Hermes credentials found in the keyring. "
                "Add them with: hermes secrets keyring add KEY=value"
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        result.secrets = secrets
        return result
