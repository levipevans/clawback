from __future__ import annotations

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
