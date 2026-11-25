from __future__ import annotations

from email.message import EmailMessage

import pytest

from clawback.config import EmailConfig
from clawback.tools.email import EmailError, Mailer

_SETTINGS = {
    "host": "smtp.example.com",
    "port": 587,
    "user": "",
    "password": "",
    "from_addr": "bot@example.com",
    "security": "starttls",
}


def test_disabled_mailer_is_unavailable():
    mailer = Mailer(EmailConfig(), None)
    assert mailer.available() is False
    with pytest.raises(EmailError):
        mailer.send("owner@example.com", "hi", "body")


def test_recipient_must_match_the_owner():
    mailer = Mailer(EmailConfig(enabled=True, recipient="owner@example.com"), _SETTINGS)
    assert mailer.available() is True
    with pytest.raises(EmailError) as excinfo:
        mailer.send("someone-else@example.com", "hi", "body")
    assert "owner" in str(excinfo.value)


def test_empty_subject_is_refused():
    mailer = Mailer(EmailConfig(enabled=True, recipient="owner@example.com"), _SETTINGS)
    with pytest.raises(EmailError):
        mailer.send("owner@example.com", "   ", "body")


def test_oversized_body_is_refused():
    mailer = Mailer(EmailConfig(enabled=True, recipient="owner@example.com"), _SETTINGS)
    with pytest.raises(EmailError):
        mailer.send("owner@example.com", "hi", "x" * 20_001)


def test_trusted_recipients_are_allowed():
    mailer = Mailer(
        EmailConfig(
            enabled=True,
            recipient="owner@example.com",
            trusted=["friend@example.com"],
        ),
        _SETTINGS,
    )
    assert mailer.can_send_to("owner@example.com") is True
    assert mailer.can_send_to("friend@example.com") is True
    assert mailer.can_send_to("stranger@example.com") is False


def test_reply_threads_and_subjects(monkeypatch):
    mailer = Mailer(
        EmailConfig(
            enabled=True,
            recipient="owner@example.com",
            trusted=["friend@example.com"],
        ),
        _SETTINGS,
    )
    original = EmailMessage()
    original["From"] = "friend@example.com"
    original["Subject"] = "Question"
    original["Message-ID"] = "<abc@example.com>"
    original["References"] = "<root@example.com>"

    captured: dict = {}

    def fake_send(to, subject, body, *, headers=None):
        captured.update({"to": to, "subject": subject, "body": body, "headers": headers})
        return {"ok": True}

    monkeypatch.setattr(mailer, "_send_message", fake_send)
    mailer.reply(original, "Answer")
    assert captured["to"] == "friend@example.com"
    assert captured["subject"] == "Re: Question"
    assert captured["headers"]["In-Reply-To"] == "<abc@example.com>"
    assert "<abc@example.com>" in captured["headers"]["References"]


def test_reply_to_untrusted_is_refused(monkeypatch):
    mailer = Mailer(EmailConfig(enabled=True, recipient="owner@example.com"), _SETTINGS)
    original = EmailMessage()
    original["From"] = "stranger@example.com"
    original["Subject"] = "Question"
    with pytest.raises(EmailError):
        mailer.reply(original, "Answer")
