from __future__ import annotations

import textwrap

import pytest

from clawback.config import (
    AppConfig,
    ConfigError,
    EmailConfig,
    TaskConfig,
    load_config,
    openrouter_api_key,
    smtp_settings,
)


def test_default_config_is_created(tmp_path):
    config = load_config(tmp_path)
    assert (tmp_path / "config.toml").exists()
    assert config.model == "deepseek/deepseek-v4.1-flash-1017"
    assert config.email.enabled is False
    assert config.include_standard_read_roots is True
    assert config.extra_write_roots == []


def test_duplicate_task_names_are_rejected(tmp_path):
    (tmp_path / "config.toml").write_text(
        textwrap.dedent(
            """
            [[tasks]]
            name = "same"
            prompt = "a"

            [[tasks]]
            name = "same"
            prompt = "b"
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_invalid_task_name_is_rejected():
    with pytest.raises(Exception):
        TaskConfig(name="bad name!", prompt="x")


def test_invalid_email_recipient_is_rejected():
    with pytest.raises(Exception):
        EmailConfig(enabled=True, recipient="not-an-address")


def test_smtp_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CLAWBACK_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAWBACK_SMTP_PORT", "465")
    monkeypatch.setenv("CLAWBACK_SMTP_USER", "bot")
    monkeypatch.setenv("CLAWBACK_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("CLAWBACK_SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("CLAWBACK_SMTP_SECURITY", "ssl")
    settings = smtp_settings()
    assert settings == {
        "host": "smtp.example.com",
        "port": 465,
        "user": "bot",
        "password": "secret",
        "from_addr": "bot@example.com",
        "security": "ssl",
    }


def test_smtp_is_none_without_host(monkeypatch):
    monkeypatch.delenv("CLAWBACK_SMTP_HOST", raising=False)
    assert smtp_settings() is None


def test_missing_key_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        openrouter_api_key()
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_extra_config_keys_are_rejected(tmp_path):
    (tmp_path / "config.toml").write_text("surprise = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_defaults_merge_with_partial_toml(tmp_path):
    (tmp_path / "config.toml").write_text('model = "x/y"\n', encoding="utf-8")
    config = load_config(tmp_path)
    assert config.model == "x/y"
    assert config.budgets.max_seconds == 90
    assert isinstance(config, AppConfig)
