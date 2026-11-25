from __future__ import annotations

import json

from clawback.audit import AuditLog


def test_records_and_tails(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("cycle.start", cycle_id="abc")
    audit.record("tool.call", tool="read_file", ok=True)
    entries = audit.tail(10)
    assert [e["event"] for e in entries] == ["cycle.start", "tool.call"]
    assert entries[1]["tool"] == "read_file"
    assert "ts" in entries[0]


def test_secrets_are_redacted(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record(
        "tool.call",
        api_key="sk-or-secret",
        nested={"password": "hunter2", "safe": "yes"},
        headers={"Authorization": "Bearer abc"},
    )
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-or-secret" not in raw
    assert "hunter2" not in raw
    assert "Bearer abc" not in raw
    entry = json.loads(raw.splitlines()[0])
    assert entry["api_key"] == "***REDACTED***"
    assert entry["nested"]["safe"] == "yes"


def test_long_strings_are_truncated(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("tool.call", body="x" * 10_000)
    entry = audit.tail(1)[0]
    assert len(entry["body"]) < 5000


def test_wipe_removes_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.record("x")
    assert path.exists()
    audit.wipe()
    assert not path.exists()
    assert audit.tail(5) == []
