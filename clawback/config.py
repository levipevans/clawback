"""Configuration: safe defaults, secrets only from the environment."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
CONFIG_FILENAME = "config.toml"

DEFAULT_CONFIG = '''\
# Clawback configuration.
#
# Secrets are NEVER stored in this file. They come from environment variables:
#   OPENROUTER_API_KEY          required for the model
#   CLAWBACK_SMTP_HOST          optional, for the gated email tool
#   CLAWBACK_SMTP_PORT          default 587
#   CLAWBACK_SMTP_USER
#   CLAWBACK_SMTP_PASSWORD
#   CLAWBACK_SMTP_FROM
#   CLAWBACK_SMTP_SECURITY      starttls | ssl
#   CLAWBACK_IMAP_HOST          optional, for inbound mail
#   CLAWBACK_IMAP_PORT          default 993
#   CLAWBACK_IMAP_USER
#   CLAWBACK_IMAP_PASSWORD
#   CLAWBACK_IMAP_SECURITY      ssl | starttls | none
#
# The model can only see the Docker sandbox. `sandbox_dir` is the ONE folder
# it may write to. Leave it unset to use <profile>\\workspace.

model = "deepseek/deepseek-v4.1-flash-1017"

# Mount the standard user folders (Desktop, Documents, Downloads, Pictures,
# Music, Videos) read-only. Dotfiles and AppData are always excluded.
include_standard_read_roots = true

# Additional folders to mount read-only, e.g. "D:\\\\Research".
extra_read_roots = []

# Additional folders to mount read-write. Every write still requires your
# confirmation, but adding a folder here makes it possible at all.
extra_write_roots = []

# Folders to hide even if they would otherwise be mounted. Hiding is enforced
# with an empty read-only mount, not a path check.
hidden_roots = []

cycle_interval_minutes = 30
interactive_approval_timeout_seconds = 120

[budgets]
# Hard limits for one wake cycle. When any limit is hit the cycle stops.
max_seconds = 0
max_tool_calls = 200
max_model_rounds = 200
tool_timeout_seconds = 120
max_tool_output_bytes = 1000000
# Research helpers
max_subagents = 20
max_subagent_depth = 5
subagent_seconds = 600
max_running_subagents = 32

[email]
# Outbound email is gated to the owner and the trusted list below.
# Inbound email polls IMAP, screens unknown senders, and can reply.
enabled = false

# The assistant's own mailbox (used as From / IMAP user).
address = ""

# The owner. Always trusted; replies go here.
recipient = ""

# Additional senders treated as trusted (no classifier pass).
trusted = []

# Poll IMAP for incoming mail.
inbound = false
poll_minutes = 5

# Require From / Return-Path / Sender / Reply-To consistency for trusted mail.
require_header_match = true

# Delete mail the classifier marks malicious. If false it is left in the inbox.
delete_malicious = true

# Reply automatically to trusted senders. Screened senders never get a reply.
auto_reply = true

max_body_bytes = 50000

# Scheduled tasks are opt-in. A task without `enabled = true` never runs.
[[tasks]]
name = "workspace-check"
enabled = false
every_minutes = 60
prompt = "List the files in /work and report anything that looks unfinished. Do not modify anything."
'''


class ConfigError(RuntimeError):
    pass


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_seconds: int = Field(default=0, ge=0, le=86_400)
    max_tool_calls: int = Field(default=200, ge=0, le=5_000)
    max_model_rounds: int = Field(default=200, ge=0, le=5_000)
    tool_timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    max_tool_output_bytes: int = Field(default=1_000_000, ge=1_000, le=20_000_000)
    max_subagents: int = Field(default=20, ge=1, le=100)
    max_subagent_depth: int = Field(default=5, ge=1, le=20)
    subagent_seconds: int = Field(default=600, ge=10, le=3_600)
    max_running_subagents: int = Field(default=32, ge=1, le=256)


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    every_minutes: int = Field(default=60, ge=1, le=10_080)
    prompt: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _simple_name(cls, value: str) -> str:
        if not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("task name may only contain letters, digits, '-' and '_'")
        return value


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    address: str = ""
    recipient: str = ""
    trusted: list[str] = Field(default_factory=list)
    inbound: bool = False
    poll_minutes: int = Field(default=5, ge=1, le=1440)
    require_header_match: bool = True
    delete_malicious: bool = True
    auto_reply: bool = True
    max_body_bytes: int = Field(default=50_000, ge=1_000, le=1_000_000)

    @field_validator("recipient", "address")
    @classmethod
    def _looks_like_address(cls, value: str) -> str:
        value = value.strip().lower()
        if value and "@" not in value:
            raise ValueError("must look like an email address")
        return value

    @field_validator("trusted")
    @classmethod
    def _trusted_addresses(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            address = value.strip().lower()
            if not address:
                continue
            if "@" not in address:
                raise ValueError(f"trusted address is invalid: {value!r}")
            if address not in out:
                out.append(address)
        return out

    def allowed_recipients(self) -> set[str]:
        values = set(self.trusted)
        if self.recipient:
            values.add(self.recipient)
        return values


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = DEFAULT_MODEL
    sandbox_dir: str | None = None
    include_standard_read_roots: bool = True
    extra_read_roots: list[str] = Field(default_factory=list)
    extra_write_roots: list[str] = Field(default_factory=list)
    hidden_roots: list[str] = Field(default_factory=list)
    cycle_interval_minutes: int = Field(default=30, ge=1, le=1440)
    interactive_approval_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    budgets: Budgets = Field(default_factory=Budgets)
    email: EmailConfig = Field(default_factory=EmailConfig)
    tasks: list[TaskConfig] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def _model_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("tasks")
    @classmethod
    def _unique_task_names(cls, tasks: list[TaskConfig]) -> list[TaskConfig]:
        names = [t.name for t in tasks]
        if len(names) != len(set(names)):
            raise ValueError("task names must be unique")
        return tasks


def config_path(profile: Path) -> Path:
    return profile / CONFIG_FILENAME


def load_config(profile: Path) -> AppConfig:
    path = config_path(profile)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return AppConfig(**raw)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc


def openrouter_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "OPENROUTER_API_KEY is not set. Set it as a Windows user environment "
            "variable and restart the terminal."
        )
    return key


def smtp_settings() -> dict[str, Any] | None:
    host = os.environ.get("CLAWBACK_SMTP_HOST", "").strip()
    if not host:
        return None
    try:
        port = int(os.environ.get("CLAWBACK_SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return {
        "host": host,
        "port": port,
        "user": os.environ.get("CLAWBACK_SMTP_USER", "").strip(),
        "password": os.environ.get("CLAWBACK_SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("CLAWBACK_SMTP_FROM", "").strip(),
        "security": os.environ.get("CLAWBACK_SMTP_SECURITY", "starttls").strip().lower(),
    }


def imap_settings() -> dict[str, Any] | None:
    host = os.environ.get("CLAWBACK_IMAP_HOST", "").strip()
    if not host:
        return None
    try:
        port = int(os.environ.get("CLAWBACK_IMAP_PORT", "993"))
    except ValueError:
        port = 993
    return {
        "host": host,
        "port": port,
        "user": os.environ.get("CLAWBACK_IMAP_USER", "").strip(),
        "password": os.environ.get("CLAWBACK_IMAP_PASSWORD", ""),
        "security": os.environ.get("CLAWBACK_IMAP_SECURITY", "ssl").strip().lower(),
    }
