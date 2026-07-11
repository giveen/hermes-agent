"""Native full-file read (read_file_raw) for the LOCAL file-ops backend.

Mirrors the paginated read's native fast path: the local backend reads via
stdlib (os + pathlib) instead of shelling out to ``wc -c`` / ``head -c`` /
``cat``.  Remote backends (docker/ssh/modal/daytona) keep the shell read
because their filesystem is unreachable from host Python.

Asserts: (1) zero subprocess spawns on local, (2) byte-identical ``content``
and ``file_size`` vs the shell path across varied file shapes, (3) BOM is
stripped (so patch's fuzzy matcher sees clean content), (4) binary/image
redirect parity, (5) remote still routes through execute() and matches.
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


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def test_native_read_raw_uses_no_shell(tmp_path):
    p = tmp_path / "a.py"
    _write(str(p), "x = 1\ny = 2\n")
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file_raw(str(p))
    assert res.error is None
    assert ops.env.calls == 0


def test_native_read_raw_byte_identical_to_shell(tmp_path):
    cases = {
        "plain.py": "def f():\n    return 1\n" * 50,
        "bom.py": "print(1)\nprint(2)\n",
        "crlf.txt": "a\r\nb\r\nc\r\n",
        "nolf.txt": "x\ny\nz",
        "unicode.txt": "hello 世界 \nline2\n",
        "manylines": "\n".join(f"l{i}" for i in range(2000)) + "\n",
    }
    for name, content in cases.items():
        p = tmp_path / name
        _write(str(p), content)
        local = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
        remote = ShellFileOperations(_RemoteEnv(str(tmp_path)), cwd=str(tmp_path))
        rl = local.read_file_raw(str(p))
        rr = remote.read_file_raw(str(p))
        if name == "crlf.txt":
            # The remote mock shells `cat` via subprocess.run(text=True), which
            # strips CR on this harness; compare native against on-disk bytes
            # (authoritative) instead. Native read must be byte-faithful.
            assert rl.content == content, f"{name}: native not byte-identical to disk"
            with open(str(p), "rb") as fb:
                assert rl.content.encode("utf-8") == fb.read(), f"{name}: native bytes differ"
        else:
            assert rl.content == rr.content, f"{name}: content differs"
        assert rl.file_size == rr.file_size, f"{name}: size differs"
        assert local.env.calls == 0
        assert remote.env.calls == 3  # wc -c + head -c + cat


def test_native_read_raw_strips_bom(tmp_path):
    p = tmp_path / "bomcheck.py"
    with open(str(p), "wb") as fh:
        fh.write(b"\xef\xbb\xbfx=1\ny=2\n")
    res = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path)).read_file_raw(str(p))
    assert res.content[0] != "\ufeff"
    assert res.content == "x=1\ny=2\n"


def test_native_read_raw_binary_detected(tmp_path):
    p = tmp_path / "x.bin"
    with open(str(p), "wb") as fh:
        fh.write(b"\x00" * 1000)
    res = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path)).read_file_raw(str(p))
    assert res.is_binary is True
    assert res.error is not None


def test_native_read_raw_image_redirect(tmp_path):
    p = tmp_path / "pic.png"
    _write(str(p), "x")
    res = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path)).read_file_raw(str(p))
    assert res.is_image is True


def test_native_read_raw_missing_file_suggests(tmp_path):
    ops = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    res = ops.read_file_raw(str(tmp_path / "ghost.txt"))
    assert res.error is not None
    assert "File not found" in res.error


def test_remote_read_raw_matches_native_and_routes_shell(tmp_path):
    p = tmp_path / "r.txt"
    _write(str(p), "v1\nv2\nv3\n")
    local = ShellFileOperations(_LocalEnv(str(tmp_path)), cwd=str(tmp_path))
    remote = ShellFileOperations(_RemoteEnv(str(tmp_path)), cwd=str(tmp_path))
    rl = local.read_file_raw(str(p))
    rr = remote.read_file_raw(str(p))
    assert rl.content == rr.content
    assert rl.file_size == rr.file_size
    assert remote.env.calls == 3
