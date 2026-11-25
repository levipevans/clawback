"""Bounded wake cycles.

A cycle is a fixed budget: wall-clock time, tool calls, model rounds. Only
tasks with `enabled = true` run. At the end of the cycle the assistant goes
back to idle. Scheduled cycles are read-only in practice because nobody is
available to confirm consequential actions.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Callable

from .approvals import ApprovalBroker
from .controller import Status

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

EventFn = Callable[[str], None]


class Scheduler:
    def __init__(self, runtime: "Runtime", *, on_event: EventFn | None = None):
        self.runtime = runtime
        self.on_event = on_event
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cycle_at: float = 0.0
        self._last_cycle_summary: str = ""

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="clawback-scheduler", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause.set()
        if self.runtime.controller.snapshot().status is not Status.RUNNING:
            self.runtime.controller.set(Status.PAUSED, "by owner")
        self.runtime.audit.record("scheduler.pause")

    def resume(self) -> None:
        self._pause.clear()
        if self.runtime.controller.snapshot().status is not Status.RUNNING:
            self.runtime.controller.set(Status.IDLE)
        self.runtime.audit.record("scheduler.resume")

    def stop(self) -> None:
        self._stop.set()
        self.runtime.cancel_event.set()

    def stop_current(self) -> None:
        self.runtime.cancel_event.set()
        self.runtime.audit.record("cycle.stop_requested")

    def _loop(self) -> None:
        if self.paused:
            self.runtime.controller.set(Status.PAUSED, "by owner")
        else:
            self.runtime.controller.set(Status.IDLE)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let the scheduler die silently
                self.runtime.audit.record("scheduler.error", error=f"{type(exc).__name__}: {exc}")
                self.runtime.controller.set(Status.ERROR, f"{type(exc).__name__}: {exc}")
            sleep_seconds = max(1, int(self.runtime.config.cycle_interval_minutes * 60))
            for _ in range(sleep_seconds):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def _tick(self) -> None:
        if self._pause.is_set():
            if self.runtime.controller.snapshot().status is not Status.RUNNING:
                self.runtime.controller.set(Status.PAUSED, "by owner")
            return
        due = self.due_tasks()
        if not due:
            if self.runtime.controller.snapshot().status is not Status.RUNNING:
                self.runtime.controller.set(Status.IDLE)
            return
        if not self.runtime.sandbox_ok:
            self.runtime.audit.record("cycle.skipped", reason="sandbox_unavailable")
            self.runtime.controller.set(Status.SANDBOX_UNAVAILABLE, "tools disabled")
            return
        if not self.runtime.gate.acquire(blocking=False):
            self.runtime.audit.record("cycle.skipped", reason="agent_busy")
            return
        try:
            self._run_cycle(due)
        finally:
            self.runtime.gate.release()
            if self._pause.is_set():
                self.runtime.controller.set(Status.PAUSED, "by owner")
            else:
                self.runtime.controller.set(Status.IDLE)

    def due_tasks(self) -> list:
        now = time.time()
        due = []
        for task in self.runtime.config.tasks:
            if not self.runtime.state.task_enabled(task.name, task.enabled):
                continue
            last = self.runtime.state.last_run(task.name)
            if now - last >= task.every_minutes * 60:
                due.append(task)
        return due

    def _run_cycle(self, due: list) -> None:
        cycle_id = uuid.uuid4().hex[:12]
        self.runtime.cancel_event.clear()
        started = time.monotonic()
        deadline = started + self.runtime.config.budgets.max_seconds
        self._last_cycle_at = time.time()
        self.runtime.controller.set(Status.RUNNING, f"cycle {cycle_id}")
        self.runtime.audit.record(
            "cycle.start", cycle_id=cycle_id, tasks=[t.name for t in due]
        )
        if self.on_event:
            self.on_event(f"scheduled cycle {cycle_id}: {', '.join(t.name for t in due)}")
        ran = 0
        try:
            for task in due:
                if self.runtime.cancel_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    self.runtime.audit.record("cycle.deadline", cycle_id=cycle_id)
                    break
                self.runtime.audit.record("task.start", cycle_id=cycle_id, task=task.name)
                broker = ApprovalBroker(interactive=False, timeout_seconds=1.0)
                result = self.runtime.agent.run(
                    task.prompt,
                    mode="scheduled",
                    cycle_id=f"{cycle_id}:{task.name}",
                    broker=broker,
                    deadline=deadline,
                    on_event=None,
                    cancel=self.runtime.cancel_event,
                )
                self.runtime.state.set_last_run(task.name, time.time())
                ran += 1
                memory = getattr(self.runtime, "memory", None)
                if memory is not None:
                    try:
                        memory.append_interaction(
                            task.prompt,
                            result.text,
                            result.tools_used,
                            mode=f"scheduled:{task.name}",
                        )
                    except OSError:
                        pass
                self.runtime.audit.record(
                    "task.finish",
                    cycle_id=cycle_id,
                    task=task.name,
                    stopped=result.stopped_reason,
                    tool_calls=result.tool_calls,
                    rounds=result.rounds,
                    summary=result.text[:1500],
                )
        finally:
            summary = f"{ran}/{len(due)} tasks"
            self._last_cycle_summary = summary
            self.runtime.audit.record(
                "cycle.finish",
                cycle_id=cycle_id,
                tasks_run=ran,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            if self.on_event:
                self.on_event(f"cycle {cycle_id} finished ({summary})")

    def status_rows(self) -> list[dict]:
        rows = []
        now = time.time()
        for task in self.runtime.config.tasks:
            enabled = self.runtime.state.task_enabled(task.name, task.enabled)
            last = self.runtime.state.last_run(task.name)
            next_due = (last + task.every_minutes * 60) if last else 0.0
            rows.append(
                {
                    "name": task.name,
                    "enabled": enabled,
                    "every_minutes": task.every_minutes,
                    "last_run": last,
                    "next_due_in": max(0.0, next_due - now) if last else 0.0,
                }
            )
        return rows
