"""Native read path for the LOCAL file-ops backend.

The local backend (HostEnvironment) must read via stdlib (os + pathlib)
instead of shelling out to ``wc -c`` / ``head -c`` / ``sed -n`` / ``wc -l``.
Remote backends (docker/ssh/modal/daytona) keep the shell read because
their filesystem is unreachable from host Python.

These tests assert: (1) the native path lands zero subprocess spawns,
(2) its ``ReadResult`` output is byte-identical to the shell path on the
same file, (3) the line count is correct for a final line with NO trailing
newline (the shell ``wc -l`` undercounts there), and (4) the remote path
still routes through execute() and matches the native output.
"""

from __future__ import annotations

import os
import subprocess

from tools.file_operations import ShellFileOperations


class _LocalEnv:
    is_local = True

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = 0

    def execute(self, command: str, cwd: str = None, **kwargs):
        self.calls += 1
        return {"output": "", "returncode": 0}


class _RemoteEnv:
    is_local = False

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = 0

    def execute(self, command: str, cwd: str = None, **kwargs):
        self.calls += 1
        data = kwargs.get("stdin_data")
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd or self.cwd,
            input=data,
            capture_output=True,
            text=True,
        )
        return {"output": proc.stdout + proc.stderr, "returncode": proc.returncode}


def _make_file(tmp_path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_native_read_uses_no_shell(tmp_path):
    p = _make_file(tmp_path, "a.py", "x = 1\ny = 2\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(p)
    assert res.error is None
    assert ops.env.calls == 0


def test_native_read_matches_shell_output(tmp_path):
    # Identical content + contract on the same file.
    content = "\n".join(f"def f{i}(): return {i}" for i in range(500)) + "\n"
    p = _make_file(tmp_path, "big.py", content)
    local = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    remote = ShellFileOperations(_RemoteEnv(str(tmp_path)), cwd=str(tmp_path))
    r_loc = local.read_file(p, 1, 200)
    r_rem = remote.read_file(p, 1, 200)
    assert r_loc.content == r_rem.content
    assert r_loc.total_lines == r_rem.total_lines == 500
    assert r_loc.truncated == r_rem.truncated


def test_native_read_pagination_midfile(tmp_path):
    content = "\n".join(f"line{i}" for i in range(1, 101)) + "\n"
    p = _make_file(tmp_path, "mid.txt", content)
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(p, 41, 20)
    first = res.content.split("\n")[0]
    assert first == "41|line41"
    assert res.total_lines == 100
    assert res.truncated is True


def test_native_read_eof_no_trailing_newline_counts_correctly(tmp_path):
    # 3 real lines, NO trailing newline.  wc -l would report 2 (undercount).
    p = _make_file(tmp_path, "nonl.txt", "line1\nline2\nline3")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(p, 1, 500)
    assert res.error is None
    assert res.total_lines == 3
    assert res.truncated is False
    assert "3|line3" in res.content


def test_native_read_binary_detected(tmp_path):
    # 1000 NUL bytes -> binary.
    p = tmp_path / "bin.dat"
    p.write_bytes(b"\x00" * 1000)
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(str(p))
    assert res.is_binary is True
    assert res.error is not None


def test_native_read_image_redirects(tmp_path):
    p = _make_file(tmp_path, "pic.png", "notreallypng")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(str(p))
    assert res.is_image is True
    assert res.error is None


def test_native_read_missing_file_suggests(tmp_path):
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file(str(tmp_path / "ghost.txt"))
    assert res.error is not None
    assert "File not found" in res.error


def test_remote_read_matches_native_and_routes_shell(tmp_path):
    content = "\n".join(f"v{i}" for i in range(50)) + "\n"
    p = _make_file(tmp_path, "r.txt", content)
    local = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    remote = ShellFileOperations(_RemoteEnv(str(tmp_path)), cwd=str(tmp_path))
    r_loc = local.read_file(p, 1, 50)
    r_rem = remote.read_file(p, 1, 50)
    assert r_loc.content == r_rem.content
    assert remote.env.calls >= 1
