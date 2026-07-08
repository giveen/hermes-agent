#!/usr/bin/env python3
"""
Keyring Tool — Store and retrieve credentials in the system keyring.

Uses the GNOME Keyring / libsecret D-Bus API so credentials survive reboots,
are unlocked automatically on desktop login, and are never written to disk
in plaintext (no .env file exposure).

Supports two credential shapes:
  - Simple env-var: ``name="OPENAI_API_KEY", value="sk-..."``
  - Website login: ``domain="google.com", username="user@example.com", password="hunter2"``
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

KEYRING_SCHEMA = {
    "name": "keyring",
    "description": (
        "Store, retrieve, and list credentials in the system keyring. "
        "Secrets are encrypted at rest by the OS keyring, unlocked automatically "
        "on desktop login, and never written to disk in plaintext.\n\n"
        "Actions:\n"
        '  - ``store``: save a simple env-var credential (e.g. API key)\n'
        '  - ``store_credential``: save a website login (username + password)\n'
        "  - ``get``: retrieve a credential by name\n"
        "  - ``get_credential``: retrieve a website login by domain\n"
        '  - ``list``: list all stored credentials\n\n'
        "Stored credentials are readable by Hermes at startup via the "
        "``libsecret`` secret source (enable with: hermes setup secrets)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["store", "store_credential", "get", "get_credential", "list"],
                "description": "What to do with the keyring.",
            },
            "name": {
                "type": "string",
                "description": "Environment-variable name for ``store`` / ``get`` actions (e.g. ``OPENAI_API_KEY``).",
            },
            "value": {
                "type": "string",
                "description": "Secret value for ``store`` action.",
            },
            "domain": {
                "type": "string",
                "description": "Domain for ``store_credential`` / ``get_credential`` actions (e.g. ``google.com``).",
            },
            "username": {
                "type": "string",
                "description": "Username for ``store_credential`` action.",
            },
            "password": {
                "type": "string",
                "description": "Password for ``store_credential`` action.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes for ``store_credential`` action.",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def check_keyring_requirements() -> bool:
    """Return True when the system keyring is reachable."""
    try:
        from agent.secret_sources.libsecret import _collection_is_locked

        locked = _collection_is_locked()
        return locked is not None
    except Exception:
        return False


def keyring_handler(args: dict, **kw: Any) -> str:
    """Dispatch keyring operations."""
    action = args.get("action", "")
    task_id = kw.get("task_id")

    try:
        from agent.secret_sources.libsecret import (
            store_env_var,
            lookup_env_var,
            _list_all,
            _lookup_secret,
        )

        if action == "store":
            name = args.get("name", "")
            value = args.get("value", "")
            if not name or not value:
                return json.dumps({"success": False, "error": "Both 'name' and 'value' are required."})
            if store_env_var(name, value):
                return json.dumps({"success": True, "message": f"Stored {name} in the system keyring."})
            return json.dumps({"success": False, "error": "Failed to store credential. Keyring may be locked."})

        elif action == "store_credential":
            domain = args.get("domain", "")
            username = args.get("username", "")
            password = args.get("password", "")
            notes = args.get("notes", "")
            if not domain or not username or not password:
                return json.dumps({"success": False, "error": "'domain', 'username', and 'password' are required."})

            # Store as a single JSON blob under a credential: key
            cred_key = f"credential:{domain}"
            cred_data = {"username": username, "password": password, "notes": notes}
            if store_env_var(cred_key, json.dumps(cred_data)):
                return json.dumps({
                    "success": True,
                    "message": f"Stored login for {domain} in the system keyring.",
                })
            return json.dumps({"success": False, "error": "Failed to store credential. Keyring may be locked."})

        elif action == "get":
            name = args.get("name", "")
            if not name:
                return json.dumps({"success": False, "error": "'name' is required."})
            value = lookup_env_var(name)
            if value is not None:
                return json.dumps({"success": True, "name": name, "value": value})
            return json.dumps({"success": False, "error": f"No credential found for '{name}'."})

        elif action == "get_credential":
            domain = args.get("domain", "")
            if not domain:
                return json.dumps({"success": False, "error": "'domain' is required."})
            cred_key = f"credential:{domain}"
            raw = _lookup_secret(cred_key)
            if raw:
                try:
                    data = json.loads(raw)
                    return json.dumps({
                        "success": True,
                        "domain": domain,
                        "username": data.get("username", ""),
                        "password": data.get("password", ""),
                        "notes": data.get("notes", ""),
                    })
                except json.JSONDecodeError:
                    return json.dumps({"success": True, "domain": domain, "raw": raw})
            return json.dumps({"success": False, "error": f"No login found for '{domain}'."})

        elif action == "list":
            all_s = _list_all()
            if all_s is None:
                return json.dumps({"success": False, "error": "Keyring is unavailable or locked."})

            simple = []
            credentials = []
            for key in sorted(all_s):
                if key.startswith("credential:"):
                    domain = key[len("credential:"):]
                    credentials.append(domain)
                else:
                    simple.append(key)

            result = {"success": True, "env_vars": simple, "credentials": credentials}
            return json.dumps(result)

        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

    except Exception as exc:
        logger.exception("keyring tool error")
        return json.dumps({"success": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="keyring",
    toolset="file",
    schema=KEYRING_SCHEMA,
    handler=keyring_handler,
    check_fn=check_keyring_requirements,
    emoji="🔑",
)
