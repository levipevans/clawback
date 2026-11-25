from __future__ import annotations

from clawback.state import StateStore


def test_write_roots_persist_and_cross_process_reload(tmp_path):
    path = tmp_path / "state.json"
    first = StateStore(path)
    first.add_extra_write_root("/x")

    second = StateStore(path)
    assert second.extra_write_roots() == ["/x"]

    second.add_extra_write_root("/y")
    assert first.extra_write_roots() == ["/x", "/y"]

    assert first.remove_extra_write_root("/x") is True
    assert first.extra_write_roots() == ["/y"]


def test_task_toggle_is_visible_to_a_running_instance(tmp_path):
    path = tmp_path / "state.json"
    running = StateStore(path)
    cli = StateStore(path)
    cli.set_task_enabled("digest", True)
    assert running.task_enabled("digest", False) is True


def test_email_seen_persists_and_reloads(tmp_path):
    path = tmp_path / "state.json"
    first = StateStore(path)
    first.add_email_seen("<m1@example.com>")
    second = StateStore(path)
    assert second.is_email_seen("<m1@example.com>") is True

    second.add_email_seen("<m2@example.com>")
    assert first.is_email_seen("<m2@example.com>") is True
    assert first.is_email_seen("<missing@example.com>") is False
