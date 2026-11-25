from __future__ import annotations

import pytest

from clawback.policy import (
    Policy,
    PolicyError,
    RootGrant,
    plan_roots,
)


def test_work_is_writable(policy):
    resolved = policy.resolve("/work/notes.txt", for_write=True)
    assert resolved.writable is True
    assert resolved.path == "/work/notes.txt"


def test_read_root_is_read_only(policy):
    resolved = policy.resolve("/ro/documents/note.txt")
    assert resolved.writable is False
    with pytest.raises(PolicyError):
        policy.resolve("/ro/documents/note.txt", for_write=True)


def test_writable_grant_allows_writes(tmp_path, profile):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    policy = Policy(
        sandbox=tmp_path / "work",
        roots=[RootGrant("scratch", scratch, "/rw/scratch", True)],
        hidden=[],
        empty_dir=profile / "runtime" / "empty_dir",
        empty_file=profile / "runtime" / "empty_file",
    )
    resolved = policy.resolve("/rw/scratch/output.txt", for_write=True)
    assert resolved.writable is True
    assert resolved.root == "scratch"
    mounts = {m.container: m for m in policy.mounts()}
    assert mounts["/rw/scratch"].read_only is False


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/work/../../etc/passwd",
        "/work/../ro",
        "relative/path",
        "\\work\\notes.txt",
        "/work/\x00evil",
        "//work/notes.txt",
        "/",
    ],
)
def test_paths_outside_the_policy_are_denied(policy, path):
    with pytest.raises(PolicyError):
        policy.resolve(path)


@pytest.fixture
def allow_tmp_roots(monkeypatch):
    # pytest's tmp_path lives under AppData, which the real predicate rejects.
    monkeypatch.setattr("clawback.policy.is_sensitive_name", lambda path: False)


def test_hidden_folder_is_overlaid_and_blocked(
    tmp_path, profile, read_root, allow_tmp_roots
):
    secret = read_root / "secret"
    secret.mkdir()
    (secret / "private.txt").write_text("nope", encoding="utf-8")

    roots, hidden = plan_roots(
        sandbox=tmp_path / "work",
        include_standard=False,
        extra_read_roots=[str(read_root)],
        extra_write_roots=[],
        hidden_roots=[str(secret)],
    )
    assert [r.slug for r in roots] == ["documents"]
    policy = Policy(
        sandbox=tmp_path / "work",
        roots=roots,
        hidden=hidden,
        empty_dir=profile / "runtime" / "empty_dir",
        empty_file=profile / "runtime" / "empty_file",
    )
    mounts = policy.mounts()
    targets = [m.container for m in mounts]
    assert "/ro/documents/secret" in targets
    with pytest.raises(PolicyError):
        policy.resolve("/ro/documents/secret/private.txt")


def test_hidden_root_is_dropped(tmp_path, allow_tmp_roots):
    root = tmp_path / "documents"
    root.mkdir()
    roots, _hidden = plan_roots(
        sandbox=tmp_path / "work",
        include_standard=False,
        extra_read_roots=[str(root)],
        extra_write_roots=[],
        hidden_roots=[str(root)],
    )
    assert roots == []


def test_sensitive_roots_are_excluded(tmp_path):
    root = tmp_path / ".ssh"
    root.mkdir()
    roots, _ = plan_roots(
        sandbox=tmp_path / "work",
        include_standard=False,
        extra_read_roots=[str(root)],
        extra_write_roots=[],
        hidden_roots=[],
    )
    assert roots == []


def test_missing_extra_root_is_ignored(tmp_path):
    roots, _ = plan_roots(
        sandbox=tmp_path / "work",
        include_standard=False,
        extra_read_roots=[str(tmp_path / "does-not-exist")],
        extra_write_roots=[],
        hidden_roots=[],
    )
    assert roots == []


def test_workspace_is_not_double_mounted(tmp_path, allow_tmp_roots):
    root = tmp_path / "home"
    root.mkdir()
    sandbox = root / "ClawbackWorkspace"
    sandbox.mkdir()
    roots, _ = plan_roots(
        sandbox=sandbox,
        include_standard=False,
        extra_read_roots=[str(root)],
        extra_write_roots=[],
        hidden_roots=[],
    )
    assert roots == []


def test_same_folder_read_and_write_prefers_write(tmp_path, allow_tmp_roots):
    root = tmp_path / "shared"
    root.mkdir()
    roots, _ = plan_roots(
        sandbox=tmp_path / "work",
        include_standard=False,
        extra_read_roots=[str(root)],
        extra_write_roots=[str(root)],
        hidden_roots=[],
    )
    assert len(roots) == 1
    assert roots[0].writable is True
    assert roots[0].container.startswith("/rw/")


def test_mounts_mark_modes(policy):
    mounts = {m.container: m for m in policy.mounts()}
    assert mounts["/work"].read_only is False
    assert mounts["/ro/documents"].read_only is True
