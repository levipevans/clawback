"""Session and long-term memory.

* Conversation keeps a bounded chat history for the current terminal session
  so follow-ups ("what about the second file?") have context.
* MemoryStore owns `memory.md` in the profile. It is appended after each
  interaction and injected into the system prompt as untrusted data.

The model never gets a tool that writes memory.md. The host owns the file, so
a prompt-injected file cannot silently rewrite the assistant's memory.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MEMORY_FILENAME = "memory.md"

HEADER = """# Clawback memory

This file is maintained locally by Clawback. It is injected into the model's
context as untrusted data. Delete it to forget everything, or edit it by hand.

"""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass
class Conversation:
    """Bounded in-process history for the current terminal session."""

    max_messages: int = 40
    max_chars: int = 60_000
    _messages: list[dict] = field(default_factory=list)

    def messages(self) -> list[dict]:
        return [dict(message) for message in self._messages]

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": str(text)})
        self._trim()

    def add_assistant(self, text: str) -> None:
        self._messages.append({"role": "assistant", "content": str(text)})
        self._trim()

    def reset(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)

    def _size(self) -> int:
        return sum(len(str(m.get("content") or "")) for m in self._messages)

    def _over_limit(self) -> bool:
        return len(self._messages) > self.max_messages or self._size() > self.max_chars

    def _trim(self) -> None:
        while self._over_limit() and len(self._messages) >= 2:
            del self._messages[:2]
        while self._over_limit() and self._messages:
            self._messages.pop(0)


class MemoryStore:
    def __init__(self, path: Path, *, max_chars: int = 64_000, context_chars: int = 6_000):
        self.path = path
        self.max_chars = max_chars
        self.context_chars = context_chars
        self._lock = threading.RLock()

    def read(self) -> str:
        with self._lock:
            try:
                return self.path.read_text(encoding="utf-8")
            except OSError:
                return ""

    def context(self, max_chars: int | None = None) -> str:
        text = self.read()
        if not text:
            return ""
        limit = max(1, max_chars or self.context_chars)
        if len(text) > limit:
            return "...\n" + text[-limit:]
        return text

    def append(self, entry: str) -> None:
        with self._lock:
            text = self.read()
            if not text.startswith("# Clawback memory"):
                text = HEADER + text
            text = text.rstrip() + "\n\n" + entry.strip() + "\n"
            self._write(self._trim(text))

    def append_interaction(
        self,
        user: str,
        assistant: str,
        tools: list[str] | None = None,
        *,
        mode: str = "interactive",
    ) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        tools_text = ", ".join(tools) if tools else "none"
        entry = (
            f"## {stamp} ({mode})\n"
            f"- **You:** {_clip(user, 300)}\n"
            f"- **Clawback:** {_clip(assistant, 600)}\n"
            f"- **Tools:** {tools_text}"
        )
        self.append(entry)

    def append_note(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append(f"## {stamp} (note)\n- **You:** {_clip(text, 2000)}")

    def append_assistant(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append(f"## {stamp} (assistant)\n- **Clawback:** {_clip(text, 4000)}")

    def clear(self) -> None:
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                pass

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _trim(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        marker = "\n## "
        trimmed_line = "_(older memory trimmed)_"
        if marker not in text:
            return text[-self.max_chars :]
        header, *sections = text.split(marker)
        # Keep exactly one trim marker; do not let it accumulate across writes.
        header = header.replace(trimmed_line, "").rstrip()
        kept: list[str] = []
        total = len(header) + len(trimmed_line) + 8
        for section in reversed(sections):
            piece = marker + section
            if total + len(piece) > self.max_chars:
                break
            kept.append(piece)
            total += len(piece)
        kept.reverse()
        return header + "\n\n" + trimmed_line + "\n" + "".join(kept)

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
