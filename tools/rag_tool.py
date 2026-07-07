"""RAG tool — ingest documents and query them semantically.

Uses FastEmbed (ONNX-based, lightweight embedding) for local semantic search.
No external API calls, no GPU required, no vector database server needed.
Stores everything in SQLite with numpy array embeddings.

Tools:
  rag_ingest(path, recursive, chunk_size)  — ingest documents
  rag_query(query, top_k)                  — semantic search
  rag_list_sources()                       — show ingested sources
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RAG_DIR = "rag"
_DB_FILENAME = "rag_store.db"
_DEFAULT_CHUNK_SIZE = 512  # characters (~128 tokens)
_DEFAULT_CHUNK_OVERLAP = 64
_DEFAULT_TOP_K = 5
_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"  # 384-dim, ~5ms on CPU

# Supported file extensions for ingestion
_SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".kt", ".scala", ".sql", ".sh",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".xml", ".html", ".css", ".scss",
    ".log", ".csv", ".tsv",
})

# Directories to skip
_SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "build", "dist", "target", ".tox", ".eggs", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".hypothesis",
})

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded singleton)
# ---------------------------------------------------------------------------

_local = threading.local()


def _get_embedding_model():
    """Get or create the FastEmbed model (thread-local)."""
    if not hasattr(_local, "model") or _local.model is None:
        from fastembed import TextEmbedding
        _local.model = TextEmbedding(model_name=_EMBEDDING_MODEL)
    return _local.model


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_DB_PATH: Optional[Path] = None
_db_lock = threading.Lock()


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        from hermes_constants import get_hermes_home
        _DB_PATH = get_hermes_home() / _RAG_DIR / _DB_FILENAME
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rag_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            file_size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES rag_sources(id),
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_source
            ON rag_chunks(source_id);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


def _chunk_text(text: str, chunk_size: int = _DEFAULT_CHUNK_SIZE,
                overlap: int = _DEFAULT_CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries.

    Strategy:
    1. Split on double newlines (paragraphs)
    2. Merge small paragraphs into chunks up to chunk_size
    3. If a paragraph is larger than chunk_size, split on sentences
    """
    if not text.strip():
        return []

    # Normalize line endings
    text = text.replace("\r\n", "\n")

    # Split into paragraphs
    paragraphs = re.split(r"\n\n+", text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: List[str] = []
    current_chunk: List[str] = []
    current_len = 0

    def _flush():
        nonlocal current_chunk, current_len
        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            if overlap > 0 and chunks:
                # Add overlap from the end of the previous chunk
                prev = chunks[-1]
                overlap_text = prev[-overlap:] if len(prev) > overlap else prev
                chunk_text = overlap_text + "\n\n" + chunk_text
            chunks.append(chunk_text)
            current_chunk = []
            current_len = 0

    for para in paragraphs:
        if len(para) <= chunk_size:
            if current_len + len(para) + 2 <= chunk_size:
                current_chunk.append(para)
                current_len += len(para) + 2
            else:
                _flush()
                current_chunk = [para]
                current_len = len(para)
        else:
            _flush()
            # Split large paragraph on sentences
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                if current_len + len(sent) + 1 <= chunk_size:
                    current_chunk.append(sent)
                    current_len += len(sent) + 1
                else:
                    _flush()
                    current_chunk = [sent]
                    current_len = len(sent)
            _flush()

    _flush()
    return chunks


# ---------------------------------------------------------------------------
# Core ingestion
# ---------------------------------------------------------------------------


def _should_skip_path(path: Path) -> bool:
    """Check if a path should be skipped."""
    if any(p in _SKIP_DIRS for p in path.parts):
        return True
    if path.name.startswith("."):
        return True
    return False


def _collect_files(path: Path, recursive: bool = True) -> List[Path]:
    """Collect all ingestible files from a path."""
    if path.is_file():
        if path.suffix.lower() in _SUPPORTED_EXTENSIONS:
            return [path]
        return []

    pattern = "**/*" if recursive else "*"
    files: List[Path] = []
    for f in sorted(path.glob(pattern)):
        if f.is_file() and f.suffix.lower() in _SUPPORTED_EXTENSIONS:
            if not _should_skip_path(f):
                files.append(f)
    return files


def _needs_reingest(conn: sqlite3.Connection, file_path: str,
                    file_size: int, mtime: float) -> bool:
    """Check if a file has changed since last ingest."""
    row = conn.execute(
        "SELECT file_size, mtime FROM rag_sources WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    if row is None:
        return True
    return row["file_size"] != file_size or abs(row["mtime"] - mtime) > 0.001


def _remove_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM rag_chunks WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM rag_sources WHERE id = ?", (source_id,))


def ingest_file(file_path: Path, chunk_size: int = _DEFAULT_CHUNK_SIZE,
                chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP) -> Dict[str, Any]:
    """Ingest a single file into the RAG store."""
    if file_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return {"file": str(file_path), "status": "skipped",
                "reason": f"unsupported extension: {file_path.suffix}"}

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"file": str(file_path), "status": "error", "reason": str(exc)}

    if not text.strip():
        return {"file": str(file_path), "status": "skipped", "reason": "empty file"}

    # Chunk
    chunks = _chunk_text(text, chunk_size=chunk_size, overlap=chunk_overlap)
    if not chunks:
        return {"file": str(file_path), "status": "skipped", "reason": "no chunks"}

    # Generate embeddings
    model = _get_embedding_model()
    embeddings = list(model.embed(chunks))

    # Store
    conn = _get_connection()
    try:
        stat = file_path.stat()
        file_size = stat.st_size
        mtime = stat.st_mtime

        # Check if re-ingest needed
        rel_path = str(file_path)
        if _needs_reingest(conn, rel_path, file_size, mtime):
            existing = conn.execute(
                "SELECT id FROM rag_sources WHERE file_path = ?", (rel_path,)
            ).fetchone()
            if existing:
                _remove_source(conn, existing["id"])

            cursor = conn.execute(
                "INSERT INTO rag_sources (file_path, file_size, mtime, chunk_count) "
                "VALUES (?, ?, ?, ?)",
                (rel_path, file_size, mtime, len(chunks)),
            )
            source_id = cursor.lastrowid

            for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                emb_blob = emb.tobytes()
                conn.execute(
                    "INSERT INTO rag_chunks (source_id, chunk_index, text, embedding) "
                    "VALUES (?, ?, ?, ?)",
                    (source_id, idx, chunk, emb_blob),
                )
            conn.commit()
            return {"file": rel_path, "status": "ingested",
                    "chunks": len(chunks), "bytes": file_size}
        else:
            return {"file": rel_path, "status": "unchanged",
                    "chunks": (conn.execute(
                        "SELECT chunk_count FROM rag_sources WHERE file_path = ?",
                        (rel_path,)).fetchone() or [0])[0]}
    except Exception as exc:
        conn.rollback()
        logger.warning("Failed to ingest %s: %s", file_path, exc)
        return {"file": str(file_path), "status": "error", "reason": str(exc)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def search_query(query: str, top_k: int = _DEFAULT_TOP_K) -> List[Dict[str, Any]]:
    """Search ingested documents semantically.

    Embeds the query, computes cosine similarity against all stored chunks,
    returns the top-k most relevant results.
    """
    model = _get_embedding_model()
    query_emb = list(model.embed([query]))[0]

    conn = _get_connection()
    try:
        # Load all chunks
        rows = conn.execute("""
            SELECT c.id, c.text, c.chunk_index, c.embedding, s.file_path
            FROM rag_chunks c
            JOIN rag_sources s ON c.source_id = s.id
            WHERE c.embedding IS NOT NULL
            ORDER BY c.id
        """).fetchall()
        # Compute similarities
        results: List[Dict[str, Any]] = []
        for row in rows:
            emb = np.frombuffer(row["embedding"], dtype=np.float32)
            score = _cosine_similarity(query_emb, emb)
            results.append({
                "score": round(score, 4),
                "text": row["text"][:500],
                "source": row["file_path"],
                "chunk_index": row["chunk_index"],
                "chunk_id": row["id"],
            })

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rag_ingest(
    path: str,
    recursive: bool = True,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> str:
    """Ingest documents from a file or directory into the RAG store.

    Parses the file content, splits into chunks, generates embeddings
    via FastEmbed (local, CPU, no API calls), and stores in SQLite.

    Args:
        path: File or directory path to ingest.
        recursive: When True and path is a directory, recurse into subdirectories.
        chunk_size: Target chunk size in characters (default: 512 ~128 tokens).
        chunk_overlap: Overlap between chunks in characters (default: 64).

    Returns:
        JSON string with ingestion results.
    """
    target = Path(path)
    if not target.exists():
        return json.dumps({"success": False, "error": f"Path does not exist: {path}"})

    files = _collect_files(target, recursive=recursive)
    if not files:
        return json.dumps({
            "success": False,
            "error": f"No supported files found in: {path}",
            "supported_extensions": sorted(_SUPPORTED_EXTENSIONS),
        })

    results = []
    total_chunks = 0
    total_bytes = 0
    errors = 0

    for file_path in files:
        result = ingest_file(file_path, chunk_size=chunk_size,
                             chunk_overlap=chunk_overlap)
        results.append(result)
        if result.get("status") == "ingested":
            total_chunks += result.get("chunks", 0)
            total_bytes += result.get("bytes", 0)
        elif result.get("status") == "error":
            errors += 1

    return json.dumps({
        "success": True,
        "files_found": len(files),
        "files_ingested": sum(1 for r in results if r.get("status") == "ingested"),
        "files_unchanged": sum(1 for r in results if r.get("status") == "unchanged"),
        "files_skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "files_errors": errors,
        "total_chunks": total_chunks,
        "total_bytes": total_bytes,
        "details": results,
    }, indent=2, ensure_ascii=False)


def rag_query(query: str, top_k: int = _DEFAULT_TOP_K) -> str:
    """Search ingested documents semantically.

    Embeds the query using FastEmbed and returns the most relevant
    document chunks with similarity scores.

    Args:
        query: Natural language query.
        top_k: Number of results to return (default: 5, max: 50).

    Returns:
        JSON string with ranked results.
    """
    if not query or not query.strip():
        return json.dumps({"success": False, "error": "Query is empty"})

    top_k = max(1, min(top_k, 50))
    results = search_query(query.strip(), top_k=top_k)

    if not results:
        return json.dumps({
            "success": True,
            "query": query,
            "results": [],
            "total_results": 0,
            "hint": "No documents found. Ingest some files first with rag_ingest.",
        })

    return json.dumps({
        "success": True,
        "query": query,
        "results": results,
        "total_results": len(results),
    }, indent=2, ensure_ascii=False)


def rag_list_sources() -> str:
    """List all ingested document sources with metadata."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT file_path, file_size, chunk_count, ingested_at "
            "FROM rag_sources ORDER BY ingested_at DESC"
        ).fetchall()

        sources = [
            {
                "file": r["file_path"],
                "size": r["file_size"],
                "chunks": r["chunk_count"],
                "ingested_at": r["ingested_at"],
            }
            for r in rows
        ]

        total_chunks = conn.execute(
            "SELECT COUNT(*) as c FROM rag_chunks"
        ).fetchone()["c"]

        return json.dumps({
            "success": True,
            "total_sources": len(sources),
            "total_chunks": total_chunks,
            "sources": sources,
        }, indent=2, ensure_ascii=False)
    finally:
        conn.close()


def rag_ingest_pdf(
    path: str,
    ocr: bool = True,
    dpi: int = 300,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> str:
    """Extract text from a PDF and ingest into the RAG store.

    Uses PyMuPDF for text extraction. When ``ocr=True`` and a page has
    sparse text (likely scanned), renders the page as an image and OCRs
    it via Tesseract. The extracted text is chunked and embedded just
    like any other ingested document.

    Args:
        path: Path to the PDF file.
        ocr: When True, OCR pages with sparse text (requires tesseract).
        dpi: DPI for page rendering when OCR is needed (higher = better OCR).
        chunk_size: Target chunk size in characters (default: 512).
        chunk_overlap: Overlap between chunks (default: 64).

    Returns:
        JSON string with ingestion results.
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        return json.dumps({"success": False, "error": f"File not found: {path}"})
    if pdf_path.suffix.lower() != ".pdf":
        return json.dumps({"success": False, "error": f"Not a PDF: {path}"})

    ocr_available = False
    if ocr:
        try:
            import pytesseract  # noqa: F401
            ocr_available = True
        except ImportError:
            logger.warning("pytesseract not installed — OCR disabled for this run")

    try:
        import fitz  # PyMuPDF
    except ImportError:
        return json.dumps({
            "success": False,
            "error": "PyMuPDF not installed. Run: pip install pymupdf",
        })

    try:
        doc = fitz.open(path)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Failed to open PDF: {exc}"})

    pages_text: List[str] = []
    ocr_pages = 0
    text_pages = 0
    errors = 0
    total_chars = 0

    for page_num in range(len(doc)):
        try:
            page = doc[page_num]
            # Extract text via PyMuPDF
            text = page.get_text().strip()

            # If text is sparse and OCR is available, render + OCR
            if len(text) < 50 and ocr_available:
                pix = page.get_pixmap(dpi=dpi)
                img_bytes = pix.tobytes("png")
                import pytesseract
                ocr_text = pytesseract.image_to_string(img_bytes).strip()
                if ocr_text:
                    pages_text.append(ocr_text)
                    ocr_pages += 1
                    total_chars += len(ocr_text)
                else:
                    pages_text.append(f"[Page {page_num + 1} — no text extracted]")
            elif text:
                pages_text.append(text)
                text_pages += 1
                total_chars += len(text)
            else:
                pages_text.append(f"[Page {page_num + 1} — empty]")
        except Exception as exc:
            logger.warning("Page %d extraction failed: %s", page_num + 1, exc)
            pages_text.append(f"[Page {page_num + 1} — error: {exc}]")
            errors += 1

    doc.close()

    if not pages_text:
        return json.dumps({
            "success": False,
            "error": "No text could be extracted from the PDF",
        })

    combined = "\n\n".join(pages_text)

    # Write to a temp file and ingest via the normal RAG pipeline
    tmp_dir = _get_db_path().parent / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / f"{pdf_path.stem}_extracted.txt"
    tmp_file.write_text(combined, encoding="utf-8")

    # Ingest the temp file
    result = ingest_file(tmp_file, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    # Clean up temp file
    try:
        tmp_file.unlink()
    except OSError:
        pass

    return json.dumps({
        "success": result.get("status") == "ingested",
        "file": str(pdf_path),
        "pages": len(doc) if doc else len(pages_text),
        "text_pages": text_pages,
        "ocr_pages": ocr_pages,
        "error_pages": errors,
        "total_chars": total_chars,
        "ingest_result": result,
    }, indent=2, ensure_ascii=False)
def rag_remove_source(file_path: str) -> str:
    """Remove an ingested source and its chunks from the RAG store.

    Args:
        file_path: File path of the source to remove.

    Returns:
        JSON string with removal result.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM rag_sources WHERE file_path = ?", (file_path,)
        ).fetchone()
        if not row:
            return json.dumps({
                "success": False, "error": f"Source not found: {file_path}",
            })
        _remove_source(conn, row["id"])
        conn.commit()
        return json.dumps({
            "success": True, "removed": file_path,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error

RAG_INGEST_SCHEMA = {
    "name": "rag_ingest",
    "description": (
        "Ingest documents into the local RAG store. Reads files, splits them "
        "into chunks, generates embeddings (via FastEmbed, local CPU, no API), "
        "and stores for semantic search. Re-ingests only changed files. "
        "Supports: .py, .js, .ts, .rs, .md, .txt, .go, .java, .c, .cpp, "
        "and 30+ other file types."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory path to ingest.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Recurse into subdirectories (default: True).",
            },
            "chunk_size": {
                "type": "integer",
                "description": "Target chunk size in characters (default: 512).",
            },
            "chunk_overlap": {
                "type": "integer",
                "description": "Overlap between chunks in characters (default: 64).",
            },
        },
        "required": ["path"],
    },
}

RAG_QUERY_SCHEMA = {
    "name": "rag_query",
    "description": (
        "Search ingested documents semantically. Embeds the query using "
        "FastEmbed and returns the most relevant document chunks ranked by "
        "cosine similarity. Use this when you need to find information in "
        "documents you've previously ingested with rag_ingest."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 5, max: 50).",
            },
        },
        "required": ["query"],
    },
}

RAG_LIST_SOURCES_SCHEMA = {
    "name": "rag_list_sources",
    "description": "List all ingested document sources with chunk counts and ingestion timestamps.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

RAG_REMOVE_SOURCE_SCHEMA = {
    "name": "rag_remove_source",
    "description": "Remove an ingested source and its chunks from the RAG store.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "File path of the source to remove.",
            },
        },
        "required": ["file_path"],
    },
}

RAG_INGEST_PDF_SCHEMA = {
    "name": "rag_ingest_pdf",
    "description": (
        "Extract text from a PDF and ingest into the RAG store. "
        "Uses PyMuPDF for text extraction. When a page has very little "
        "text (likely scanned), renders it as an image and OCRs via "
        "Tesseract. The extracted text is chunked, embedded with "
        "FastEmbed, and stored for semantic search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the PDF file.",
            },
            "ocr": {
                "type": "boolean",
                "description": "OCR scanned pages via Tesseract (default: True).",
            },
            "dpi": {
                "type": "integer",
                "description": "DPI for page rendering when OCR is needed (default: 300).",
            },
            "chunk_size": {
                "type": "integer",
                "description": "Target chunk size in characters (default: 512).",
            },
            "chunk_overlap": {
                "type": "integer",
                "description": "Overlap between chunks (default: 64).",
            },
        },
        "required": ["path"],
    },
}


def _check_reqs() -> bool:
    try:
        from fastembed import TextEmbedding  # noqa: F401
        return True
    except ImportError:
        return False


def _handle_ingest(args, **kw):
    return rag_ingest(
        path=args.get("path", ""),
        recursive=args.get("recursive", True),
        chunk_size=args.get("chunk_size", _DEFAULT_CHUNK_SIZE),
        chunk_overlap=args.get("chunk_overlap", _DEFAULT_CHUNK_OVERLAP),
    )


def _handle_query(args, **kw):
    return rag_query(
        query=args.get("query", ""),
        top_k=args.get("top_k", _DEFAULT_TOP_K),
    )


def _handle_list_sources(args, **kw):
    return rag_list_sources()


def _handle_remove_source(args, **kw):
    return rag_remove_source(file_path=args.get("file_path", ""))


registry.register(
    name="rag_ingest",
    toolset="rag",
    schema=RAG_INGEST_SCHEMA,
    handler=_handle_ingest,
    check_fn=_check_reqs,
    emoji="📥",
)
registry.register(
    name="rag_query",
    toolset="rag",
    schema=RAG_QUERY_SCHEMA,
    handler=_handle_query,
    check_fn=_check_reqs,
    emoji="🔍",
)


def _handle_ingest_pdf(args, **kw):
    return rag_ingest_pdf(
        path=args.get("path", ""),
        ocr=args.get("ocr", True),
        dpi=args.get("dpi", 300),
        chunk_size=args.get("chunk_size", _DEFAULT_CHUNK_SIZE),
        chunk_overlap=args.get("chunk_overlap", _DEFAULT_CHUNK_OVERLAP),
    )


registry.register(
    name="rag_ingest_pdf",
    toolset="rag",
    schema=RAG_INGEST_PDF_SCHEMA,
    handler=_handle_ingest_pdf,
    check_fn=_check_reqs,
    emoji="📄",
)
registry.register(
    name="rag_list_sources",
    toolset="rag",
    schema=RAG_LIST_SOURCES_SCHEMA,
    handler=_handle_list_sources,
    check_fn=_check_reqs,
    emoji="📋",
)
registry.register(
    name="rag_remove_source",
    toolset="rag",
    schema=RAG_REMOVE_SOURCE_SCHEMA,
    handler=_handle_remove_source,
    check_fn=_check_reqs,
    emoji="🗑️",
)
