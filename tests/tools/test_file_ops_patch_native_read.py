"""Route patch_replace reads through native read_file_raw (local backend).

Verifies that ``patch_replace`` no longer shells out to ``cat`` for its own
initial read and post-write verification (both now use ``read_file_raw``,
which reads via stdlib on the local backend). The shared lint subsystem
(``_check_lint_delta``) still shells for in-process linter content — that is
out of patch_replace's body and out of scope here, so we assert patch_replace
adds NO cat reads beyond what a plain write_file already triggers.

Also pins the correctness invariants that this routing must not regress:
CRLF is preserved on disk, binary files are refused, and edit results match.
"""

from __future__ import annotations

import os

from tools.file_operations import ShellFileOperations


class _LocalEnv:
    is_local = True

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.cmds = []

    def execute(self, command: str, cwd: str = None, **kwargs):
        self.cmds.append(command)
        return {"output": "", "returncode": 0}


class _RemoteEnv:
    is_local = False

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.cmds = []

    def execute(self, command: str, cwd: str = None, **kwargs):
        self.cmds.append(command)
        import subprocess
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd or self.cwd,
            input=kwargs.get("stdin_data"),
            capture_output=True,
            text=True,
        )
        return {"output": proc.stdout + proc.stderr, "returncode": proc.returncode}


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def _cat_reads(cmds) -> list:
    return [c for c in cmds if c.strip().startswith("cat ")]


def test_patch_replace_local_adds_no_own_cat_reads(tmp_path):
    # A plain write_file triggers the lint subsystem's cat read(s). Capture
    # that baseline; patch_replace must add no further cat reads of its own
    # (its initial read + post-write verify now go through native read_file_raw).
    p = tmp_path / "app.py"
    _write(str(p), "def f():\n    return 1\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    ops.write_file(str(p), "def f():\n    return 1\n")
    baseline_cats = _cat_reads(ops.env.cmds)
    ops.env.cmds.clear()

    res = ops.patch_replace(str(p), "return 1", "return 2")
    assert res.success is True
    patch_cats = _cat_reads(ops.env.cmds)
    # patch_replace must not introduce cat reads beyond the lint baseline.
    assert patch_cats == baseline_cats, f"patch_replace added cat reads: {patch_cats}"
    assert open(str(p)).read() == "def f():\n    return 2\n"


def test_patch_replace_local_verify_reread_native(tmp_path):
    p = tmp_path / "m.py"
    _write(str(p), "a = 1\nb = 2\nc = 3\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.patch_replace(str(p), "b = 2", "b = 99")
    assert res.success is True
    # Post-write verify re-read is native (read_file_raw), not a cat.
    patch_cats = _cat_reads(ops.env.cmds)
    baseline = _cat_reads(_capture_write_baseline(ops, str(p)))
    assert patch_cats == baseline, f"verify re-read added cat: {patch_cats}"
    assert "b = 99" in open(str(p)).read()


def _capture_write_baseline(ops, path: str) -> list:
    ops.env.cmds.clear()
    ops.write_file(path, open(path).read())
    return list(ops.env.cmds)


def test_patch_replace_remote_still_shells(tmp_path):
    p = tmp_path / "r.py"
    _write(str(p), "x = 1\n")
    ops = ShellFileOperations(_RemoteEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.patch_replace(str(p), "x = 1", "x = 2")
    assert res.success is True
    full_cat = _cat_reads(ops.env.cmds)
    assert len(full_cat) >= 2  # read + verify via shell cat on remote
    assert open(str(p)).read() == "x = 2\n"


def test_patch_replace_crlf_preserved_on_disk(tmp_path):
    # Native read preserves CRLF; write_file round-trips it. Verify via BYTES
    # (Python text-mode open() translates \r\n -> \n on read, hiding it).
    p = tmp_path / "crlf.py"
    _write(str(p), "a\r\nb\r\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.patch_replace(str(p), "b", "B")
    assert res.success is True
    assert open(str(p), "rb").read() == b"a\r\nB\r\n"


def test_patch_replace_binary_guarded(tmp_path):
    p = tmp_path / "x.bin"
    with open(str(p), "wb") as fh:
        fh.write(b"\x00\x01\x02" * 300)
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.patch_replace(str(p), "\x00\x01", "\x00\x02")
    # Native read_file_raw detects binary and patch_replace refuses, instead
    # of attempting a text patch on mojibake (old cat path's silent failure).
    assert res.success is False
    assert res.error is not None and "binary" in res.error.lower()


def test_lint_delta_reads_from_disk_without_cat(tmp_path):
    # _check_lint_delta must read content from disk (content=None) via
    # read_file_raw, not cat, on the local backend.
    p = tmp_path / "lintme.py"
    _write(str(p), "x=1\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops._check_lint_delta(str(p), pre_content=None, post_content="x=1\n")
    # No cat on the local read path.
    assert _cat_reads(ops.env.cmds) == [], f"lint delta cat-reads: {_cat_reads(ops.env.cmds)}"
    # Lint result is a real LintResult (success or skipped, not error).
    assert res is not None


def test_write_file_precontent_read_without_cat(tmp_path):
    # write_file's pre-content read (for lint delta + LSP shift map) must be
    # native on local, not cat.
    p = tmp_path / "w.py"
    _write(str(p), "y = 2\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    ops.write_file(str(p), "y = 3\n")
    assert _cat_reads(ops.env.cmds) == [], f"write pre-content cat-reads: {_cat_reads(ops.env.cmds)}"
    assert open(str(p)).read() == "y = 3\n"
