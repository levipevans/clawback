"""Small, atomic JSON state store.

State that changes at runtime (task toggles, last-run timestamps, roots added
through the CLI) lives here rather than being rewritten into config.toml.
That keeps the declarative config reviewable and avoids clobbering comments.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._read()
        self._mtime: int | None = self._stat_mtime()

    def _stat_mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        """Pick up changes made by the CLI while the app is running."""
        mtime = self._stat_mtime()
        if mtime is not None and mtime != self._mtime:
            self._data = self._read()
            self._mtime = mtime

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self._stat_mtime()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._maybe_reload()
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._maybe_reload()
            self._data[key] = value
            self._write()

    def task_enabled(self, name: str, default: bool) -> bool:
        with self._lock:
            self._maybe_reload()
            overrides = self._data.get("task_enabled", {})
            if isinstance(overrides, dict) and name in overrides:
                return bool(overrides[name])
            return default

    def set_task_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self._maybe_reload()
            overrides = dict(self._data.get("task_enabled", {}))
            overrides[name] = bool(enabled)
            self._data["task_enabled"] = overrides
            self._write()

    def last_run(self, name: str) -> float:
        with self._lock:
            self._maybe_reload()
            runs = self._data.get("last_run", {})
            try:
                return float(runs.get(name, 0.0))
            except (TypeError, ValueError):
                return 0.0

    def set_last_run(self, name: str, timestamp: float) -> None:
        with self._lock:
            self._maybe_reload()
            runs = dict(self._data.get("last_run", {}))
            runs[name] = float(timestamp)
            self._data["last_run"] = runs
            self._write()

    def extra_roots(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            value = self._data.get("extra_read_roots", [])
            return [str(v) for v in value] if isinstance(value, list) else []

    def add_extra_root(self, path: str) -> None:
        with self._lock:
            self._maybe_reload()
            roots = self.extra_roots()
            if path not in roots:
                roots.append(path)
                self._data["extra_read_roots"] = roots
                self._write()

    def remove_extra_root(self, path: str) -> bool:
        with self._lock:
            self._maybe_reload()
            roots = self.extra_roots()
            if path not in roots:
                return False
            roots.remove(path)
            self._data["extra_read_roots"] = roots
            self._write()
            return True

    def extra_write_roots(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            value = self._data.get("extra_write_roots", [])
            return [str(v) for v in value] if isinstance(value, list) else []

    def add_extra_write_root(self, path: str) -> None:
        with self._lock:
            self._maybe_reload()
            roots = self.extra_write_roots()
            if path not in roots:
                roots.append(path)
                self._data["extra_write_roots"] = roots
                self._write()

    def remove_extra_write_root(self, path: str) -> bool:
        with self._lock:
            self._maybe_reload()
            roots = self.extra_write_roots()
            if path not in roots:
                return False
            roots.remove(path)
            self._data["extra_write_roots"] = roots
            self._write()
            return True

    def hidden_roots(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            value = self._data.get("hidden_roots", [])
            return [str(v) for v in value] if isinstance(value, list) else []

    def add_hidden_root(self, path: str) -> None:
        with self._lock:
            self._maybe_reload()
            roots = self.hidden_roots()
            if path not in roots:
                roots.append(path)
                self._data["hidden_roots"] = roots
                self._write()

    def remove_hidden_root(self, path: str) -> bool:
        with self._lock:
            self._maybe_reload()
            roots = self.hidden_roots()
            if path not in roots:
                return False
            roots.remove(path)
            self._data["hidden_roots"] = roots
            self._write()
            return True

    def email_seen(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            value = self._data.get("email_seen", [])
            return [str(v) for v in value] if isinstance(value, list) else []

    def is_email_seen(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self.email_seen()

    def add_email_seen(self, message_id: str, cap: int = 500) -> None:
        if not message_id:
            return
        with self._lock:
            self._maybe_reload()
            seen = self.email_seen()
            if message_id in seen:
                return
            seen.append(message_id)
            self._data["email_seen"] = seen[-cap:]
            self._write()

    def wipe(self) -> None:
        with self._lock:
            self._data = {}
            self._mtime = None
            if self.path.exists():
                self.path.unlink()
