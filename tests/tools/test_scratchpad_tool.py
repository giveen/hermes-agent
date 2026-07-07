"""Tests for the scratchpad shared workspace tool.

Key behaviors tested:
  1. Create, append, read round-trip
  2. Concurrent appends from multiple agents — no overwrites
  3. Parent collects all entries after children finish
  4. Markdown formatting
  5. Merge with optional summary
  6. Error handling (missing session, empty sections)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.scratchpad_tool import (
    SCRATCHPAD_APPEND_SCHEMA,
    SCRATCHPAD_CREATE_SCHEMA,
    SCRATCHPAD_MERGE_SCHEMA,
    SCRATCHPAD_READ_SCHEMA,
    scratchpad_append,
    scratchpad_create,
    scratchpad_merge,
    scratchpad_read,
)


@pytest.fixture(autouse=True)
def _scratchpad_env(tmp_path: Path, monkeypatch):
    """Redirect scratchpad DB to a temp directory per test."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Reset the global DB path cache so _get_db_path() picks up the new home
    import tools.scratchpad_tool as spt

    spt._DB_PATH = None
    # Clear thread-local connection
    if hasattr(spt._local, "conn"):
        del spt._local.conn
    yield


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestScratchpadBasic:
    def test_create_and_read_empty(self):
        """Create a scratchpad, verify it exists."""
        result = json.loads(scratchpad_create(name="test-pad", goal="testing"))
        assert result["success"] is True
        session_id = result["session_id"]
        assert session_id.startswith("sp-")

        # Read back — should be empty
        read_result = json.loads(scratchpad_read(session_id))
        assert read_result["success"] is True
        assert read_result["total_entries"] == 0
        assert read_result["total_sections"] == 0

    def test_create_append_read(self):
        """Create, append a few entries, read them back."""
        result = json.loads(scratchpad_create(name="test", goal="research"))
        sid = result["session_id"]

        # Append entries
        for i in range(3):
            r = json.loads(scratchpad_append(sid, "pricing", f"competitor {i} price is ${i*10}"))
            assert r["success"] is True

        r = json.loads(scratchpad_append(sid, "features", "competitor X has feature Y"))
        assert r["success"] is True

        # Read all
        read_result = json.loads(scratchpad_read(sid))
        assert read_result["total_entries"] == 4
        assert read_result["total_sections"] == 2
        assert "pricing" in read_result["sections"]
        assert "features" in read_result["sections"]
        assert len(read_result["sections"]["pricing"]) == 3
        assert len(read_result["sections"]["features"]) == 1

    def test_read_single_section(self):
        """Filter by section name."""
        result = json.loads(scratchpad_create(name="filter-test"))
        sid = result["session_id"]

        scratchpad_append(sid, "section_a", "data A")
        scratchpad_append(sid, "section_b", "data B")
        scratchpad_append(sid, "section_a", "more A")

        # Read only section_a
        read_result = json.loads(scratchpad_read(sid, section="section_a"))
        assert read_result["total_entries"] == 2
        assert "section_a" in read_result["sections"]
        assert "section_b" not in read_result["sections"]

    def test_read_markdown_format(self):
        """Markdown format returns clean structured markdown."""
        result = json.loads(scratchpad_create(name="md-test", goal="markdown check"))
        sid = result["session_id"]

        scratchpad_append(sid, "results", "Found X", agent_id="agent-a")
        scratchpad_append(sid, "results", "Found Y", agent_id="agent-b")

        md = scratchpad_read(sid, format="markdown")
        assert "# Scratchpad: md-test" in md
        assert "**Goal:** markdown check" in md
        assert "## results" in md
        assert "Found X" in md
        assert "Found Y" in md
        assert "(by agent-a)" in md
        assert "(by agent-b)" in md
        assert "Merged at" in md

    def test_error_missing_session(self):
        """Append/read/merge on a non-existent session raises a clear error."""
        with pytest.raises(ValueError, match="not found"):
            scratchpad_append("sp-nonexistent", "section", "data")

        with pytest.raises(ValueError, match="not found"):
            scratchpad_read("sp-nonexistent")

        with pytest.raises(ValueError, match="not found"):
            scratchpad_merge("sp-nonexistent")


