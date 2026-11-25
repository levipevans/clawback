"""Email sender/reply (host-side, allowlisted).

Outbound mail may only go to the owner or an address in the trusted list.
Replies only go back to the original trusted sender, in the same thread.
No attachments, no HTML, no BCC.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage, Message
from email.utils import make_msgid, parseaddr

from ..config import EmailConfig, smtp_settings


class EmailError(RuntimeError):
    pass


def _address(value: str) -> str:
    return parseaddr(value or "")[1].strip().lower()


class Mailer:
    def __init__(self, config: EmailConfig, settings: dict | None):
        self.config = config
        self.settings = settings

    @property
    def from_addr(self) -> str:
        return ((self.settings or {}).get("from_addr") or self.config.address or "").strip().lower()

    def available(self) -> bool:
        return bool(
            self.config.enabled
            and self.from_addr
            and self.settings
            and self.settings.get("host")
        )

    def can_send_to(self, to: str) -> bool:
        address = _address(to)
        return bool(address) and address in self.config.allowed_recipients()

    def describe(self) -> str:
        if not self.config.enabled:
            return "email disabled"
        if not self.settings or not self.settings.get("host"):
            return "email enabled but SMTP environment variables are missing"
        if not self.from_addr:
            return "email enabled but no From address configured"
        if self.config.inbound:
            return (
                f"email {self.from_addr}: outbound + inbound, "
                f"{len(self.config.allowed_recipients())} trusted"
            )
        return (
            f"email {self.from_addr}: outbound -> "
            f"{', '.join(sorted(self.config.allowed_recipients())) or 'nobody'}"
        )

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.available():
            raise EmailError(f"email is not available ({self.describe()})")
        address = _address(to)
        if not self.can_send_to(address):
            raise EmailError(
                f"recipient {to!r} is not the owner or a trusted address; refusing to send"
            )
        return self._send_message(address, subject, body)

    def reply(self, original: Message, body: str) -> dict:
        address = _address(original.get("From", ""))
        if not self.can_send_to(address):
            raise EmailError(f"cannot reply to untrusted address {address!r}")
        subject = str(original.get("Subject", "") or "Gibity Skibbit")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        headers: dict[str, str] = {}
        message_id = str(original.get("Message-ID", "") or "").strip()
        if message_id:
            headers["In-Reply-To"] = message_id
            references = str(original.get("References", "") or "").strip()
            headers["References"] = (
                f"{references} {message_id}".strip() if references else message_id
            )
        return self._send_message(address, subject, body, headers=headers)

    def _send_message(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        subject = subject.strip()
        if not subject:
            raise EmailError("subject must not be empty")
        if len(subject) > 200:
            raise EmailError("subject is too long")
        if len(body.encode("utf-8")) > 20_000:
            raise EmailError("body is too large")

        settings = self.settings or {}
        message = EmailMessage()
        message["From"] = self.from_addr
        message["To"] = to
        message["Subject"] = subject
        domain = self.from_addr.split("@", 1)[-1] if "@" in self.from_addr else None
        message["Message-ID"] = make_msgid(domain=domain)
        for name, value in (headers or {}).items():
            if value:
                message[name] = value
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

        return {"ok": True, "to": to, "subject": subject}


def build_mailer(config: EmailConfig) -> Mailer:
    return Mailer(config, smtp_settings())
