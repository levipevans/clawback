"""Integration tests for the real Docker boundary.

These are skipped when Docker is unavailable. They prove the properties the
rest of the design relies on: the host filesystem is absent, the read-only
roots really are read-only, the process is non-root, and there is no network.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from clawback.policy import Policy, RootGrant
from clawback.sandbox.docker import DockerSandbox


def _docker_ready() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="Docker is not available")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    base = tmp_path_factory.mktemp("sandbox-it")
    work = base / "work"
    work.mkdir()
    readonly = base / "readonly"
    readonly.mkdir()
    (readonly / "note.txt").write_text("hello", encoding="utf-8")
    (readonly / "hidden").mkdir()
    (readonly / "hidden" / "secret.txt").write_text("secret", encoding="utf-8")
    writable = base / "writable"
    writable.mkdir()
    (writable / "existing.txt").write_text("before", encoding="utf-8")
    empty_dir = base / "empty_dir"
    empty_dir.mkdir()
    empty_file = base / "empty_file"
    empty_file.write_bytes(b"")

    policy = Policy(
        sandbox=work,
        roots=[
            RootGrant("documents", readonly, "/ro/documents", False),
            RootGrant("scratch", writable, "/rw/scratch", True),
        ],
        hidden=[readonly / "hidden"],
        empty_dir=empty_dir,
        empty_file=empty_file,
    )
    box = DockerSandbox(policy, container_name="clawback-test-sandbox")
    box.start()
    try:
        yield box
    finally:
        box.stop()


def _bash(sandbox, command, timeout=20):
    return sandbox.run_bash(command, timeout=timeout, max_output_bytes=50_000)


def test_runs_as_non_root(sandbox):
    result = _bash(sandbox, "id -u")
    assert result.ok is True
    assert result.stdout.strip() == "1000"


def test_work_is_writable(sandbox):
    result = _bash(sandbox, "echo hi > /work/probe.txt && cat /work/probe.txt")
    assert result.ok is True
    assert "hi" in result.stdout


def test_read_root_is_visible_but_read_only(sandbox):
    read = _bash(sandbox, "cat /ro/documents/note.txt")
    assert read.ok is True
    assert "hello" in read.stdout

    write = _bash(sandbox, "touch /ro/documents/probe.txt")
    assert write.ok is False
    assert "Read-only file system" in (write.stderr + write.stdout)


def test_host_filesystem_is_absent(sandbox):
    result = _bash(sandbox, "ls /mnt/c /host_mnt /Users C:/ 2>&1")
    assert result.ok is False
    assert "No such file or directory" in result.stdout


def test_network_is_unreachable(sandbox):
    command = (
        "python3 -c \"import socket; socket.setdefaulttimeout(3); "
        "socket.create_connection(('1.1.1.1', 80))\""
    )
    result = _bash(sandbox, command)
    assert result.ok is False
    combined = result.stderr + result.stdout
    assert "Network is unreachable" in combined or "timed out" in combined.lower()


def test_hidden_folder_is_masked(sandbox):
    read = _bash(sandbox, "cat /ro/documents/hidden/secret.txt")
    assert read.ok is False
    listing = _bash(sandbox, "ls -A /ro/documents/hidden")
    assert listing.ok is True
    assert listing.stdout.strip() == ""


def test_writable_grant_is_writable(sandbox):
    result = _bash(sandbox, "echo after > /rw/scratch/new.txt && cat /rw/scratch/new.txt")
    assert result.ok is True
    assert "after" in result.stdout

    written = sandbox.exec_ops(
        "write", {"path": "/rw/scratch/ops.txt", "content": "from ops"}, timeout=20, max_output_bytes=10_000
    )
    assert written.ok is True
    read_back = _bash(sandbox, "cat /rw/scratch/ops.txt")
    assert "from ops" in read_back.stdout


def test_writable_grant_is_readable(sandbox):
    result = _bash(sandbox, "cat /rw/scratch/existing.txt")
    assert result.ok is True
    assert "before" in result.stdout


def test_structured_ops_can_read_read_roots(sandbox):
    result = sandbox.exec_ops(
        "read", {"path": "/ro/documents/note.txt"}, timeout=20, max_output_bytes=10_000
    )
    assert result.ok is True
    assert "hello" in result.stdout


def test_delete_in_writable_grant_uses_local_trash(sandbox):
    written = sandbox.exec_ops(
        "write", {"path": "/rw/scratch/del.txt", "content": "x"}, timeout=20, max_output_bytes=10_000
    )
    assert written.ok is True
    deleted = sandbox.exec_ops(
        "delete", {"path": "/rw/scratch/del.txt"}, timeout=20, max_output_bytes=10_000
    )
    assert deleted.ok is True
    assert ".clawback-trash" in deleted.stdout
    gone = _bash(sandbox, "test -e /rw/scratch/del.txt")
    assert gone.ok is False
    trashed = _bash(sandbox, "ls /rw/scratch/.clawback-trash")
    assert "del.txt" in trashed.stdout


def test_structured_ops_cannot_traverse(sandbox):
    result = sandbox.exec_ops(
        "read", {"path": "/work/../etc/passwd"}, timeout=20, max_output_bytes=10_000
    )
    assert result.ok is False
    assert "outside /work and /ro" in result.stdout


def test_structured_ops_cannot_write_read_roots(sandbox):
    result = sandbox.exec_ops(
        "write",
        {"path": "/ro/documents/nope.txt", "content": "x"},
        timeout=20,
        max_output_bytes=10_000,
    )
    assert result.ok is False
    assert "only /work and writable grants" in result.stdout


def test_structured_ops_work(sandbox):
    written = sandbox.exec_ops(
        "write", {"path": "/work/ops.txt", "content": "from ops"}, timeout=20, max_output_bytes=10_000
    )
    assert written.ok is True
    read = sandbox.exec_ops("read", {"path": "/work/ops.txt"}, timeout=20, max_output_bytes=10_000)
    assert read.ok is True
    assert "from ops" in read.stdout
