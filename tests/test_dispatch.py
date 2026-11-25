from __future__ import annotations

from clawback.approvals import ApprovalResult
from clawback.config import AppConfig
from clawback.policy import Policy, RootGrant
from clawback.tools.dispatch import ToolDispatcher


class ApprovingBroker:
    def request(self, tool, args, preview):
        return ApprovalResult(True, "approved", "req", "digest")


class DenyingBroker:
    def request(self, tool, args, preview):
        return ApprovalResult(False, "owner_denied", "req", "digest")


def _dispatcher(policy, sandbox, audit, mailer):
    return ToolDispatcher(
        policy=policy,
        sandbox=sandbox,
        audit=audit,
        config=AppConfig(),
        mailer=mailer,
        on_event=None,
    )


def test_read_is_allowed_without_approval(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox(read_text="hello")
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "read_file",
        {"path": "/ro/documents/note.txt"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.ok is True
    assert outcome.decision == "allow"
    assert sandbox.calls[-1] == ("read", {"path": "/ro/documents/note.txt"})


def test_write_requires_approval(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/work/new.txt", "content": "hi"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "denied"
    assert not any(op == "write" for op, _args in sandbox.calls)


def test_approved_write_executes(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/work/new.txt", "content": "hi"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "approved"
    writes = [args for op, args in sandbox.calls if op == "write"]
    assert writes and writes[-1]["path"] == "/work/new.txt"


def test_traversal_is_denied_before_the_sandbox(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "read_file",
        {"path": "/work/../../etc/passwd"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "denied"
    assert sandbox.calls == []


def test_write_to_read_only_root_is_denied(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/ro/documents/x.txt", "content": "hi"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "denied"
    assert sandbox.calls == []


def test_invalid_arguments_are_rejected(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/work/x.txt"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "error"
    assert sandbox.calls == []


def test_write_to_writable_grant_executes(tmp_path, profile, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    policy = Policy(
        sandbox=tmp_path / "work",
        roots=[RootGrant("scratch", scratch, "/rw/scratch", True)],
        hidden=[],
        empty_dir=profile / "runtime" / "empty_dir",
        empty_file=profile / "runtime" / "empty_file",
    )
    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/rw/scratch/new.txt", "content": "hi"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "approved"
    writes = [args for op, args in sandbox.calls if op == "write"]
    assert writes and writes[-1]["path"] == "/rw/scratch/new.txt"


def test_bash_requires_approval(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    denied = dispatcher.execute(
        "run_bash",
        {"command": "ls"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="scheduled",
    )
    assert denied.decision == "needs_approval"
    assert sandbox.calls == []

    approved = dispatcher.execute(
        "run_bash",
        {"command": "ls"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert approved.decision == "approved"
    assert sandbox.calls[-1] == ("bash", {"command": "ls"})


def test_email_is_allowlisted_and_audited(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    mailer = FakeMailer()
    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, mailer)
    outcome = dispatcher.execute(
        "send_email",
        {"to": "owner@example.com", "subject": "hi", "body": "hello"},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "approved"
    assert len(mailer.sent) == 1
    assert mailer.sent[0]["to"] == "owner@example.com"
    assert any(e["event"] == "tool.call" for e in audit.tail(10))


def test_every_tool_call_is_audited(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    dispatcher = _dispatcher(policy, FakeSandbox(), audit, FakeMailer())
    dispatcher.execute(
        "list_dir",
        {"path": "/work"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    entry = [e for e in audit.tail(10) if e["event"] == "tool.call"][-1]
    assert entry["tool"] == "list_dir"
    assert entry["decision"] == "allow"
    assert entry["ok"] is True
