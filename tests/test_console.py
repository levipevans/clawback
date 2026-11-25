from __future__ import annotations

import threading

from clawback.audit import AuditLog
from clawback.config import AppConfig
from clawback.console import ConsoleUI
from clawback.runtime import Runtime


class FakeRuntime:
    def __init__(self, tmp_path):
        self.agent = object()
        self.config = AppConfig()
        self.cancel_event = threading.Event()
        self.audit = AuditLog(tmp_path / "audit.jsonl")

    def run_interactive(self, *args, **kwargs):
        raise KeyboardInterrupt


def test_first_interrupt_arms_second_quits(tmp_path):
    ui = ConsoleUI(Runtime(profile=tmp_path / "profile"))
    assert ui._should_quit_on_interrupt() is False
    assert ui._should_quit_on_interrupt() is True


def test_chat_interrupt_cancels_then_quits(tmp_path):
    runtime = FakeRuntime(tmp_path)
    ui = ConsoleUI(runtime)

    ui._chat("hello")
    assert runtime.cancel_event.is_set()
    assert ui._quit is False
    assert any(event["event"] == "agent.interrupted" for event in runtime.audit.tail(5))

    ui._chat("again")
    assert ui._quit is True
