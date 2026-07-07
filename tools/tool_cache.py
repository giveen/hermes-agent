"""
Tool result cache — diskcache-backed, with configurable TTL.

Caches results from idempotent read-only tools (read_file, search_files, grep, etc.)
across turns within a session. Each entry is keyed on (tool_name, hash of args).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Tools whose results are safe to cache (idempotent, no side effects)
_CACHEABLE_TOOLS = frozenset({
    "read_file",
    "search_files",
    "grep",
    "web_extract",
    "web_search",
    "browser_snapshot",
    "vision_analyze",
    "resolve_import_path",
    "get_current_directory",
})

# Default TTL in seconds.  Read tools return file contents / search results
# that are stable within a short window.
_DEFAULT_TTL_S = 60

# Max result size in bytes to cache (10 MB).  Larger results (e.g. full-file
# reads of huge binaries) would waste disk space for negligible replay benefit.
_MAX_RESULT_BYTES = 10 * 1024 * 1024

_CACHE: "diskcache.Cache | None" = None


def _get_cache() -> "diskcache.Cache | None":
    """Lazy-init the diskcache instance. Returns None on any error."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        import diskcache
    except ImportError:
        logger.debug("diskcache not available — tool caching disabled")
        _CACHE = False  # sentinel
        return None

    try:
        cache_dir = get_hermes_home() / "cache" / "tool_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _CACHE = diskcache.Cache(str(cache_dir))
        logger.debug("Tool cache initialized at %s", cache_dir)
        return _CACHE
    except Exception as exc:
        logger.debug("Failed to initialize tool cache: %s", exc)
        _CACHE = False  # sentinel
        return None


def _cache_key(tool_name: str, args: dict) -> str:
    """Build a deterministic cache key from tool name and arguments."""
    raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.sha256(raw.encode()).hexdigest()
    return f"{tool_name}:{h}"


def is_cacheable(tool_name: str) -> bool:
    """Return True if this tool is safe to cache."""
    return tool_name in _CACHEABLE_TOOLS


def get_cached_result(tool_name: str, args: dict) -> str | None:
    """Return cached result for (tool_name, args), or None on miss/error."""
    if not is_cacheable(tool_name):
        return None
    cache = _get_cache()
    if not cache:
        return None
    key = _cache_key(tool_name, args)
    try:
        result = cache.get(key)
        if result is not None:
            logger.debug("Tool cache HIT: %s", key[:60])
        return result
    except Exception as exc:
        logger.debug("Tool cache read error: %s", exc)
        return None


def set_cached_result(tool_name: str, args: dict, result: str, ttl: int = _DEFAULT_TTL_S) -> None:
    """Store a tool result in the cache, unless it's an error or too large."""
    if not is_cacheable(tool_name):
        return
    # Don't cache error results
    if result.startswith('{"error"') or result.startswith('{"success": false'):
        return
    if len(result) > _MAX_RESULT_BYTES:
        logger.debug("Tool cache SKIP (too large): %s (%d bytes)", tool_name, len(result))
        return
    cache = _get_cache()
    if not cache:
        return
    key = _cache_key(tool_name, args)
    try:
        cache.set(key, result, expire=ttl)
        logger.debug("Tool cache SET: %s (ttl=%ds)", key[:60], ttl)
    except Exception as exc:
        logger.debug("Tool cache write error: %s", exc)


# ── Question→answer response caching ─────────────────────────────────
# Caches direct LLM responses (no tool calls) keyed on normalized question
# text + a hash of the system context.  TTL is days-scale — if you ask
# "what is the capital of France" twice, the second hit returns instantly.

# Default TTL for response cache: 7 days
_RESPONSE_TTL_S = 7 * 24 * 3600


def _system_context_hash(agent: Any) -> str:
    """Build a hash of the agent's current system context.

    When skills, tools, model, or personality change, the hash changes
    and previously cached responses are invalidated.
    """
    parts = [
        str(getattr(agent, "model", "") or ""),
        str(getattr(agent, "provider", "") or ""),
        str(getattr(agent, "_cached_system_prompt", "") or ""),
    ]
    try:
        tools = sorted(getattr(agent, "tools", []) or [])
        parts.append(str(tools))
    except Exception:
        pass
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_cached_response(user_message: str, agent: Any = None) -> str | None:
    """Return a cached direct response for this question, or None.

    Matches on normalized question text + system context hash.
    Returns None on cache miss, error, or context mismatch.
    """
    cache = _get_cache()
    if not cache:
        return None
    question = user_message.strip().lower()
    if not question:
        return None
    ctx = _system_context_hash(agent) if agent is not None else ""
    key = f"response:{ctx}:{hashlib.sha256(question.encode()).hexdigest()}"
    try:
        result = cache.get(key)
        if result is not None:
            logger.debug("Response cache HIT: %s...", question[:50])
        return result
    except Exception as exc:
        logger.debug("Response cache read error: %s", exc)
        return None


def set_cached_response(user_message: str, response: str, agent: Any = None, *, ttl: int = _RESPONSE_TTL_S) -> None:
    """Cache a direct LLM response for this question.

    Only caches text-only responses (no tool calls).  Errors and empty
    responses are not cached.
    """
    if not response or response.startswith("I apologize") or len(response) < 5:
        return
    question = user_message.strip().lower()
    if not question:
        return
    cache = _get_cache()
    if not cache:
        return
    ctx = _system_context_hash(agent) if agent is not None else ""
    key = f"response:{ctx}:{hashlib.sha256(question.encode()).hexdigest()}"
    try:
        cache.set(key, response, expire=ttl)
        logger.debug("Response cache SET: %s... (ttl=%ds)", question[:50], ttl)
    except Exception as exc:
        logger.debug("Response cache write error: %s", exc)
