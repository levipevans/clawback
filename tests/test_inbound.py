from __future__ import annotations

import threading
from email.message import EmailMessage
from email.mime.text import MIMEText

from clawback.agent import AgentResult
from clawback.audit import AuditLog
from clawback.config import AppConfig, EmailConfig
from clawback.controller import Controller, Status
from clawback.inbound import (
    EmailApprovalBroker,
    EmailInbox,
    Verdict,
    header_trust,
    parse_message,
)
from clawback.memory import MemoryStore
from clawback.state import StateStore


def raw_email(
    *,
    sender: str = "owner@example.com",
    to: str = "clawback@example.com",
    subject: str = "Hello",
    body: str = "Hi there",
    message_id: str = "<m1@example.com>",
    return_path: str | None = "<owner@example.com>",
    sender_header: str | None = None,
    reply_to: str | None = None,
    auth: str | None = "mx.example; spf=pass; dkim=pass; dmarc=pass",
    auto_submitted: str | None = None,
    precedence: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if return_path is not None:
        message["Return-Path"] = return_path
    if sender_header is not None:
        message["Sender"] = sender_header
    if reply_to is not None:
        message["Reply-To"] = reply_to
    if auth is not None:
        message["Authentication-Results"] = auth
    if auto_submitted is not None:
        message["Auto-Submitted"] = auto_submitted
    if precedence is not None:
        message["Precedence"] = precedence
    message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.flags: dict[int, set[str]] = {}
        self.deleted: set[int] = set()
        self.expunged = 0
        self.logged_out = False

    def select(self, mailbox):
        return "OK", [b"1"]

    def search(self, charset, criteria):
        assert criteria == "UNSEEN"
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, uid, spec):
        index = int(uid) - 1
        return "OK", [(b"1 (RFC822)", self.messages[index])]

    def store(self, uid, op, flags):
        self.flags.setdefault(int(uid), set()).add(flags)
        if "\\Deleted" in flags:
            self.deleted.add(int(uid))
        return "OK", [b""]

    def expunge(self):
        self.expunged += 1
        return "OK", [b""]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


class FakeClassifier:
    def __init__(self, verdict: Verdict):
        self.verdict = verdict
        self.calls = 0

    def classify(self, message):
        self.calls += 1
        return self.verdict


class FakeAgent:
    def __init__(self, text: str = "answer", tools=None):
        self.text = text
        self.tools = tools or []
        self.calls: list[tuple[str, dict]] = []

    def run(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return AgentResult(
            self.text,
            "completed",
            rounds=1,
            tool_calls=len(self.tools),
            tools_used=list(self.tools),
        )


class FakeMailer:
    def __init__(self, available: bool = True, can_send: bool = True):
        self._available = available
        self._can_send = can_send
        self.replies: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    def can_send_to(self, to: str) -> bool:
        return self._can_send

    def reply(self, original, body):
        self.replies.append((original.get("Message-ID"), body))
        return {"ok": True}

    def describe(self) -> str:
        return "fake email"


def make_inbox(tmp_path, *, messages, classifier, agent=None, mailer=None, auto_reply=True):
    config = AppConfig(
        include_standard_read_roots=False,
        email=EmailConfig(
            enabled=True,
            address="clawback@example.com",
            recipient="owner@example.com",
            trusted=["owner@example.com"],
            inbound=True,
            poll_minutes=1,
            auto_reply=auto_reply,
        ),
    )
    state = StateStore(tmp_path / "state.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    memory = MemoryStore(tmp_path / "memory.md")
    controller = Controller()
    controller.set(Status.IDLE)
    agent = agent or FakeAgent()
    mailer = mailer or FakeMailer()
    imap = FakeIMAP(messages)
    inbox = EmailInbox(
        config=config,
        state=state,
        audit=audit,
        mailer=mailer,
        agent=agent,
        memory=memory,
        controller=controller,
        gate=threading.Lock(),
        cancel_event=threading.Event(),
        imap={"host": "imap.example.com", "port": 993, "user": "clawback@example.com"},
        imap_factory=lambda settings: imap,
        classifier=classifier,
    )
    return inbox, imap, state, agent, mailer, memory, audit


def test_parse_plain_message():
    message = parse_message(b"1", raw_email(body="Hello body"), 50_000)
    assert message.from_addr == "owner@example.com"
    assert message.subject == "Hello"
    assert message.body == "Hello body"
    assert message.message_id == "<m1@example.com>"
    assert message.return_path == "owner@example.com"


def test_parse_html_only_message():
    raw = MIMEText("<html><body><b>Hello</b> world</body></html>", "html").as_bytes()
    message = parse_message(b"1", raw, 50_000)
    assert "Hello" in message.body
    assert "<b>" not in message.body


def test_header_trust_accepts_matching_headers():
    message = parse_message(b"1", raw_email(), 50_000)
    ok, reason = header_trust(message, {"owner@example.com"})
    assert ok is True
    assert reason == "headers_match"


def test_header_trust_rejects_return_path_mismatch():
    message = parse_message(b"1", raw_email(return_path="<bounce@evil.test>"), 50_000)
    ok, reason = header_trust(message, {"owner@example.com"})
    assert ok is False
    assert reason == "return_path_mismatch"


def test_header_trust_rejects_failed_authentication():
    message = parse_message(
        b"1", raw_email(auth="mx.example; spf=fail; dkim=pass; dmarc=fail"), 50_000
    )
    ok, reason = header_trust(message, {"owner@example.com"})
    assert ok is False
    assert reason == "authentication_dmarc_failed"


def test_trusted_message_runs_session_and_replies(tmp_path):
    inbox, imap, state, agent, mailer, memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(body="Please check /work")],
        classifier=FakeClassifier(Verdict(False, "safe", 0.9)),
    )
    stats = inbox.poll_once()
    assert stats["processed"] == 1
    assert len(agent.calls) == 1
    assert "Please check /work" in agent.calls[0][0]
    assert agent.calls[0][1]["mode"] == "email"
    assert len(mailer.replies) == 1
    assert mailer.replies[0][1] == "answer"
    assert state.is_email_seen("<m1@example.com>") is True
    assert "(\\Seen)" in imap.flags[1]
    assert "email from owner@example.com" in memory.read()