# ---------------------------------------------------------------------------
# Concurrent access — no overwrites
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_parallel_appends_no_overwrites(self):
        """Simulate N subagents appending to the same scratchpad concurrently.
        Every entry must survive — no overwrites, no data loss."""
        result = json.loads(scratchpad_create(name="concurrent-test"))
        sid = result["session_id"]

        N_AGENTS = 5
        ENTRIES_PER_AGENT = 20
        barrier = threading.Barrier(N_AGENTS)
        errors: list[Exception] = []
        lock = threading.Lock()

        def _agent_task(agent_id: str):
            try:
                barrier.wait(timeout=10)
                for i in range(ENTRIES_PER_AGENT):
                    r = json.loads(
                        scratchpad_append(sid, "data", f"{agent_id}-entry-{i}", agent_id=agent_id)
                    )
                    assert r["success"] is True
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=_agent_task, args=(f"agent-{n}",))
            for n in range(N_AGENTS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        # Verify all entries survived
        read_result = json.loads(scratchpad_read(sid))
        assert read_result["total_entries"] == N_AGENTS * ENTRIES_PER_AGENT, (
            f"Expected {N_AGENTS * ENTRIES_PER_AGENT} entries, got {read_result['total_entries']}"
        )
        assert read_result["total_sections"] == 1

        # Verify every agent's entries are present
        contents = [e["content"] for e in read_result["sections"]["data"]]
        for n in range(N_AGENTS):
            for i in range(ENTRIES_PER_AGENT):
                expected = f"agent-{n}-entry-{i}"
                assert expected in contents, f"Missing: {expected}"

    def test_same_section_concurrent_appends_preserve_order(self):
        """Entries appended to the same section keep insertion order."""
        result = json.loads(scratchpad_create(name="order-test"))
        sid = result["session_id"]

        N = 30
        errors: list[Exception] = []
        lock = threading.Lock()

        def _writer(idx: int):
            try:
                r = json.loads(
                    scratchpad_append(sid, "ordered", f"entry-{idx:04d}")
                )
                assert r["success"] is True
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        read_result = json.loads(scratchpad_read(sid))
        entries = read_result["sections"]["ordered"]
        assert len(entries) == N


# ---------------------------------------------------------------------------
# Parent collection
# ---------------------------------------------------------------------------


class TestParentCollection:
    def test_parent_creates_collects_summary(self):
        """Simulate the full parent → children → parent workflow.

        Parent:
          1. Creates scratchpad
          2. Passes session_id to children
        Children (simulated as direct calls):
          3. Each appends findings
        Parent:
          4. Reads all entries
          5. Merges into structured markdown
        """
        # Step 1: Parent creates scratchpad
        result = json.loads(scratchpad_create(
            name="competitor-research",
            goal="Research 3 competitors: pricing, features, market position",
        ))
        sid = result["session_id"]

        # Step 2-3: Simulate 3 children writing findings
        # Child A: pricing research
        scratchpad_append(sid, "pricing", "Competitor A: $99/mo basic, $199/mo pro", agent_id="child-a")
        scratchpad_append(sid, "pricing", "Competitor B: free tier + $49/mo paid", agent_id="child-a")

        # Child B: features research
        scratchpad_append(sid, "features", "Competitor A: API, SSO, audit logs", agent_id="child-b")
        scratchpad_append(sid, "features", "Competitor B: API only, no SSO", agent_id="child-b")

        # Child C: market position
        scratchpad_append(sid, "market", "Competitor A targets enterprise (10k+ employees)", agent_id="child-c")
        scratchpad_append(sid, "market", "Competitor B targets SMB (1-50 employees)", agent_id="child-c")

        # Step 4: Parent reads all
        read_result = json.loads(scratchpad_read(sid))
        assert read_result["total_entries"] == 6
        assert read_result["total_sections"] == 3
        assert set(read_result["sections"].keys()) == {"pricing", "features", "market"}

        # Step 5: Parent merges — no LLM summary (unit test, no aux model)
        merge_result = json.loads(scratchpad_merge(sid, summarize=False))
        assert merge_result["success"] is True
        assert merge_result["total_entries"] == 6
        assert merge_result["total_sections"] == 3

        merged = merge_result["merged_markdown"]
        assert "# Scratchpad: competitor-research" in merged
        assert "**Goal:** Research 3 competitors" in merged
        assert "## pricing" in merged
        assert "## features" in merged
        assert "## market" in merged
        assert "Competitor A: $99/mo" in merged
        assert "Competitor B: API only" in merged
        assert "(by child-a)" in merged
        assert "(by child-b)" in merged
        assert "(by child-c)" in merged


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_create_schema_requires_name(self):
        assert SCRATCHPAD_CREATE_SCHEMA["parameters"]["required"] == ["name"]

    def test_append_schema_requires_session_section_content(self):
        required = SCRATCHPAD_APPEND_SCHEMA["parameters"]["required"]
        assert "session_id" in required
        assert "section" in required
        assert "content" in required

    def test_read_schema_requires_session_id(self):
        assert SCRATCHPAD_READ_SCHEMA["parameters"]["required"] == ["session_id"]

    def test_merge_schema_requires_session_id(self):
        assert SCRATCHPAD_MERGE_SCHEMA["parameters"]["required"] == ["session_id"]
