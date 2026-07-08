"""Tests for agent.coding_context — RuntimeMode seam, resolver, toolset, git probe."""

import json
import os
import subprocess
import shutil
from pathlib import Path

import pytest

from agent import coding_context as cc


def test_coding_guidance_advertises_persistent_terminal_state():
    assert "Terminal state persists across calls" in cc.CODING_AGENT_GUIDANCE
    assert "Activate a virtualenv" in cc.CODING_AGENT_GUIDANCE
    assert "instead of re-sourcing it before every test command" in cc.CODING_AGENT_GUIDANCE


def _git_init(path):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(path),
    }
    # Commit a source file so the fixture is a real *code* workspace: a bare git
    # repo with no code no longer flips into the coding posture (see
    # _detect_profile_name / _has_code_files), so "a code repo" needs code.
    (Path(path) / "main.py").write_text("print('hi')\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init commit"],
    ):
        subprocess.run([shutil.which("git"), "-C", str(path), *args], check=True, env=env)


# ── resolver ──────────────────────────────────────────────────────────────

class TestIsCodingContext:
    def test_off_never_activates(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "off"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False

    def test_on_forces_even_without_git(self, tmp_path):
        cfg = {"agent": {"coding_context": "on"}}
        assert cc.is_coding_context(platform="telegram", cwd=tmp_path, config=cfg) is True

    def test_auto_requires_git_repo(self, tmp_path):
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False
        _git_init(tmp_path)
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_auto_bare_git_repo_without_code_stays_general(self, tmp_path):
        # A git repo of only prose (notes/writing/research — a big non-coding use
        # case) is NOT a code workspace: .git alone must not flip the posture.
        cfg = {"agent": {"coding_context": "auto"}}
        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path),
        }
        (tmp_path / "notes.md").write_text("# my novel\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "notes"]):
            subprocess.run([shutil.which("git"), "-C", str(tmp_path), *args], check=True, env=env)

        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False
        # …but adding a manifest or source file makes it a code workspace.
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_auto_skips_messaging_surfaces(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="discord", cwd=tmp_path, config=cfg) is False
        assert cc.is_coding_context(platform="tui", cwd=tmp_path, config=cfg) is True

    def test_default_mode_is_auto(self, tmp_path):
        # Unknown/missing value normalizes to auto.
        _git_init(tmp_path)
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config={}) is True


# ── toolset substitution ────────────────────────────────────────────────────

