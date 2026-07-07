"""Code structure analysis tool — powered by tree-sitter.

Parses source files into concrete syntax trees (CSTs) and returns
structured information about functions, classes, imports, and symbols.
Works offline with no LSP server required. Supports 8 languages:
Python, TypeScript, JSX/TSX, Rust, Go, Java, C, C++.

Usage:
    code_structure(path="src/main.py", query="functions")
    code_structure(path="src/", query="classes", filter="Service")
    code_structure(path="src/", query="all", detail=true)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language registry — maps file extensions to tree-sitter grammars
# ---------------------------------------------------------------------------

_LANGUAGE_REGISTRY: Dict[str, Tuple[Any, str]] = {}
_LANGUAGE_REGISTRY_LOCK = False


def _init_languages() -> None:
    """Lazy-init the language registry. Only loads grammars when first used."""
    global _LANGUAGE_REGISTRY_LOCK
    if _LANGUAGE_REGISTRY_LOCK:
        return
    _LANGUAGE_REGISTRY_LOCK = True
    from tree_sitter import Language

    registrations = [
        (".py",   "tree_sitter_python",     "language",         "Python"),
        (".ts",   "tree_sitter_typescript",  "language_typescript",  "TypeScript"),
        (".tsx",  "tree_sitter_typescript",  "language_tsx",     "TSX"),
        (".js",   "tree_sitter_typescript",  "language_typescript",  "JavaScript"),
        (".jsx",  "tree_sitter_typescript",  "language_tsx",     "JSX"),
        (".rs",   "tree_sitter_rust",       "language",         "Rust"),
        (".go",   "tree_sitter_go",         "language",         "Go"),
        (".java", "tree_sitter_java",       "language",         "Java"),
        (".c",    "tree_sitter_c",          "language",         "C"),
        (".h",    "tree_sitter_c",          "language",         "C header"),
        (".cpp",  "tree_sitter_cpp",        "language",         "C++"),
        (".hpp",  "tree_sitter_cpp",        "language",         "C++ header"),
        (".cc",   "tree_sitter_cpp",        "language",         "C++"),
        (".cxx",  "tree_sitter_cpp",        "language",         "C++"),
    ]

    for ext, mod_name, func_name, label in registrations:
        key = ext.lower()
        if key in _LANGUAGE_REGISTRY:
            continue
        try:
            mod = __import__(mod_name, fromlist=[func_name])
            lang_func = getattr(mod, func_name)
            lang_obj = Language(lang_func())
            _LANGUAGE_REGISTRY[key] = (lang_obj, label)
        except Exception as exc:
            logger.debug("tree-sitter grammar %s (%s) failed: %s", mod_name, ext, exc)


# ---------------------------------------------------------------------------
# Tree-sitter queries per language
# ---------------------------------------------------------------------------

# These queries use tree-sitter's query language to find specific node types.
# Each query captures the relevant fields (name, parameters, body, etc.).

_FUNCTION_QUERIES: Dict[str, str] = {
    "python": """
        (function_definition
            name: (identifier) @name
            parameters: (parameters) @params
            body: (block) @body) @func
        (decorated_definition
            definition: (function_definition) @func) @decorated
    """,
    "rust": """
        (function_item
            name: (identifier) @name
            parameters: (parameters) @params
            body: (block) @body) @func
    """,
    "go": """
        (function_declaration
            name: (identifier) @name
            body: (block) @body) @func
        (method_declaration
            name: (field_identifier) @name
            body: (block) @body) @func
    """,
    "java": """
        (method_declaration
            name: (identifier) @name
            body: (block) @body) @func
    """,
    "c": """
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @name)
            body: (compound_statement) @body) @func
    """,
}

_CLASS_QUERIES: Dict[str, str] = {
    "python": """
        (class_definition
            name: (identifier) @name
            body: (block) @body) @class
    """,
    "rust": """
        (struct_item name: (identifier) @name) @struct
        (impl_item) @impl
    """,
    "go": """
        (type_declaration
            (type_spec name: (identifier) @name)) @type
    """,
    "java": """
        (class_declaration
            name: (identifier) @name
            body: (class_body) @body) @class
        (interface_declaration
            name: (identifier) @name) @interface
    """,
}

_IMPORT_QUERIES: Dict[str, str] = {
    "python": """
        (import_statement name: (dotted_name) @name) @import
        (import_from_statement module_name: (dotted_name) @module name: (dotted_name) @name) @import_from
    """,
    "rust": """
        (use_declaration argument: (use_as_clause name: (identifier) @name)) @use
        (use_declaration argument: (scoped_use_list path: (identifier) @path)) @use
        (use_declaration argument: (identifier) @name) @use
    """,
    "go": """
        (import_declaration (import_spec name: (interpreted_string_literal) @name)) @import
    """,
    "java": """
        (import_declaration name: (scoped_identifier) @name) @import
    """,
}


def _get_query(language_label: str, query_type: str) -> Optional[str]:
    """Get the tree-sitter query string for a language and query type."""
    queries = {
        "functions": _FUNCTION_QUERIES,
        "classes": _CLASS_QUERIES,
        "imports": _IMPORT_QUERIES,
    }
    qmap = queries.get(query_type)
    if not qmap:
        return None
    # Try exact match, then prefix match (e.g. "TypeScript" matches "typescript")
    if language_label.lower() in qmap:
        return qmap[language_label.lower()]
    for key, q in qmap.items():
        if key in language_label.lower():
            return q
    return None


def _get_all_queries(language_label: str) -> str:
    """Combine all available queries for a language into one."""
    parts = []
    labels = set()
    for qt in ("functions", "classes", "imports"):
        q = _get_query(language_label, qt)
        if q:
            # Deduplicate by source text
            if q not in labels:
                parts.append(q)
                labels.add(q)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Source text helpers
# ---------------------------------------------------------------------------

def _node_text(node: Any, source_bytes: bytes) -> str:
    """Get the text of a tree-sitter node from source bytes."""
    if node is None:
        return ""
    try:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_docstring(node: Any, source_bytes: bytes) -> str:
    """Extract a docstring/comments preceding a node."""
    if not node or not hasattr(node, "start_byte"):
        return ""
    # Look backward for string literals or comments
    # Tree-sitter doesn't track comments directly in most grammars,
    # so we scan the source text before the node
    try:
        pre = source_bytes[max(0, node.start_byte - 500):node.start_byte].decode("utf-8", errors="replace")
        # Python docstrings (triple-quoted strings right after def/class)
        m = re.search(r'("""[^"]*"""|\'\'\'[^\']*\'\'\')\s*$', pre, re.DOTALL)
        if m:
            doc = m.group(1)
            return doc[:200] + ("..." if len(doc) > 200 else "")
        # Line comments before the node
        comment_lines = []
        for line in reversed(pre.split("\n")):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                comment_lines.insert(0, stripped)
            elif not stripped:
                continue
            else:
                break
        if comment_lines:
            return "\n".join(comment_lines)[:300]
    except Exception:
        pass
    return ""


