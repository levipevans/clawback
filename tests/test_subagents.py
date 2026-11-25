from __future__ import annotations

import threading

import pytest

from clawback.subagents import SubagentManager


def test_batch_runs_in_parallel_and_reports_status():
    events: list[str] = []

    def runner(task, max_seconds, parent_deadline, depth, permission_mode, broker):
        return {"ok": True, "text": f"done:{task}", "tools": ["web_search"]}

    manager = SubagentManager(runner, on_event=events.append)
    batch_id, jobs = manager.start_batch(
        ["alpha", "beta"],
        max_seconds=10,
        parent_deadline=None,
        depth=1,
        permission_mode="interactive",
        broker=None,
    )
    assert len(jobs) == 2
    assert any("started" in event for event in events)

    result = manager.wait_batch(batch_id, 5)
    assert result["done"] is True
    assert [job["status"] for job in result["jobs"]] == ["done", "done"]
    assert [job["result"] for job in result["jobs"]] == ["done:alpha", "done:beta"]
    assert any("finished" in event for event in events)


def test_wait_all_accepts_timeout_keyword():
    manager = SubagentManager(
        lambda *args, **kwargs: {"ok": True, "text": "", "tools": []}
    )
    manager.wait_all(timeout_seconds=0.1)


def test_manager_blocks_when_max_running_is_exceeded():
    gate = threading.Event()

    def runner(task, max_seconds, parent_deadline, depth, permission_mode, broker):
        gate.wait(3)
        return {"ok": True, "text": task, "tools": []}

    manager = SubagentManager(runner, max_running=2)
    batch_id, _ = manager.start_batch(
        ["a", "b"],
        max_seconds=10,
        parent_deadline=None,
        depth=1,
        permission_mode="interactive",
        broker=None,
    )
    with pytest.raises(ValueError):
        manager.start_batch(
            ["c"],
            max_seconds=10,
            parent_deadline=None,
            depth=1,
            permission_mode="interactive",
            broker=None,
        )
    gate.set()
    result = manager.wait_batch(batch_id, 5)
    assert result["done"] is True