class TestCodingSelection:
    def test_selects_coding_under_focus(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "focus"}}
        out = cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg)
        assert out is not None
        assert out[0] == cc.CODING_TOOLSET

    def test_auto_is_prompt_only(self, tmp_path):
        # Default posture must never override the user's configured toolsets —
        # off-by-default toolsets are already off, and explicit opt-ins
        # (image-gen, other tools, …) survive entering a code workspace.
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None
        # …while the prompt posture is still active.
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_on_is_prompt_only(self, tmp_path):
        cfg = {"agent": {"coding_context": "on"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_focus_requires_workspace(self, tmp_path):
        # focus inherits auto's detection gate — bare dir stays general.
        cfg = {"agent": {"coding_context": "focus"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None

    def test_none_when_inactive(self, tmp_path):
        cfg = {"agent": {"coding_context": "off"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None

    def test_coding_toolset_is_registered(self):
        from toolsets import resolve_toolset

        tools = resolve_toolset(cc.CODING_TOOLSET)
        # Coding essentials present…
        for t in ("read_file", "write_file", "patch", "search_files", "terminal", "todo"):
            assert t in tools
        # …and the noise is gone.
        for t in ("send_message", "text_to_speech", "image_generate", "computer_use"):
            assert t not in tools


# ── git/workspace probe ─────────────────────────────────────────────────────

class TestWorkspaceBlock:
    def test_empty_outside_repo(self, tmp_path):
        assert cc.build_coding_workspace_block(tmp_path) == ""

    def test_reports_branch_and_clean_status(self, tmp_path):
        _git_init(tmp_path)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Workspace" in block
        assert f"Root: {tmp_path.resolve()}" in block or "Root:" in block
        assert "Branch: main" in block
        assert "Status: clean" in block
        assert "init commit" in block

    def test_reports_dirty_counts(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "untracked.txt").write_text("hi")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "untracked" in block
        assert "clean" not in block.split("Status:")[1].splitlines()[0]


# ── project facts (verify-loop detection) ───────────────────────────────────

class TestProjectFacts:
    def test_package_json_scripts_surface_verify_commands(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "lint": "eslint .", "dev": "vite"}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Project: package.json (pnpm)" in block
        assert "pnpm run test" in block and "pnpm run lint" in block
        # Non-verify scripts (dev servers, …) stay out of the snapshot.
        assert "run dev" not in block

    def test_pytest_config_and_run_tests_script(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "run_tests.sh").write_text("#!/bin/sh\n")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "scripts/run_tests.sh" in block
        assert "pytest" in block.split("Verify:")[1]

    def test_makefile_verify_targets_only(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "Makefile").write_text("test:\n\tgo test ./...\n\ndeploy:\n\t./deploy.sh\n")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "make test" in block
        assert "make deploy" not in block

    def test_context_files_listed(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "AGENTS.md").write_text("# rules")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Context files: AGENTS.md" in block

    def test_worktree_detected_without_primary_path(self, tmp_path):
        # A linked worktree should be detected, but the output must NOT contain
        # the absolute path to the primary tree — exposing that path causes the
        # model to sometimes run commands in the wrong directory.
        main_tree = tmp_path / "main"
        main_tree.mkdir()
        _git_init(main_tree)
        worktree = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main_tree), "worktree", "add", "-b", "wt-branch", str(worktree)],
            check=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        block = cc.build_coding_workspace_block(worktree)
        assert "Worktree: linked" in block
        # The primary tree path must NOT appear anywhere in the output.
        assert str(main_tree.resolve()) not in block
        assert str(main_tree) not in block
        # The worktree root IS the reported root.
        assert f"Root: {worktree.resolve()}" in block or "Root:" in block

    def test_marker_only_project_gets_snapshot_without_git(self, tmp_path):
        # A non-git project (manifest only) still gets a workspace snapshot —
        # just without the git lines.
        (tmp_path / "package.json").write_text("{}")
        block = cc.build_coding_workspace_block(tmp_path)
        assert f"Root: {tmp_path.resolve()}" in block
        assert "package.json" in block
        assert "Branch:" not in block and "Status:" not in block

    def test_malformed_package_json_is_ignored(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "package.json").write_text("{not json")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Project: package.json" in block
        assert "Verify:" not in block

    def test_detect_project_facts_structured(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "dev": "vite"}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        facts = cc.detect_project_facts(tmp_path)
        assert facts.manifests == ["package.json"]
        assert facts.package_managers == ["pnpm"]
        assert facts.verify_commands == ["pnpm run test"]  # dev excluded
        assert facts.context_files == []

    def test_project_facts_for_matches_prompt_block(self, tmp_path):
        # Invariant: the structured facts the UI consumes must not drift from the
        # commands the prompt snapshot renders — one detector feeds both.
        _git_init(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        facts = cc.project_facts_for(tmp_path)
        assert facts is not None
        verify_line = cc.build_coding_workspace_block(tmp_path).split("Verify:")[1].splitlines()[0]
        assert facts["verifyCommands"]
        for cmd in facts["verifyCommands"]:
            assert cmd in verify_line

    def test_project_facts_for_none_outside_workspace(self, tmp_path):
        assert cc.project_facts_for(tmp_path) is None


# ── $HOME dotfiles guard ────────────────────────────────────────────────────

class TestHomeDotfilesGuard:
    def test_dotfiles_repo_at_home_is_not_coding(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _git_init(home)
        monkeypatch.setattr(Path, "home", lambda: home)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=home, config=cfg) is False
        # …and a plain subdirectory of the dotfiles repo stays general too.
        docs = home / "Documents"
        docs.mkdir()
        assert cc.is_coding_context(platform="cli", cwd=docs, config=cfg) is False

    def test_marker_at_home_is_not_a_project_signal(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / "Makefile").write_text("all:\n")
        monkeypatch.setattr(Path, "home", lambda: home)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=home, config=cfg) is False

    def test_real_project_under_dotfiles_home_still_detects(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _git_init(home)
        monkeypatch.setattr(Path, "home", lambda: home)
        proj = home / "www" / "app"
        proj.mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=proj, config=cfg) is True

    def test_on_mode_bypasses_the_guard(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        cfg = {"agent": {"coding_context": "on"}}
        assert cc.is_coding_context(platform="cli", cwd=home, config=cfg) is True