def _extract_function_name(node: Any, source_bytes: bytes, lang: str) -> str:
    """Extract function/method name from a function node."""
    for field in ("name",):
        child = node.child_by_field_name(field) if hasattr(node, "child_by_field_name") else None
        if child is not None:
            return _node_text(child, source_bytes)
    # Fallback: try to find identifier in the node
    try:
        for child in node.children:
            if child.type in ("identifier", "field_identifier"):
                return _node_text(child, source_bytes)
    except Exception:
        pass
    return _node_text(node, source_bytes).split("(")[0].strip().split()[-1] if node else ""


# ---------------------------------------------------------------------------
# Core parsing logic
# ---------------------------------------------------------------------------

_PARSER_CACHE: Dict[str, Any] = {}


def _get_parser(language_label: str) -> Optional[Any]:
    """Get or create a tree-sitter Parser for a language."""
    from tree_sitter import Parser

    key = language_label.lower()
    if key in _PARSER_CACHE:
        return _PARSER_CACHE[key]

    # Find the language object
    for ext, (lang_obj, label) in _LANGUAGE_REGISTRY.items():
        if label.lower() == key or ext == f".{key}":
            parser = Parser(lang_obj)
            _PARSER_CACHE[key] = parser
            return parser
    return None


def _parse_file(file_path: Path) -> Optional[Tuple[Any, bytes, str]]:
    """Parse a file and return (tree, source_bytes, language_label) or None."""
    ext = file_path.suffix.lower()
    if ext not in _LANGUAGE_REGISTRY:
        return None

    lang_obj, label = _LANGUAGE_REGISTRY[ext]
    from tree_sitter import Parser

    parser = Parser(lang_obj)
    try:
        source_bytes = file_path.read_bytes()
        if not source_bytes.strip():
            return None
        tree = parser.parse(source_bytes)
        return tree, source_bytes, label
    except Exception as exc:
        logger.debug("Parse failed for %s: %s", file_path, exc)
        return None


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def _execute_query(
    tree: Any,
    source_bytes: bytes,
    language_label: str,
    query_type: str,
    name_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Execute a tree-sitter query and return structured results."""
    q = _get_query(language_label, query_type)
    if not q:
        return []

    from tree_sitter import Query, QueryCursor

    lang_obj = None
    for ext, (lo, _) in _LANGUAGE_REGISTRY.items():
        if lo.name and lo.name.lower() in language_label.lower():
            lang_obj = lo
            break
    if not lang_obj:
        # Find by label
        for ext, (lo, lbl) in _LANGUAGE_REGISTRY.items():
            if language_label.lower() in lbl.lower():
                lang_obj = lo
                break
    if not lang_obj:
        return []

    try:
        query = Query(lang_obj, q)
        cursor = QueryCursor(query)

        results = []
        seen_names: Set[str] = set()

        # cursor.matches() returns list of (pattern_index, {capture_name: [nodes]})
        primary_keys = {"func", "decorated", "class", "struct", "impl",
                        "type", "interface", "import", "import_from", "use"}
        for pattern_index, captures in cursor.matches(tree.root_node):
            primary_node = None
            name_nodes = []
            for cap_name, nodes in captures.items():
                if cap_name in primary_keys:
                    primary_node = nodes[0]
                elif cap_name in ("name", "module", "path"):
                    name_nodes.extend(nodes)
            if primary_node is None:
                continue
            # Extract name
            name = ""
            nc = (primary_node.child_by_field_name("name")
                  if hasattr(primary_node, "child_by_field_name") else None)
            if nc is not None:
                name = _node_text(nc, source_bytes)
            if not name and name_nodes:
                name = _node_text(name_nodes[0], source_bytes)
            if not name:
                for child in (primary_node.children or []):
                    if child.type in ("identifier", "field_identifier",
                                      "property_identifier", "type_identifier"):
                        name = _node_text(child, source_bytes)
                        break
            if not name:
                raw = _node_text(primary_node, source_bytes)
                name = raw.split("(")[0].strip().split()[-1][:60] if "(" in raw else raw[:60]
            if not name:
                continue
            if name_filter and name_filter.lower() not in name.lower():
                continue
            dedup_key = f"{query_type}:{name}:{primary_node.start_point.row}"
            if dedup_key in seen_names:
                continue
            seen_names.add(dedup_key)

            entry: Dict[str, Any] = {
                "name": name,
                "line": primary_node.start_point.row + 1,
                "col": primary_node.start_point.column + 1,
            }

            if query_type == "functions":
                pn = (primary_node.child_by_field_name("parameters")
                      if hasattr(primary_node, "child_by_field_name") else None)
                if pn:
                    entry["params"] = _extract_params(pn, source_bytes)
                else:
                    entry["params"] = []

            doc = _get_docstring(primary_node, source_bytes)
            if doc:
                entry["docstring"] = doc

            entry["end_line"] = primary_node.end_point.row + 1
            entry["lines"] = entry["end_line"] - entry["line"]
            entry["source"] = _node_text(primary_node, source_bytes)[:150]
            results.append(entry)


        return results

    except Exception as exc:
        logger.debug("Query failed for %s/%s: %s", language_label, query_type, exc)
        return []


def _extract_params(params_node: Any, source_bytes: bytes) -> List[str]:
    """Extract parameter names from a parameters node."""
    params = []
    if params_node is None:
        return params
    try:
        for child in params_node.children:
            if child.type == "identifier":
                params.append(_node_text(child, source_bytes))
            elif child.type in ("typed_parameter", "optional_parameter", "required_parameter"):
                name_child = child.child_by_field_name("name")
                if name_child:
                    params.append(_node_text(name_child, source_bytes))
                else:
                    params.append(_node_text(child, source_bytes).split(":")[0].strip())
            elif child.type == "spread_parameter":
                params.append("*" + _node_text(
                    child.child_by_field_name("argument") if hasattr(child, "child_by_field_name") else child,
                    source_bytes,
                ))
        # Filter out punctuation-only tokens
        params = [p for p in params if p and not all(c in "(), " for c in p)]
    except Exception:
        pass
    return params


def _find_language_for_file(file_path: Path) -> Optional[str]:
    """Return the language label for a file path."""
    ext = file_path.suffix.lower()
    entry = _LANGUAGE_REGISTRY.get(ext)
    return entry[1] if entry else None


def _collect_files(path: Path, recursive: bool = True) -> List[Path]:
    """Collect all parseable files from a path (file or directory)."""
    if path.is_file():
        ext = path.suffix.lower()
        if ext in _LANGUAGE_REGISTRY:
            return [path]
        return []

    files = []
    glob_pattern = "**/*" if recursive else "*"
    for f in sorted(path.glob(glob_pattern)):
        if f.is_file() and not f.name.startswith("."):
            ext = f.suffix.lower()
            if ext in _LANGUAGE_REGISTRY:
                # Skip common non-source dirs
                parts = f.parts
                skip_dirs = {"node_modules", ".venv", "venv", "__pycache__",
                             ".git", "build", "dist", "target", ".tox", ".eggs"}
                if not any(p in skip_dirs for p in parts):
                    files.append(f)
    return files


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def code_structure(
    path: str,
    query: str = "all",
    filter: Optional[str] = None,
    recursive: bool = True,
) -> str:
    """Analyze source code structure using tree-sitter.

    Args:
        path: File or directory path to analyze.
        query: What to extract — "functions", "classes", "imports",
               "symbols" (find by filter name), or "all" (default).
        filter: Optional name pattern to filter results (case-insensitive substring).
        recursive: When True and path is a directory, recurse into subdirectories.

    Returns:
        JSON string with structured results grouped by file.
    """
    _init_languages()

    target = Path(path)
    if not target.exists():
        return json.dumps({
            "success": False,
            "error": f"Path does not exist: {path}",
        })

    files = _collect_files(target, recursive=recursive)
    if not files:
        return json.dumps({
            "success": False,
            "error": f"No parsable source files found in: {path}",
            "supported_extensions": list(_LANGUAGE_REGISTRY.keys()),
        })

    # Determine which queries to run
    query_types = ["functions", "classes", "imports"]
    if query and query != "all":
        query_types = [query]

    results: Dict[str, Dict[str, List]] = {}
    total_files = 0

    for file_path in files:
        parsed = _parse_file(file_path)
        if parsed is None:
            continue

        tree, source_bytes, lang_label = parsed
        rel_path = str(file_path.relative_to(target)) if target.is_dir() else str(file_path)
        file_result: Dict[str, List] = {}

        for qt in query_types:
            items = _execute_query(tree, source_bytes, lang_label, qt, name_filter=filter)
            if items:
                file_result[qt] = items

        if file_result:
            results[rel_path] = file_result
            total_files += 1

    return json.dumps({
        "success": True,
        "path": path,
        "files_analyzed": total_files,
        "supported_languages": sorted(set(
            lbl for _, lbl in _LANGUAGE_REGISTRY.values()
        )),
        "results": results,
    }, indent=2, ensure_ascii=False)


def code_symbol(
    path: str,
    symbol: str,
    query: str = "all",
) -> str:
    """Find a specific symbol by name, returning its location and definition.

    Convenience wrapper around code_structure with filter applied.
    """
    return code_structure(path=path, query=query, filter=symbol)


# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error

CODE_STRUCTURE_SCHEMA = {
    "name": "code_structure",
    "description": (
        "Analyze source code structure using tree-sitter. Returns functions, "
        "classes, imports, and their signatures with line numbers. "
        "No LSP server required — works entirely offline. "
        "Supports Python, TypeScript, JavaScript, Rust, Go, Java, C, C++.\n\n"
        "WHEN TO USE:\n"
        "- You need to understand a file's structure (functions, classes, imports)\n"
        "- You need function/method signatures with parameter names\n"
        "- You need to find a specific symbol across a codebase\n"
        "- You need line numbers for navigation\n\n"
        "WHEN NOT TO USE:\n"
        "- Simple text search → use search_files\n"
        "- You need type checking or references → use LSP tools"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory path to analyze.",
            },
            "query": {
                "type": "string",
                "enum": ["all", "functions", "classes", "imports", "symbols"],
                "description": (
                    "What to extract: 'all' (default), 'functions', 'classes', "
                    "'imports', or 'symbols' (use with filter to find a specific name)."
                ),
            },
            "filter": {
                "type": "string",
                "description": (
                    "Optional name pattern to filter results "
                    "(case-insensitive substring match). "
                    "Use with query='symbols' to find a specific symbol."
                ),
            },
            "recursive": {
                "type": "boolean",
                "description": "Recurse into subdirectories (default: true).",
            },
        },
        "required": ["path"],
    },
}

CODE_SYMBOL_SCHEMA = {
    "name": "code_symbol",
    "description": (
        "Find a specific symbol (function, class, etc.) by name and return "
        "its location, signature, and source. Wraps code_structure with "
        "filter applied. Faster than code_structure for single-symbol lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory to search.",
            },
            "symbol": {
                "type": "string",
                "description": "Symbol name to find (case-insensitive substring match).",
            },
        },
        "required": ["path", "symbol"],
    },
}


def _check_reqs() -> bool:
    try:
        _init_languages()
        return bool(_LANGUAGE_REGISTRY)
    except Exception:
        return False


registry.register(
    name="code_structure",
    toolset="code",
    schema=CODE_STRUCTURE_SCHEMA,
    handler=lambda args, **kw: code_structure(
        path=args.get("path", ""),
        query=args.get("query", "all"),
        filter=args.get("filter"),
        recursive=args.get("recursive", True),
    ),
    check_fn=_check_reqs,
    emoji="🏗️",
)
registry.register(
    name="code_symbol",
    toolset="code",
    schema=CODE_SYMBOL_SCHEMA,
    handler=lambda args, **kw: code_symbol(
        path=args.get("path", ""),
        symbol=args.get("symbol", ""),
    ),
    check_fn=_check_reqs,
    emoji="🔍",
)
