"""Test doubles shared across test modules."""

from __future__ import annotations

import json

from clawback.sandbox.docker import ExecResult


class FakeSandbox:
    def __init__(self, read_text: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.read_text = read_text

    def exec_ops(self, op, args, *, timeout, max_output_bytes):
        self.calls.append((op, dict(args)))
        if op == "read":
            payload = {"ok": True, "path": args.get("path"), "text": self.read_text, "binary": False}
        elif op == "stat":
            payload = {"ok": True, "path": args.get("path"), "type": "file", "size": 3}
        else:
            payload = {"ok": True, "path": args.get("path")}
        return ExecResult(True, 0, json.dumps(payload), "", duration=0.01)

    def run_bash(self, command, *, timeout, max_output_bytes):
        self.calls.append(("bash", {"command": command}))
        return ExecResult(True, 0, "ok\n", "", duration=0.01)


class FakeMailer:
    def __init__(self, available: bool = True):
        self.sent: list[dict] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def describe(self) -> str:
        return "fake email"

    def send(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "to": to, "subject": subject}
