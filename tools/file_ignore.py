"""
.llmignore support — vendor-neutral AI ignore files.

Respects the `.llmignore` spec (https://rival.tips/llmignore) by providing
cascading directory-level ignore checks for file access tools.  Syntax is
identical to `.gitignore` (fnmatch/glob).

Usage::

    from tools.file_ignore import is_llmignored

    if is_llmignored("/path/to/project/.env"):
        print("This file should not be sent to an LLM")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet, List, Optional

logger = logging.getLogger(__name__)

# Cache of parsed patterns keyed by directory path.
# Cleared when ``clear_llmignore_cache()`` is called.
_llmignore_cache: dict[Path, list] = {}

_LLMIGNORE_FILENAME = ".llmignore"


def _find_llmignore_files(path: Path) -> List[Path]:
    """Walk up from *path* and collect every .llmignore found.

    Stops at the filesystem root or mount boundary, returning root-first
    (outermost first) so patterns can be evaluated in cascading order.
    """
    files: List[Path] = []
    try:
        path = path.resolve()
    except OSError:
        return files

    # Collect from ancestors root-first
    ancestors: List[Path] = []
    for parent in path.parents:
        ancestors.append(parent)
    # Also check the path itself if it's a directory
    if path.is_dir():
        ancestors.insert(0, path)
    ancestors.reverse()  # root → closest

    seen: set[Path] = set()
    for parent in ancestors:
        ignore_file = parent / _LLMIGNORE_FILENAME
        try:
            if ignore_file.is_file() and ignore_file not in seen:
                files.append(ignore_file)
                seen.add(ignore_file)
        except OSError:
            pass

    return files  # outermost first


def _parse_llmignore_file(path: Path) -> list:
    """Parse a single .llmignore file, caching the result."""
    if path in _llmignore_cache:
        return _llmignore_cache[path]

    try:
        from llmignore import parse

        content = path.read_text(encoding="utf-8", errors="replace")
        patterns = parse(content)
        _llmignore_cache[path] = patterns
        return patterns
    except Exception as exc:
        logger.debug("Failed to parse %s: %s", path, exc)
        _llmignore_cache[path] = []
        return []


def _load_cascading_patterns(path: Path) -> list:
    """Load all .llmignore patterns from *path* upward, in cascading order.

    Returns a flat list of Pattern objects from outermost .llmignore first,
    so ``match()`` last-match-wins semantics work correctly: deeper files
    (closer to *path*) come later in the list.
    """
    all_patterns: list = []
    for ignore_file in _find_llmignore_files(path):
        all_patterns.extend(_parse_llmignore_file(ignore_file))
    return all_patterns


def is_llmignored(filepath: str | Path) -> bool:
    """Return True if *filepath* matches any .llmignore cascade.

    Walks up from the file's parent directory to find all .llmignore files,
    parses them in cascading order (root → closest), and checks whether
    the relative path (relative to each .llmignore's directory) is excluded.

    The last matching pattern wins, so a negation in a deeper .llmignore
    can re-include a file excluded by a parent.
    """
    try:
        path = Path(filepath).resolve()
    except OSError:
        return False

    try:
        from llmignore import match
    except ImportError:
        return False


    for ignore_file in _find_llmignore_files(path):
        patterns = _parse_llmignore_file(ignore_file)
        if not patterns:
            continue
        # File path relative to the .llmignore's parent directory
        try:
            rel = path.relative_to(ignore_file.parent)
        except ValueError:
            # File is not under this .llmignore's directory — skip
            continue
        rel_str = str(rel.as_posix())
        if match(patterns, rel_str):
            return True

    return False


def filter_paths(paths: List[str | Path]) -> List[str | Path]:
    """Return only the paths that are NOT ignored by any .llmignore cascade."""
    return [p for p in paths if not is_llmignored(p)]


def clear_llmignore_cache() -> None:
    """Clear the pattern cache (e.g. after writing a new .llmignore file)."""
    _llmignore_cache.clear()
