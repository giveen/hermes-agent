"""Tests for the RAG tool (FastEmbed-powered semantic search)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.rag_tool import (
    _chunk_text,
    _collect_files,
    ingest_file,
    rag_ingest,
    rag_query,
    rag_list_sources,
    rag_remove_source,
    search_query,
)


@pytest.fixture(autouse=True)
def _rag_env(tmp_path: Path, monkeypatch):
    """Redirect RAG DB to a temp directory per test."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import tools.rag_tool as rt
    rt._DB_PATH = None
    yield


_SAMPLE_DOCS = {
    "readme.md": "# Herminator\n\nA personal AI agent that runs anywhere.\n\n## Features\n- Memory across sessions via Mnemosyne\n- Web search via DuckDuckGo\n- Web extraction via Crawl4AI\n- Code analysis via tree-sitter\n- RAG document search via FastEmbed",
    "setup.py": "from setuptools import setup\n\nsetup(\n    name='herminator',\n    version='0.18.0',\n    description='A personal AI agent',\n)",
    "notes.txt": "Meeting notes from 2026-07-07:\n- Jeremy wants the agent to remember his name\n- System specs: Intel Core Ultra 9, NVIDIA RTX 5090\n- Using Mnemosyne for long-term memory\n- FastEmbed for RAG document search",
}


@pytest.fixture
def doc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    for name, content in _SAMPLE_DOCS.items():
        (d / name).write_text(content)
    return d


class TestChunking:
    def test_small_text_no_split(self):
        chunks = _chunk_text("Hello world", chunk_size=1000)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_splits_on_paragraphs(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = _chunk_text(text, chunk_size=50)
        assert len(chunks) >= 2

    def test_overlap(self):
        text = "A" * 100 + "\n\n" + "B" * 100
        chunks = _chunk_text(text, chunk_size=80, overlap=20)
        if len(chunks) > 1:
            # Check overlap is present
            assert chunks[0][-20:] in chunks[1] or chunks[0][-20:] == chunks[1][:20]

    def test_empty_text(self):
        assert _chunk_text("") == []
        assert _chunk_text("   \n\n  ") == []

    def test_splits_on_paragraphs(self):
        text = "Para one.\n\nPara two.\n\nPara three.\n\nPara four.\n\nPara five."
        chunks = _chunk_text(text, chunk_size=20)
        assert len(chunks) >= 2


class TestCollectFiles:
    def test_single_file(self, doc_dir: Path):
        files = _collect_files(doc_dir / "readme.md")
        assert len(files) == 1

    def test_directory(self, doc_dir: Path):
        files = _collect_files(doc_dir)
        assert len(files) == 3

    def test_skips_node_modules(self, tmp_path: Path):
        bad = tmp_path / "node_modules" / "bad.py"
        bad.parent.mkdir(parents=True)
        bad.write_text("x = 1")
        files = _collect_files(tmp_path)
        assert len(files) == 0

    def test_skips_hidden_files(self, tmp_path: Path):
        hidden = tmp_path / ".secret.py"
        hidden.write_text("x = 1")
        files = _collect_files(tmp_path)
        assert len(files) == 0


class TestIngestion:
    def test_ingest_single_file(self, doc_dir: Path):
        result = json.loads(rag_ingest(str(doc_dir / "readme.md")))
        assert result["success"] is True
        assert result["files_ingested"] == 1
        assert result["total_chunks"] >= 1

    def test_ingest_directory(self, doc_dir: Path):
        result = json.loads(rag_ingest(str(doc_dir)))
        assert result["success"] is True
        assert result["files_ingested"] == 3

    def test_ingest_twice_is_idempotent(self, doc_dir: Path):
        r1 = json.loads(rag_ingest(str(doc_dir)))
        assert r1["files_ingested"] == 3
        r2 = json.loads(rag_ingest(str(doc_dir)))
        assert r2["files_unchanged"] == 3
        assert r2["files_ingested"] == 0

    def test_reingest_on_change(self, doc_dir: Path):
        rag_ingest(str(doc_dir))
        # Modify a file
        f = doc_dir / "notes.txt"
        f.write_text(f.read_text() + "\n- New note added")
        result = json.loads(rag_ingest(str(doc_dir)))
        assert result["files_ingested"] >= 1

    def test_unsupported_file(self, tmp_path: Path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02")
        result = json.loads(rag_ingest(str(f)))
        assert result["success"] is False

    def test_nonexistent_path(self):
        result = json.loads(rag_ingest("/nonexistent"))
        assert result["success"] is False


class TestQuery:
    def test_query_after_ingest(self, doc_dir: Path):
        rag_ingest(str(doc_dir))
        result = json.loads(rag_query("What are the system specs?"))
        assert result["success"] is True
        assert result["total_results"] >= 1
        # Should find the notes about system specs
        texts = [r["text"] for r in result["results"]]
        combined = " ".join(texts)
        assert any(kw in combined.lower() for kw in ["nvidia", "rtx", "intel", "5090"])

    def test_query_before_ingest(self):
        result = json.loads(rag_query("anything"))
        assert result["success"] is True
        assert result["total_results"] == 0

    def test_query_empty(self):
        result = json.loads(rag_query(""))
        assert result["success"] is False

    def test_query_top_k(self, doc_dir: Path):
        rag_ingest(str(doc_dir))
        result = json.loads(rag_query("AI agent", top_k=2))
        assert result["success"] is True
        assert len(result["results"]) <= 2

    def test_query_max_top_k(self):
        result = json.loads(rag_query("test", top_k=100))
        assert result["success"] is True  # clamped to 50


class TestListSources:
    def test_list_sources(self, doc_dir: Path):
        rag_ingest(str(doc_dir))
        result = json.loads(rag_list_sources())
        assert result["success"] is True
        assert result["total_sources"] == 3
        assert result["total_chunks"] >= 1

    def test_list_sources_empty(self):
        result = json.loads(rag_list_sources())
        assert result["success"] is True
        assert result["total_sources"] == 0


class TestRemoveSource:
    def test_remove_source(self, doc_dir: Path):
        rag_ingest(str(doc_dir))
        result = json.loads(rag_remove_source(str(doc_dir / "readme.md")))
        assert result["success"] is True
        sources = json.loads(rag_list_sources())
        assert sources["total_sources"] == 2

    def test_remove_nonexistent(self):
        result = json.loads(rag_remove_source("/nonexistent"))
        assert result["success"] is False


class TestSchemas:
    def test_ingest_schema_requires_path(self):
        from tools.rag_tool import RAG_INGEST_SCHEMA
        assert "path" in RAG_INGEST_SCHEMA["parameters"]["required"]

    def test_query_schema_requires_query(self):
        from tools.rag_tool import RAG_QUERY_SCHEMA
        assert "query" in RAG_QUERY_SCHEMA["parameters"]["required"]
