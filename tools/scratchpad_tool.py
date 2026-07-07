"""Scratchpad — shared workspace for parallel subagents.

Each scratchpad is a SQLite-backed append-only log that child agents
write findings into concurrently. Entries are keyed by section name
and tagged with the agent_id so nothing is ever overwritten — every
write is a new row.

Usage from delegate_task:
  1. Parent calls scratchpad_create(goal="research competitors")
     → gets scratchpad_id
  2. Parent passes scratchpad_id in context to all children
  3. Each child calls scratchpad_append(id, "pricing", "data...")
  4. Parent calls scratchpad_read(id) to collect everything
  5. Parent calls scratchpad_merge(id) for LLM synthesis
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_SCRATCHPAD_DIR = "scratchpads"
_DB_FILENAME = "scratchpads.db"
_DB_PATH: Optional[Path] = None
_local = threading.local()


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = get_hermes_home() / _SCRATCHPAD_DIR / _DB_FILENAME
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = _get_db_path()
        _local.conn = sqlite3.connect(str(db_path))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _init_schema(_local.conn)
    return _local.conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scratchpad_sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            goal TEXT DEFAULT '',
            parent_session_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            merged_markdown TEXT,
            merged_at TEXT
        );
        CREATE TABLE IF NOT EXISTS scratchpad_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES scratchpad_sessions(id),
            section TEXT NOT NULL,
            content TEXT NOT NULL,
            agent_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_scratchpad_entries_session
            ON scratchpad_entries(session_id, section);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _ensure_session_exists(session_id: str) -> None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT id FROM scratchpad_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Scratchpad session '{session_id}' not found. Create one with scratchpad_create first.")


def scratchpad_create(
    name: str,
    goal: str = "",
    parent_session_id: str = "",
) -> str:
    """Create a new scratchpad session for parallel subagents.

    Args:
        name: Human-readable name for this scratchpad (e.g. "competitor-research").
        goal: Optional description of what this scratchpad is for.
        parent_session_id: Optional Hermes session_id for traceability.

    Returns:
        JSON string with the new scratchpad session id.
    """
    session_id = f"sp-{uuid.uuid4().hex[:12]}"
    conn = _get_connection()
    conn.execute(
        "INSERT INTO scratchpad_sessions (id, name, goal, parent_session_id) VALUES (?, ?, ?, ?)",
        (session_id, name, goal, parent_session_id),
    )
    conn.commit()
    logger.info("Scratchpad created: %s (%s)", session_id, name)
    return json.dumps({"success": True, "session_id": session_id, "name": name})


def scratchpad_append(
    session_id: str,
    section: str,
    content: str,
    agent_id: str = "",
) -> str:
    """Append content to a section of a scratchpad.

    Every call adds a NEW row — existing content is never overwritten.
    Multiple subagents can safely append to the same section concurrently.

    Args:
        session_id: Scratchpad session id from scratchpad_create.
        section: Section name (e.g. "pricing", "features"). Case-sensitive.
        content: Text to append.
        agent_id: Optional identifier for which agent wrote this
            (auto-set to subagent task index when called from a child).

    Returns:
        JSON string confirming the append.
    """
    _ensure_session_exists(session_id)
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO scratchpad_entries (session_id, section, content, agent_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, section, content, agent_id, now),
    )
    conn.commit()
    return json.dumps({"success": True, "session_id": session_id, "section": section})


def scratchpad_read(
    session_id: str,
    section: Optional[str] = None,
    format: str = "json",
) -> str:
    """Read entries from a scratchpad.

    Args:
        session_id: Scratchpad session id.
        section: Optional section name to filter by. If omitted, returns all sections.
        format: Output format — "json" (default) or "markdown".

    Returns:
        JSON string with entries grouped by section.
        In "markdown" format, returns a formatted markdown document.
    """
    _ensure_session_exists(session_id)
    conn = _get_connection()

    if section:
        rows = conn.execute(
            "SELECT section, content, agent_id, created_at FROM scratchpad_entries "
            "WHERE session_id = ? AND section = ? ORDER BY id",
            (session_id, section),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT section, content, agent_id, created_at FROM scratchpad_entries "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

    if format == "markdown":
        return _format_as_markdown(session_id, rows)

    # Group by section
    sections: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        sec = row["section"]
        sections.setdefault(sec, []).append({
            "content": row["content"],
            "agent_id": row["agent_id"],
            "created_at": row["created_at"],
        })

    return json.dumps({
        "success": True,
        "session_id": session_id,
        "sections": sections,
        "total_entries": len(rows),
        "total_sections": len(sections),
    }, indent=2, ensure_ascii=False)


def scratchpad_merge(
    session_id: str,
    summarize: bool = False,
) -> str:
    """Finalize a scratchpad: mark it complete and optionally generate
    an LLM summary of all sections.

    The merged markdown combines every section into a structured document
    with agent attribution. When ``summarize=True``, an auxiliary LLM call
    generates a compact synthesis.

    Args:
        session_id: Scratchpad session id.
        summarize: If True, generate an LLM-generated executive summary
            (requires an auxiliary model configured).

    Returns:
        JSON string with the merged markdown and optional summary.
    """
    _ensure_session_exists(session_id)
    conn = _get_connection()

    # Read all entries
    rows = conn.execute(
        "SELECT section, content, agent_id, created_at FROM scratchpad_entries "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()

    merged = _format_as_markdown(session_id, rows)

    # Mark completed
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE scratchpad_sessions SET completed_at = ?, merged_markdown = ?, merged_at = ? "
        "WHERE id = ?",
        (now, merged, now, session_id),
    )
    conn.commit()

    result: Dict[str, Any] = {
        "success": True,
        "session_id": session_id,
        "merged_markdown": merged,
        "total_entries": len(rows),
        "total_sections": len(set(r["section"] for r in rows)),
    }

    if summarize:
        try:
            from agent.auxiliary_client import smart_llm
            prompt = (
                "You are a research synthesis assistant. Below is a scratchpad "
                "with findings from multiple research agents. Produce a concise "
                "executive summary (2-3 paragraphs) that synthesizes the key "
                "findings, highlights agreements and disagreements, and identifies "
                "open questions.\n\n"
                f"{merged}"
            )
            summary = smart_llm(prompt, max_tokens=2000)
            if summary and summary.strip():
                result["summary"] = summary.strip()
                conn.execute(
                    "UPDATE scratchpad_sessions SET summary = ? WHERE id = ?",
                    (summary.strip(), session_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Scratchpad LLM summary failed: %s", exc)
            result["summary_error"] = str(exc)

    return json.dumps(result, indent=2, ensure_ascii=False)


def _format_as_markdown(session_id: str, rows: sqlite3.Row | List[sqlite3.Row]) -> str:
    """Convert scratchpad entries to a structured markdown document."""
    conn = _get_connection()
    session = conn.execute(
        "SELECT name, goal FROM scratchpad_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    lines: List[str] = []
    name = session["name"] if session else session_id
    goal = session["goal"] if session else ""
    lines.append(f"# Scratchpad: {name}")
    if goal:
        lines.append(f"\n**Goal:** {goal}\n")

    # Group by section
    sections: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        sec = row["section"]
        sections.setdefault(sec, []).append({
            "content": row["content"],
            "agent_id": row["agent_id"],
        })

    for section_name, entries in sections.items():
        lines.append(f"\n## {section_name}\n")
        for entry in entries:
            agent_tag = f" *(by {entry['agent_id']})*" if entry.get("agent_id") else ""
            lines.append(f"{entry['content']}{agent_tag}\n")

    lines.append("\n---\n")
    now = datetime.now(timezone.utc).isoformat()
    lines.append(f"*Merged at {now}*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error

SCRATCHPAD_CREATE_SCHEMA = {
    "name": "scratchpad_create",
    "description": (
        "Create a new scratchpad session for parallel subagents to share "
        "findings. Returns a session_id that children use to append data. "
        "Use this when delegating parallel research or data-collection tasks, "
        "so each child can write its findings to a shared scratchpad without "
        "overwriting each other."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Human-readable name (e.g. 'competitor-research').",
            },
            "goal": {
                "type": "string",
                "description": "Optional description of what this scratchpad is for.",
            },
            "parent_session_id": {
                "type": "string",
                "description": "Optional Hermes session_id for traceability.",
            },
        },
        "required": ["name"],
    },
}

SCRATCHPAD_APPEND_SCHEMA = {
    "name": "scratchpad_append",
    "description": (
        "Append content to a section of a scratchpad. Every call adds a new "
        "row — nothing is ever overwritten. Multiple subagents can safely "
        "append to the same section at the same time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Scratchpad session id from scratchpad_create.",
            },
            "section": {
                "type": "string",
                "description": "Section name (e.g. 'pricing', 'features').",
            },
            "content": {
                "type": "string",
                "description": "Text to append.",
            },
            "agent_id": {
                "type": "string",
                "description": "Optional identifier for which agent wrote this.",
            },
        },
        "required": ["session_id", "section", "content"],
    },
}

SCRATCHPAD_READ_SCHEMA = {
    "name": "scratchpad_read",
    "description": (
        "Read entries from a scratchpad. Returns entries grouped by section. "
        "Use this in the parent agent to collect all findings from parallel "
        "subagents after they finish."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Scratchpad session id.",
            },
            "section": {
                "type": "string",
                "description": "Optional section name to filter by.",
            },
            "format": {
                "type": "string",
                "enum": ["json", "markdown"],
                "description": "Output format: 'json' (default) or 'markdown'.",
            },
        },
        "required": ["session_id"],
    },
}

SCRATCHPAD_MERGE_SCHEMA = {
    "name": "scratchpad_merge",
    "description": (
        "Finalize a scratchpad and get the merged markdown document. "
        "Optionally generates an LLM executive summary. Call this in the "
        "parent agent after all parallel subagents have finished writing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Scratchpad session id.",
            },
            "summarize": {
                "type": "boolean",
                "description": "If True, generate an LLM executive summary.",
            },
        },
        "required": ["session_id"],
    },
}


def _check_scratchpad_requirements() -> bool:
    return True


def _handle_scratchpad_create(args, **kw):
    return scratchpad_create(
        name=args.get("name", ""),
        goal=args.get("goal", ""),
        parent_session_id=args.get("parent_session_id", ""),
    )


def _handle_scratchpad_append(args, **kw):
    # Auto-set agent_id from task context if available
    agent_id = args.get("agent_id") or kw.get("task_id", "")
    return scratchpad_append(
        session_id=args.get("session_id", ""),
        section=args.get("section", ""),
        content=args.get("content", ""),
        agent_id=agent_id,
    )


def _handle_scratchpad_read(args, **kw):
    return scratchpad_read(
        session_id=args.get("session_id", ""),
        section=args.get("section"),
        format=args.get("format", "json"),
    )


def _handle_scratchpad_merge(args, **kw):
    return scratchpad_merge(
        session_id=args.get("session_id", ""),
        summarize=bool(args.get("summarize", False)),
    )


registry.register(
    name="scratchpad_create",
    toolset="scratchpad",
    schema=SCRATCHPAD_CREATE_SCHEMA,
    handler=_handle_scratchpad_create,
    check_fn=_check_scratchpad_requirements,
    emoji="📋",
)
registry.register(
    name="scratchpad_append",
    toolset="scratchpad",
    schema=SCRATCHPAD_APPEND_SCHEMA,
    handler=_handle_scratchpad_append,
    check_fn=_check_scratchpad_requirements,
    emoji="✏️",
)
registry.register(
    name="scratchpad_read",
    toolset="scratchpad",
    schema=SCRATCHPAD_READ_SCHEMA,
    handler=_handle_scratchpad_read,
    check_fn=_check_scratchpad_requirements,
    emoji="📖",
)
registry.register(
    name="scratchpad_merge",
    toolset="scratchpad",
    schema=SCRATCHPAD_MERGE_SCHEMA,
    handler=_handle_scratchpad_merge,
    check_fn=_check_scratchpad_requirements,
    emoji="🔗",
)
