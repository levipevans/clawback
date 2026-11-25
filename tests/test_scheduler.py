from __future__ import annotations

import threading
import time

from clawback.agent import AgentResult
from clawback.audit import AuditLog
from clawback.config import AppConfig, Budgets, TaskConfig
from clawback.controller import Controller, Status
from clawback.scheduler import Scheduler
from clawback.state import StateStore


class FakeAgent:
    def __init__(self):
        self.runs: list[tuple[str, str]] = []
        self.deadlines: list[float | None] = []

    def run(self, prompt, *, mode, cycle_id, broker, deadline=None, on_event=None, cancel=None):
        self.runs.append((prompt, mode))
        self.deadlines.append(deadline)
        return AgentResult("done", "completed", rounds=1, tool_calls=0)


class FakeRuntime:
    def __init__(self, tmp_path, tasks, sandbox_ok=True):
        self.config = AppConfig(tasks=tasks, budgets=Budgets(max_seconds=5))
        self.state = StateStore(tmp_path / "state.json")
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.controller = Controller()
        self.controller.set(Status.IDLE)
        self.gate = threading.Lock()
        self.cancel_event = threading.Event()
        self.sandbox_ok = sandbox_ok
        self.agent = FakeAgent()


def _task(name="digest", enabled=True, every=60):
    return TaskConfig(name=name, enabled=enabled, every_minutes=every, prompt=f"do {name}")


def test_only_enabled_tasks_run(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task("on", True), _task("off", False)])
    scheduler = Scheduler(runtime)
    scheduler._tick()
    assert runtime.agent.runs == [("do on", "scheduled")]
    assert runtime.state.last_run("on") > 0
    assert runtime.state.last_run("off") == 0


def test_task_is_not_due_until_interval_passes(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task()])
    runtime.state.set_last_run("digest", time.time())
    scheduler = Scheduler(runtime)
    assert scheduler.due_tasks() == []
    assert runtime.agent.runs == []


def test_due_task_gets_the_cycle_deadline(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task()])
    scheduler = Scheduler(runtime)
    before = time.monotonic()
    scheduler._tick()
    assert runtime.agent.deadlines[0] is not None
    assert before <= runtime.agent.deadlines[0] <= before + 6


def test_pause_prevents_cycles(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task()])
    scheduler = Scheduler(runtime)
    scheduler.pause()
    scheduler._tick()
    assert runtime.agent.runs == []
    assert runtime.controller.snapshot().status is Status.PAUSED


def test_sandbox_unavailable_skips_cycles(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task()], sandbox_ok=False)
    scheduler = Scheduler(runtime)
    scheduler._tick()
    assert runtime.agent.runs == []
    assert runtime.controller.snapshot().status is Status.SANDBOX_UNAVAILABLE
    assert any(e["event"] == "cycle.skipped" for e in runtime.audit.tail(10))


def test_cycle_is_audited(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task()])
    Scheduler(runtime)._tick()
    events = [e["event"] for e in runtime.audit.tail(20)]
    assert "cycle.start" in events
    assert "task.start" in events
    assert "task.finish" in events
    assert "cycle.finish" in events


def test_two_due_tasks_run_in_one_cycle(tmp_path):
    runtime = FakeRuntime(tmp_path, [_task("a"), _task("b")])
    Scheduler(runtime)._tick()
    assert [p for p, _m in runtime.agent.runs] == ["do a", "do b"]