def test_untrusted_malicious_is_deleted_without_running(tmp_path):
    inbox, imap, state, agent, mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(sender="evil@example.com", return_path=None)],
        classifier=FakeClassifier(Verdict(True, "prompt injection", 0.99)),
    )
    stats = inbox.poll_once()
    assert stats["deleted"] == 1
    assert agent.calls == []
    assert mailer.replies == []
    assert 1 in imap.deleted
    assert state.is_email_seen("<m1@example.com>") is True


def test_untrusted_safe_runs_without_replying(tmp_path):
    inbox, _imap, _state, agent, mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(sender="stranger@example.com", return_path=None)],
        classifier=FakeClassifier(Verdict(False, "ordinary question", 0.8)),
    )
    stats = inbox.poll_once()
    assert stats["processed"] == 1
    assert len(agent.calls) == 1
    assert "Trust: screened" in agent.calls[0][0]
    assert mailer.replies == []


def test_classifier_error_fails_closed_without_deleting(tmp_path):
    inbox, imap, _state, agent, mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(sender="stranger@example.com", return_path=None)],
        classifier=FakeClassifier(Verdict(False, "classifier_error", 0.0, ok=False)),
    )
    stats = inbox.poll_once()
    assert stats["errors"] == 1
    assert agent.calls == []
    assert mailer.replies == []
    assert 1 not in imap.deleted
    assert "(\\Seen)" in imap.flags[1]


def test_whitelisted_header_mismatch_goes_through_classifier(tmp_path):
    inbox, _imap, _state, agent, mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(return_path="<bounce@evil.test>")],
        classifier=FakeClassifier(Verdict(False, "safe", 0.7)),
    )
    stats = inbox.poll_once()
    assert stats["processed"] == 1
    assert len(agent.calls) == 1
    assert "Trust: screened" in agent.calls[0][0]
    assert mailer.replies == []


def test_auto_submitted_is_ignored(tmp_path):
    inbox, _imap, _state, agent, _mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email(auto_submitted="auto-replied")],
        classifier=FakeClassifier(Verdict(False, "safe", 0.9)),
    )
    stats = inbox.poll_once()
    assert stats["ignored"] == 1
    assert agent.calls == []


def test_seen_message_is_not_processed_twice(tmp_path):
    inbox, _imap, state, agent, _mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email()],
        classifier=FakeClassifier(Verdict(False, "safe", 0.9)),
    )
    state.add_email_seen("<m1@example.com>")
    stats = inbox.poll_once()
    assert stats["ignored"] == 1
    assert agent.calls == []


def test_auto_reply_can_be_disabled(tmp_path):
    inbox, _imap, _state, agent, mailer, _memory, _audit = make_inbox(
        tmp_path,
        messages=[raw_email()],
        classifier=FakeClassifier(Verdict(False, "safe", 0.9)),
        auto_reply=False,
    )
    stats = inbox.poll_once()
    assert stats["processed"] == 1
    assert len(agent.calls) == 1
    assert mailer.replies == []


def test_email_broker_blocks_unknown_recipients():
    broker = EmailApprovalBroker(FakeMailer(can_send=False))
    result = broker.request("send_email", {"to": "attacker@example.com"}, "")
    assert result.approved is False
    assert result.reason == "recipient_not_allowed"


def test_email_broker_allows_other_tools():
    broker = EmailApprovalBroker(FakeMailer())
    result = broker.request("write_file", {"path": "/work/x"}, "")
    assert result.approved is True
    assert result.reason == "email_auto_approved"
