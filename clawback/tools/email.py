"""Email sender (host-side, gated).

The recipient is an allowlist of exactly one address from configuration. Every
call is confirmed by the owner before it is dispatched, and the message is
audited. No attachments, no HTML, no BCC.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..config import EmailConfig, smtp_settings


class EmailError(RuntimeError):
    pass


class Mailer:
    def __init__(self, config: EmailConfig, settings: dict | None):
        self.config = config
        self.settings = settings

    def available(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.recipient
            and self.settings
            and self.settings.get("host")
            and self.settings.get("from_addr")
        )

    def describe(self) -> str:
        if not self.config.enabled:
            return "email disabled"
        if not self.config.recipient:
            return "email enabled but no recipient configured"
        if not self.settings:
            return "email enabled but SMTP environment variables are missing"
        return f"email -> {self.config.recipient}"

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.available():
            raise EmailError(f"email is not available ({self.describe()})")
        if to.strip().lower() != self.config.recipient.strip().lower():
            raise EmailError(
                f"recipient {to!r} is not the configured owner address; refusing to send"
            )
        subject = subject.strip()
        if not subject:
            raise EmailError("subject must not be empty")
        if len(subject) > 200:
            raise EmailError("subject is too long")
        if len(body.encode("utf-8")) > 20_000:
            raise EmailError("body is too large")

        settings = self.settings or {}
        message = EmailMessage()
        message["From"] = settings["from_addr"]
        message["To"] = self.config.recipient
        message["Subject"] = subject
        message.set_content(body)

        security = settings.get("security", "starttls")
        host = settings["host"]
        port = int(settings.get("port", 587))
        user = settings.get("user") or None
        password = settings.get("password") or None

        timeout = 30
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(message)
        elif security == "none":
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(message)

        return {"ok": True, "to": self.config.recipient, "subject": subject}


def build_mailer(config: EmailConfig) -> Mailer:
    return Mailer(config, smtp_settings())
