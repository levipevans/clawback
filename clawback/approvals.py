"""Confirmation gate for consequential actions.

An approval is bound to the exact tool name and arguments by a SHA-256 digest.
Approving one action cannot authorize a different one, and an approval that
times out is a denial.

Scheduled cycles run non-interactively: anything consequential is refused and
recorded as `needs_approval`. There is no "auto-yes because nobody was
looking" path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


def action_digest(tool: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    args: dict
    preview: str
    digest: str
    created_at: float
    expires_at: float
    decision: str | None = None
    decided_at: float | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_open(self) -> bool:
        return self.decision is None and time.time() < self.expires_at


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    reason: str
    request_id: str | None = None
    digest: str | None = None


PromptFn = Callable[[ApprovalRequest], None]


class ApprovalBroker:
    def __init__(
        self,
        *,
        interactive: bool,
        timeout_seconds: float,
        prompt: PromptFn | None = None,
    ):
        self.interactive = interactive
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.prompt = prompt
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def request(self, tool: str, args: dict, preview: str) -> ApprovalResult:
        digest = action_digest(tool, args)
        request_id = secrets.token_hex(4)
        now = time.time()
        req = ApprovalRequest(
            id=request_id,
            tool=tool,
            args=args,
            preview=preview,
            digest=digest,
            created_at=now,
            expires_at=now + self.timeout_seconds,
        )
        with self._lock:
            self._requests[request_id] = req

        if not self.interactive:
            req.decision = "deferred"
            req.decided_at = now
            return ApprovalResult(
                approved=False,
                reason="needs_approval",
                request_id=request_id,
                digest=digest,
            )

        if self.prompt is not None:
            try:
                self.prompt(req)
            except Exception:
                pass

        req._event.wait(timeout=self.timeout_seconds)
        with self._lock:
            if req.decision is None:
                req.decision = "timeout"
                req.decided_at = time.time()
                return ApprovalResult(
                    approved=False,
                    reason="approval_timeout",
                    request_id=request_id,
                    digest=digest,
                )
            if req.decision == "approved":
                return ApprovalResult(
                    approved=True,
                    reason="approved",
                    request_id=request_id,
                    digest=digest,
                )
            return ApprovalResult(
                approved=False,
                reason=f"owner_{req.decision}",
                request_id=request_id,
                digest=digest,
            )

    def approve(self, request_id: str, digest: str | None = None) -> bool:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or not req.is_open():
                return False
            if digest is not None and digest != req.digest:
                return False
            req.decision = "approved"
            req.decided_at = time.time()
            req._event.set()
            return True

    def deny(self, request_id: str) -> bool:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or not req.is_open():
                return False
            req.decision = "denied"
            req.decided_at = time.time()
            req._event.set()
            return True

    def open_requests(self) -> list[ApprovalRequest]:
        now = time.time()
        with self._lock:
            return [r for r in self._requests.values() if r.decision is None and now < r.expires_at]

    def prune(self, older_than_seconds: float = 600) -> None:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            stale = [k for k, r in self._requests.items() if r.decided_at and r.decided_at < cutoff]
            for key in stale:
                self._requests.pop(key, None)
