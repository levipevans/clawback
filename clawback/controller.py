"""Shared status, visible from the tray and the terminal."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Status(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    SANDBOX_UNAVAILABLE = "sandbox unavailable"


@dataclass(frozen=True)
class Snapshot:
    status: Status
    detail: str
    changed_at: float


class Controller:
    def __init__(self):
        self._lock = threading.RLock()
        self._snapshot = Snapshot(Status.STARTING, "", time.time())
        self._listeners: list[Callable[[Snapshot], None]] = []

    def set(self, status: Status, detail: str = "") -> None:
        with self._lock:
            if self._snapshot.status == status and self._snapshot.detail == detail:
                return
            self._snapshot = Snapshot(status, detail, time.time())
            listeners = list(self._listeners)
            snapshot = self._snapshot
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                pass

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: Callable[[Snapshot], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def label(self) -> str:
        snap = self.snapshot()
        base = snap.status.value
        if snap.detail:
            return f"{base} - {snap.detail}"
        return base
