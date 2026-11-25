from __future__ import annotations

import time

from clawback.approvals import ApprovalBroker, action_digest


def test_non_interactive_is_never_auto_approved():
    broker = ApprovalBroker(interactive=False, timeout_seconds=60)
    result = broker.request("write_file", {"path": "/work/x"}, "preview")
    assert result.approved is False
    assert result.reason == "needs_approval"


def test_approval_is_bound_to_the_exact_action():
    broker = ApprovalBroker(interactive=True, timeout_seconds=5)
    seen: dict = {}

    def prompt(request):
        seen["id"] = request.id
        assert broker.approve(request.id, "wrong-digest") is False
        assert broker.approve(request.id, request.digest) is True

    broker.prompt = prompt
    result = broker.request("run_bash", {"command": "ls"}, "preview")
    assert result.approved is True
    assert result.reason == "approved"
    assert result.digest == action_digest("run_bash", {"command": "ls"})


def test_denial_stops_the_action():
    broker = ApprovalBroker(interactive=True, timeout_seconds=5)

    def prompt(request):
        broker.deny(request.id)

    broker.prompt = prompt
    result = broker.request("delete_path", {"path": "/work/x"}, "preview")
    assert result.approved is False
    assert result.reason == "owner_denied"


def test_timeout_is_a_denial():
    broker = ApprovalBroker(interactive=True, timeout_seconds=1)
    started = time.monotonic()
    result = broker.request("write_file", {"path": "/work/x"}, "preview")
    assert result.approved is False
    assert result.reason == "approval_timeout"
    assert time.monotonic() - started >= 0.9
