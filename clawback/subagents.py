"""Parallel sub-agent batches.

`spawn_subagents` returns immediately; each task runs in its own daemon thread.
`wait_subagents` joins a batch when the parent actually needs the results.
`/subagents` shows one line per job with live status.

The runner is provided by Runtime so sub-agents inherit the parent dispatcher's
policy, sandbox, approval broker, and depth.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

Runner = Callable[[str, int, float | None, int, str, object], dict]
EventFn = Callable[[str], None]


@dataclass
class SubagentJob:
    id: str
    batch_id: str
    index: int
    total: int
    task: str
    depth: int
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: str = ""
    tools: list[str] = field(default_factory=list)
    error: str = ""
    thread: threading.Thread | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        finished = self.finished_at or time.time()
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "index": self.index,
            "total": self.total,
            "task": self.task,
            "depth": self.depth,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(max(0.0, finished - self.started_at), 2),
            "result": self.result[:2000],
            "tools": list(self.tools),
            "error": self.error,
        }


class SubagentManager:
    def __init__(
        self,
        runner: Runner,
        *,
        on_event: EventFn | None = None,
        max_running: int = 12,
    ):
        self.runner = runner
        self.on_event = on_event
        self.max_running = max(1, int(max_running))
        self._lock = threading.RLock()
        self._jobs: dict[str, SubagentJob] = {}
        self._batches: dict[str, list[str]] = {}

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status == "running")

    def start_batch(
        self,
        tasks: list[str],
        *,
        max_seconds: int,
        parent_deadline: float | None,
        depth: int,
        permission_mode: str,
        broker,
    ) -> tuple[str, list[SubagentJob]]:
        cleaned = [str(task).strip()[:4000] for task in tasks if str(task).strip()]
        if not cleaned:
            raise ValueError("at least one task is required")
        with self._lock:
            if self.running_count() + len(cleaned) > self.max_running:
                raise ValueError("too many sub-agents running")
        batch_id = uuid.uuid4().hex[:8]
        jobs: list[SubagentJob] = []
        threads: list[threading.Thread] = []
        for index, task in enumerate(cleaned, 1):
            job = SubagentJob(
                id=uuid.uuid4().hex[:8],
                batch_id=batch_id,
                index=index,
                total=len(cleaned),
                task=task,
                depth=depth,
            )
            thread = threading.Thread(
                target=self._run_job,
                args=(job, max_seconds, parent_deadline, depth, permission_mode, broker),
                name=f"clawback-subagent-{job.id}",
                daemon=True,
            )
            job.thread = thread
            with self._lock:
                self._jobs[job.id] = job
                self._batches.setdefault(batch_id, []).append(job.id)
            jobs.append(job)
            threads.append(thread)
            if self.on_event:
                self.on_event(f"subagent {index}/{len(cleaned)} started: {task[:100]}")
        for thread in threads:
            thread.start()
        return batch_id, jobs

    def _run_job(
        self,
        job: SubagentJob,
        max_seconds: int,
        parent_deadline: float | None,
        depth: int,
        permission_mode: str,
        broker,
    ) -> None:
        try:
            result = self.runner(
                job.task, max_seconds, parent_deadline, depth, permission_mode, broker
            )
            if not isinstance(result, dict):
                result = {"ok": True, "text": str(result), "tools": []}
            with self._lock:
                job.status = "done" if result.get("ok", True) else "error"
                job.result = str(result.get("text", ""))
                job.tools = list(result.get("tools", []))
                job.error = str(result.get("error", ""))
        except Exception as exc:
            with self._lock:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                job.finished_at = time.time()
                batch_done = self._batch_done_locked(job.batch_id)
            if self.on_event and batch_done:
                self.on_event(f"subagent batch {job.batch_id} finished")

    def _batch_done_locked(self, batch_id: str) -> bool:
        jobs = self._batches.get(batch_id, [])
        return bool(jobs) and all(self._jobs[job_id].status != "running" for job_id in jobs)

    def batch_status(self, batch_id: str) -> dict:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._batches.get(batch_id, [])]
            return {
                "ok": True,
                "batch_id": batch_id,
                "done": all(job.status != "running" for job in jobs),
                "jobs": [job.as_dict() for job in jobs],
            }

    def wait_batch(self, batch_id: str, timeout_seconds: float = 60.0) -> dict:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._batches.get(batch_id, [])]
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for job in jobs:
            if job.thread is not None:
                remaining = max(0.0, deadline - time.monotonic())
                job.thread.join(timeout=remaining)
        return self.batch_status(batch_id)

    def status(self, batch_id: str | None = None) -> dict:
        if batch_id:
            return self.batch_status(batch_id)
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.started_at)[-20:]
            return {
                "ok": True,
                "running": sum(1 for job in jobs if job.status == "running"),
                "jobs": [job.as_dict() for job in jobs],
            }

    def wait_all(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.status == "running"]
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for job in jobs:
            if job.thread is not None:
                remaining = max(0.0, deadline - time.monotonic())
                job.thread.join(timeout=remaining)
