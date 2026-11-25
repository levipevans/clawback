from __future__ import annotations

import json

from clawback.approvals import ApprovalResult
from clawback.config import AppConfig
from clawback.memory import MemoryStore
from clawback.policy import Policy, RootGrant
from clawback.state import StateStore
from clawback.tools.dispatch import ToolDispatcher


class ApprovingBroker:
    def request(self, tool, args, preview):
        return ApprovalResult(True, "approved", "req", "digest")


class DenyingBroker:
    def request(self, tool, args, preview):
        return ApprovalResult(False, "owner_denied", "req", "digest")


class FakeSubagentManager:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def start_batch(self, tasks, **kwargs):
        self.calls.append((list(tasks), dict(kwargs)))
        jobs = []
        for index, task in enumerate(tasks, 1):
            job = type("Job", (), {})()
            job.id = f"job{index}"
            job.index = index
            job.total = len(tasks)
            job.task = task
            job.status = "running"
            jobs.append(job)
        return "batch1", jobs

    def wait_batch(self, batch_id, timeout_seconds=60):
        return {"ok": True, "batch_id": batch_id, "done": True, "jobs": []}

    def status(self, batch_id=None):
        return {"ok": True, "running": 0, "jobs": []}

    def running_count(self):
        return 0


def _dispatcher(policy, sandbox, audit, mailer, memory=None, state=None, llm=None, subagent_manager=None):
    return ToolDispatcher(
        policy=policy,
        sandbox=sandbox,
        audit=audit,
        config=AppConfig(),
        mailer=mailer,
        memory=memory,
        state=state,
        llm=llm,
        subagent_manager=subagent_manager,
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


def test_write_is_allowed_without_approval(policy, audit):
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
    assert outcome.ok is True
    assert outcome.decision == "allow"
    writes = [args for op, args in sandbox.calls if op == "write"]
    assert writes and writes[-1]["path"] == "/work/new.txt"


def test_write_is_allowed_in_scheduled_cycles(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "write_file",
        {"path": "/work/new.txt", "content": "hi"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="scheduled",
    )
    assert outcome.decision == "allow"
    assert any(op == "write" for op, _args in sandbox.calls)


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
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "allow"
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


def test_bash_is_refused_in_email_sessions(policy, audit):
    from clawback.inbound import EmailApprovalBroker
    from tests.helpers import FakeMailer, FakeSandbox

    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer())
    outcome = dispatcher.execute(
        "run_bash",
        {"command": "ls"},
        broker=EmailApprovalBroker(FakeMailer()),
        cycle_id="c1",
        mode="email",
    )
    assert outcome.decision == "needs_approval"
    assert sandbox.calls == []


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


def test_memory_tools_are_host_scoped_and_never_touch_the_sandbox(tmp_path, policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    memory = MemoryStore(tmp_path / "memory.md")
    sandbox = FakeSandbox()
    dispatcher = _dispatcher(policy, sandbox, audit, FakeMailer(), memory=memory)

    appended = dispatcher.execute(
        "append_memory",
        {"text": "The owner prefers tea."},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert appended.ok is True
    assert "The owner prefers tea." in memory.read()

    read = dispatcher.execute(
        "read_memory",
        {},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert read.ok is True
    payload = json.loads(read.content)
    assert "The owner prefers tea." in payload["text"]
    assert sandbox.calls == []


def test_append_memory_rejects_empty_text(tmp_path, policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    memory = MemoryStore(tmp_path / "memory.md")
    dispatcher = _dispatcher(policy, FakeSandbox(), audit, FakeMailer(), memory=memory)
    outcome = dispatcher.execute(
        "append_memory",
        {"text": "   "},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "error"
    assert memory.read() == ""


def test_schedule_wakeup_tool_writes_state(tmp_path, policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    state = StateStore(tmp_path / "state.json")
    dispatcher = _dispatcher(policy, FakeSandbox(), audit, FakeMailer(), state=state)
    outcome = dispatcher.execute(
        "schedule_wakeup",
        {"delay_minutes": 5, "prompt": "continue the research"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.ok is True
    payload = json.loads(outcome.content)
    assert payload["id"]
    assert len(state.wakeups()) == 1


def test_web_search_tool_uses_llm(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    class FakeLLM:
        def web_search(self, query, max_results):
            return {
                "ok": True,
                "query": query,
                "summary": "a summary",
                "results": [{"title": "Example", "url": "https://example.com", "snippet": "x"}],
            }

    dispatcher = _dispatcher(policy, FakeSandbox(), audit, FakeMailer(), llm=FakeLLM())
    outcome = dispatcher.execute(
        "web_search",
        {"query": "python"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.ok is True
    payload = json.loads(outcome.content)
    assert payload["results"][0]["url"] == "https://example.com"


def test_web_fetch_rejects_localhost(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    dispatcher = _dispatcher(policy, FakeSandbox(), audit, FakeMailer())
    outcome = dispatcher.execute(
        "web_fetch",
        {"url": "http://127.0.0.1/"},
        broker=DenyingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "error"


def test_spawn_subagents_gets_parent_permissions(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    manager = FakeSubagentManager()
    dispatcher = _dispatcher(
        policy, FakeSandbox(), audit, FakeMailer(), subagent_manager=manager
    )
    outcome = dispatcher.execute(
        "spawn_subagents",
        {"tasks": ["research a", "research b"], "max_seconds": 20},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.ok is True
    tasks, kwargs = manager.calls[0]
    assert tasks == ["research a", "research b"]
    assert kwargs["depth"] == 1
    assert kwargs["permission_mode"] == "interactive"
    assert kwargs["broker"].__class__ is ApprovingBroker
    payload = json.loads(outcome.content)
    assert payload["batch_id"] == "batch1"
    assert len(payload["jobs"]) == 2


def test_spawn_subagents_requires_at_least_two(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    dispatcher = _dispatcher(
        policy, FakeSandbox(), audit, FakeMailer(), subagent_manager=FakeSubagentManager()
    )
    outcome = dispatcher.execute(
        "spawn_subagents",
        {"tasks": ["only one"]},
        broker=ApprovingBroker(),
        cycle_id="c1",
        mode="interactive",
    )
    assert outcome.decision == "error"


def test_spawn_subagents_is_hidden_at_max_depth(policy, audit):
    from tests.helpers import FakeMailer, FakeSandbox

    dispatcher = ToolDispatcher(
        policy=policy,
        sandbox=FakeSandbox(),
        audit=audit,
        config=AppConfig(),
        mailer=FakeMailer(),
        subagent_manager=FakeSubagentManager(),
        depth=AppConfig().budgets.max_subagent_depth,
    )
    names = {spec["function"]["name"] for spec in dispatcher.tool_specs()}
    assert "spawn_subagents" not in names
    assert "wait_subagents" in names
