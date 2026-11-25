from __future__ import annotations

import pytest

from clawback.audit import AuditLog
from clawback.config import AppConfig
from clawback.policy import Policy, RootGrant


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "profile"
    (root / "workspace").mkdir(parents=True)
    (root / "runtime" / "empty_dir").mkdir(parents=True)
    empty_file = root / "runtime" / "empty_file"
    empty_file.write_bytes(b"")
    return root


@pytest.fixture
def audit(profile):
    return AuditLog(profile / "audit.jsonl")


@pytest.fixture
def config():
    return AppConfig()


@pytest.fixture
def read_root(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")
    return root


@pytest.fixture
def policy(tmp_path, read_root, profile):
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    return Policy(
        sandbox=sandbox,
        roots=[RootGrant("documents", read_root, "/ro/documents", False)],
        hidden=[],
        empty_dir=profile / "runtime" / "empty_dir",
        empty_file=profile / "runtime" / "empty_file",
    )
