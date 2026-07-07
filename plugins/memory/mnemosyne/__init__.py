"""Mnemosyne memory provider — zero-dependency, SQLite-backed memory.

Uses the ``mnemosyne`` SDK directly. Stores everything in a local SQLite
database under ``HERMES_HOME/mnemosyne/data/``. No external services.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from agent.memory_manager import sanitize_context
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ---------- tool schemas ----------

RECALL_SCHEMA = {
    "name": "mnemosyne_recall",
    "description": "Search memories by semantic similarity. Returns matching memories with relevance scores. Use to find past observations, user preferences, project facts, or anything previously stored.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what you want to find"
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (max 20)",
                "default": 5
            }
        },
        "required": ["query"]
    }
}

REMEMBER_SCHEMA = {
    "name": "mnemosyne_remember",
    "description": "Store an important piece of information into long-term memory. Use for facts about the user, project conventions, environment details, decisions made, or things you've learned that should persist across sessions.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The information to remember"
            },
            "importance": {
                "type": "number",
                "description": "Importance 0.0-1.0 (1.0 = most important, preserved longer)",
                "default": 0.5
            },
            "source": {
                "type": "string",
                "description": "Source context (e.g. 'conversation', 'tool_result', 'user_preference')",
                "default": "conversation"
            }
        },
        "required": ["content"]
    }
}

FORGET_SCHEMA = {
    "name": "mnemosyne_forget",
    "description": "Delete a specific memory by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "The ID of the memory to delete"
            }
        },
        "required": ["memory_id"]
    }
}

STATS_SCHEMA = {
    "name": "mnemosyne_stats",
    "description": "Get memory system statistics — total memories, storage usage, consolidation status.",
    "parameters": {
        "type": "object",
        "properties": {}
    }
}

ALL_TOOL_SCHEMAS = [RECALL_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA, STATS_SCHEMA]


# ---------- provider ----------

class MnemosyneMemoryProvider(MemoryProvider):
    """Mnemosyne — SQLite-backed local memory with semantic search."""

    def __init__(self) -> None:
        self._client: Any = None
        self._lock = threading.Lock()
        self._db_path: Path | None = None
        self._hermes_home: str = ""
        self._session_context: str = ""

    # ----- properties -----

    @property
    def name(self) -> str:
        return "mnemosyne"

    # ----- lifecycle -----

    def is_available(self) -> bool:
        try:
            import mnemosyne  # noqa: F401
            return True
        except ImportError:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        import mnemosyne as mn

        self._hermes_home = kwargs.get("hermes_home", "") or str(
            Path.home() / ".hermes"
        )
        data_dir = Path(self._hermes_home) / "mnemosyne" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = data_dir / "mnemosyne.db"

        self._session_context = session_id or "default"
        try:
            self._client = mn.Mnemosyne(
                session_id=self._session_context,
                db_path=self._db_path,
            )
            logger.info(
                "Mnemosyne initialized at %s (session=%s)",
                self._db_path, self._session_context,
            )
        except Exception as exc:
            logger.error("Mnemosyne init failed: %s", exc)
            raise

    def shutdown(self) -> None:
        with self._lock:
            self._client = None

    # ----- system prompt -----

    def system_prompt_block(self) -> str:
        return (
            "You have Mnemosyne long-term memory available. "
            "Use `mnemosyne_remember` to store important facts, "
            "`mnemosyne_recall` to search past memories, "
            "and `mnemosyne_forget` to remove outdated entries. "
            "Relevant memories are automatically injected before each turn."
        )

    # ----- prefetch / sync -----

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant memories before the turn."""
        if not self._client or not query.strip():
            return ""
        try:
            results = self._client.recall(query, top_k=5)
            if not results:
                return ""
            lines = ["[Relevant memories from Mnemosyne:]"]
            for r in results[:5]:
                content = r.get("content", r.get("text", ""))
                score = r.get("score", r.get("similarity", ""))
                if isinstance(score, (int, float)):
                    score = f"{score:.2f}"
                if content:
                    lines.append(f"  • {content}  (relevance: {score})")
            return "\n".join(lines)
        except Exception:
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store the turn exchange as a memory."""
        if not self._client:
            return
        content = f"User: {user_content}\nAssistant: {assistant_content}"
        try:
            self._client.remember(
                content=content[:2000],
                source="conversation",
                importance=0.3,
            )
        except Exception:
            pass

    # ----- tools -----

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(ALL_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return tool_error(tool_name, "Mnemosyne not initialized")

        try:
            if tool_name == "mnemosyne_recall":
                query = args.get("query", "")
                top_k = min(int(args.get("top_k", 5)), 20)
                results = self._client.recall(query, top_k=top_k)
                return json.dumps(
                    [{"id": r.get("id", r.get("memory_id", "")),
                      "content": r.get("content", r.get("text", "")),
                      "score": r.get("score", r.get("similarity", 0))}
                     for r in (results or [])],
                    ensure_ascii=False,
                )

            if tool_name == "mnemosyne_remember":
                content = args.get("content", "").strip()
                if not content:
                    return json.dumps({"error": "content is required"})
                importance = float(args.get("importance", 0.5))
                source = args.get("source", "conversation")
                mem_id = self._client.remember(
                    content=content,
                    importance=min(max(importance, 0.0), 1.0),
                    source=source,
                )
                return json.dumps({"memory_id": mem_id, "status": "stored"})

            if tool_name == "mnemosyne_forget":
                mem_id = args.get("memory_id", "")
                if not mem_id:
                    return json.dumps({"error": "memory_id is required"})
                ok = self._client.forget(mem_id)
                return json.dumps({"deleted": ok})

            if tool_name == "mnemosyne_stats":
                stats = self._client.get_stats()
                return json.dumps(stats if stats else {}, ensure_ascii=False)

        except Exception as exc:
            return tool_error(tool_name, str(exc))

        return tool_error(tool_name, f"Unknown tool: {tool_name}")


# ---------- plugin entry point ----------

def register(ctx) -> None:
    """Register Mnemosyne as a memory provider plugin."""
    ctx.register_memory_provider(MnemosyneMemoryProvider())
    logger.info("Mnemosyne memory provider registered")
