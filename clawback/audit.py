"""Append-only activity log.

Every wake cycle, task, tool call, permission decision, approval and outcome is
recorded. The file is JSONL so it is greppable and reviewable without the app.
Secrets are redacted on the way in; large payloads are truncated.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_FILENAME = "audit.jsonl"
_SENSITIVE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer|cookie)")
_MAX_STRING = 4000


def _clean(key: str, value: Any) -> Any:
    if _SENSITIVE.search(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(k): _clean(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(key, v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_STRING:
        return value[:_MAX_STRING] + f"...[truncated {len(value) - _MAX_STRING} chars]"
    return value


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = 8_000_000):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        entry.update({k: _clean(k, v) for k, v in fields.items()})
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError:
                # Logging must never take the assistant down.
                pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                backup = self.path.with_suffix(".jsonl.1")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
        except OSError:
            pass

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        with self._lock:
            if not self.path.exists():
                return []
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def wipe(self) -> None:
        with self._lock:
            for path in (self.path, self.path.with_suffix(".jsonl.1")):
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
