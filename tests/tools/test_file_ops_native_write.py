"""Native-write fast path for the LOCAL file-ops backend.

The local backend (HostEnvironment) must write via native os/pathlib
(temp-file + os.replace) instead of spawning the shell write pipeline
(mkdir + mktemp/cat>tmp + mv + wc -c).  Remote backends
(docker/ssh/modal/daytona) must NOT take the native path because their
filesystem is unreachable from host Python — they keep the shell write.

These tests assert the *behavior contract* stays identical (atomic swap,
byte count, mode preservation, CRLF/BOM passthrough) while proving the
write pipeline does not spawn subprocesses on the local path, and that the
remote path still routes through execute() and lands the file.
"""

from __future__ import annotations

import os
import stat
import subprocess

from tools.file_operations import ShellFileOperations


# Commands that make up the legacy shell WRITE pipeline.  The native path
# must not emit any of these — the lint pre-read (cat/head) is a separate,
# smaller concern that both paths share.
_WRITE_PIPELINE = ("mkdir -p", "mktemp", "mv -f", "wc -c")


class _LocalEnv:
    """Minimal stand-in for the real HostEnvironment (local backend).

    Sets ``is_local = True`` so the native path is selected.  execute() is
    recorded but must NOT be used for the write pipeline.
    """

    is_local = True

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.exec_calls: list = []

    def execute(self, command: str, cwd: str = None, **kwargs):
        self.exec_calls.append(command)
        return {"output": "", "returncode": 0}


def _local_ops(tmp_path):
    env = _LocalEnv(cwd=str(tmp_path))
    return ShellFileOperations(env, cwd=str(tmp_path)), env


def _assert_no_write_pipeline(env):
    for call in env.exec_calls:
        for token in _WRITE_PIPELINE:
            assert token not in call, f"native write spawned shell: {call!r}"


def test_native_write_uses_no_shell_pipeline(tmp_path):
    ops, env = _local_ops(tmp_path)
    target = str(tmp_path / "out.py")
    res = ops.write_file(target, "print('hello')\n")
    assert res.error is None
    assert res.bytes_written == len("print('hello')\n".encode("utf-8"))
    _assert_no_write_pipeline(env)
    # Bytes actually landed (without going through the shell write).
    with open(target, "r", encoding="utf-8") as fh:
        assert fh.read() == "print('hello')\n"


def test_native_write_is_atomic_rename(tmp_path):
    ops, env = _local_ops(tmp_path)
    target = str(tmp_path / "atomic.txt")
    # Pre-create so we can confirm mode preservation + atomic swap.
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("old")
    os.chmod(target, 0o600)
    res = ops.write_file(target, "new-content")
    assert res.error is None
    _assert_no_write_pipeline(env)
    # No .hermes-tmp turd left behind.
    leftovers = list(tmp_path.glob(".hermes-tmp.*"))
    assert leftovers == [], f"leaked temp files: {leftovers}"
    # Mode preserved.
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    with open(target, "r", encoding="utf-8") as fh:
        assert fh.read() == "new-content"


def test_native_write_creates_parent_dirs(tmp_path):
    ops, env = _local_ops(tmp_path)
    nested = str(tmp_path / "a" / "b" / "c.txt")
    res = ops.write_file(nested, "deep")
    assert res.error is None
    assert os.path.isfile(nested)
    _assert_no_write_pipeline(env)


def test_native_write_detects_local_by_class(tmp_path):
    # Even without is_local flag, the host-env class/module match triggers it.

    class _HostLike:
        def __init__(self, cwd: str):
            self.cwd = cwd
            self.exec_calls: list = []

        def execute(self, command: str, cwd: str = None, **kwargs):
            self.exec_calls.append(command)
            return {"output": "", "returncode": 0}

    _HostLike.__module__ = "tools.environments.local"
    _HostLike.__name__ = "LocalEnvironment"

    env = _HostLike(cwd=str(tmp_path))
    ops = ShellFileOperations(env, cwd=str(tmp_path))
    res = ops.write_file(str(tmp_path / "x.txt"), "yo")
    assert res.error is None
    _assert_no_write_pipeline(env)


def test_remote_backend_stays_on_shell(tmp_path):
    # A non-local env must take the shell write path and still land the file.
    recorded = []

    class _RemoteBashEnv:
        is_local = False

        def __init__(self, cwd: str):
            self.cwd = cwd

        def execute(self, command: str, cwd: str = None, **kwargs):
            recorded.append(command)
            import subprocess

            proc = subprocess.run(
                ["bash", "-c", command],
                cwd=cwd or self.cwd,
                input=kwargs.get("stdin_data"),
                capture_output=True,
                text=True,
            )
            return {"output": proc.stdout + proc.stderr, "returncode": proc.returncode}

    env = _RemoteBashEnv(cwd=str(tmp_path))
    ops = ShellFileOperations(env, cwd=str(tmp_path))
    target = str(tmp_path / "remote.txt")
    res = ops.write_file(target, "via-shell")
    assert res.bytes_written == len("via-shell".encode("utf-8"))
    assert res.error is None
    assert os.path.isfile(target)
    with open(target, "r", encoding="utf-8") as fh:
        assert fh.read() == "via-shell"
    # The shell write pipeline (mkdir + atomic mv) was exercised.
    assert any("mv -f" in call for call in recorded), "remote path skipped shell write"


def test_native_write_crlf_preserved(tmp_path):
    ops, _ = _local_ops(tmp_path)
    target = str(tmp_path / "win.txt")
    with open(target, "wb") as fh:
        fh.write(b"line1\r\nline2\r\n")
    res = ops.write_file(target, "alpha\r\nbeta\r\n")
    assert res.error is None
    raw = open(target, "rb").read()
    assert raw == b"alpha\r\nbeta\r\n"
