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
