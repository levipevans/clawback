"""Terminal interface. This is the primary UI; the tray is a status surface."""

from __future__ import annotations

import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .approvals import ApprovalBroker, ApprovalRequest
from .controller import Status
from .paths import is_sensitive_name, normalize
from .runtime import BusyError, Runtime


def _ts(value: float | None) -> str:
    if not value:
        return "never"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


class ConsoleUI:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.console = Console()
        self._active_broker: ApprovalBroker | None = None
        self._quit = False

    # -- entrypoints ------------------------------------------------------
    def banner(self) -> None:
        rt = self.runtime
        sandbox = "ready" if rt.sandbox_ok else "UNAVAILABLE (tools disabled)"
        body = (
            f"[bold]Clawback {__version__}[/bold]  -  local assistant\n\n"
            f"profile    {rt.profile}\n"
            f"workspace  {rt.policy.sandbox}\n"
            f"model      {rt.config.model}\n"
            f"sandbox    {sandbox}\n\n"
            "[dim]Type a request, or /help for commands.[/dim]"
        )
        self.console.print(Panel(body, border_style="cyan"))

    def run(self) -> int:
        self.banner()
        while not self._quit:
            try:
                line = input("clawback> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                self._command(line)
            else:
                self._chat(line)
        return 0

    def run_once(self, prompt: str) -> int:
        self._chat(prompt)
        return 0

    # -- chat -------------------------------------------------------------
    def _chat(self, prompt: str) -> None:
        if self.runtime.agent is None:
            self.console.print("[red]The model is not connected. Set OPENROUTER_API_KEY.[/red]")
            return
        broker = ApprovalBroker(
            interactive=True,
            timeout_seconds=self.runtime.config.interactive_approval_timeout_seconds,
            prompt=self._prompt_approval,
        )
        self._active_broker = broker
        self.console.print("[dim]thinking...[/dim]")
        try:
            result = self.runtime.run_interactive(prompt, broker=broker, on_event=self._event)
        except BusyError:
            self.console.print("[yellow]A scheduled cycle is running. Use /stop or try again.[/yellow]")
            return
        except Exception as exc:
            self.console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
            return
        finally:
            self._active_broker = None
        self.console.print()
        self.console.print(result.text)
        self.console.print(
            f"[dim]{result.stopped_reason} - {result.tool_calls} tool calls - "
            f"{result.rounds} rounds - {result.duration:.1f}s[/dim]"
        )

    def _event(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/dim]")

    def _prompt_approval(self, request: ApprovalRequest) -> None:
        self.console.print(
            Panel(
                request.preview,
                title=f"Confirm: {request.tool}  (id {request.id})",
                border_style="yellow",
            )
        )
        try:
            answer = input("Approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        broker = self._active_broker
        if broker is None:
            return
        if answer in ("y", "yes"):
            broker.approve(request.id, request.digest)
        else:
            broker.deny(request.id)

    # -- commands ---------------------------------------------------------
    def _command(self, line: str) -> None:
        body = line[1:].strip()
        cmd, _, rest = body.partition(" ")
        cmd = cmd.lower()
        rest = rest.strip().strip('"')

        handlers = {
            "help": self._cmd_help,
            "status": self._cmd_status,
            "tasks": self._cmd_tasks,
            "enable": self._cmd_enable,
            "disable": self._cmd_disable,
            "roots": self._cmd_roots,
            "add-root": self._cmd_add_root,
            "add-write-root": self._cmd_add_write_root,
            "remove-root": self._cmd_remove_root,
            "hide-root": self._cmd_hide_root,
            "unhide-root": self._cmd_unhide_root,
            "log": self._cmd_log,
            "memory": self._cmd_memory,
            "remember": self._cmd_remember,
            "email": self._cmd_email,
            "new": self._cmd_new,
            "wipe": self._cmd_wipe,
            "sandbox": self._cmd_sandbox,
            "path": self._cmd_path,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "stop": self._cmd_stop,
            "quit": self._cmd_quit,
            "exit": self._cmd_quit,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self.console.print(f"[red]unknown command /{cmd}[/red]")
            self._cmd_help()
            return
        handler(rest)

    def _cmd_help(self, _rest: str = "") -> None:
        table = Table(title="commands", show_header=True, header_style="bold")
        table.add_column("command")
        table.add_column("what it does")
        rows = [
            ("<text>", "talk to the assistant (tools are approval-gated)"),
            ("/status", "status, sandbox, model, locations"),
            ("/pause  /resume", "pause or resume scheduled wake cycles"),
            ("/stop", "cancel the current cycle or run"),
            ("/tasks", "list scheduled tasks"),
            ("/enable NAME  /disable NAME", "toggle a task"),
            ("/roots", "show mounted folders and their modes"),
            ("/add-root PATH", "grant read-only access to a folder"),
            ("/add-write-root PATH", "grant read-write access to a folder"),
            ("/remove-root PATH", "remove a read or write grant"),
            ("/hide-root PATH", "hide a folder (empty mount)"),
            ("/unhide-root PATH", "undo a hide"),
            ("/memory [show|clear|reset|path]", "long-term memory file"),
            ("/remember TEXT", "add a note to memory"),
            ("/email [status|check|poll]", "inbound email status, connectivity, one poll"),
            ("/new", "clear this session's conversation context"),
            ("/log [n]", "recent activity"),
            ("/wipe", "delete audit log, memory, and runtime state"),
            ("/sandbox", "Docker sandbox health"),
            ("/path", "print data locations"),
            ("/quit", "stop and exit"),
        ]
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def _cmd_status(self, _rest: str = "") -> None:
        snap = self.runtime.controller.snapshot()
        table = Table(show_header=False, box=None)
        table.add_column("k", style="bold")
        table.add_column("v", overflow="fold")
        table.add_row("status", snap.status.value + (f" - {snap.detail}" if snap.detail else ""))
        table.add_row("changed", _ts(snap.changed_at))
        table.add_row("model", self.runtime.config.model)
        table.add_row("sandbox", "ready" if self.runtime.sandbox_ok else "unavailable")
        table.add_row("workspace", str(self.runtime.policy.sandbox))
        table.add_row("roots", f"{len(self.runtime.policy.roots)} mounted")
        table.add_row("writable grants", f"{len(self.runtime.policy.writable_roots())} (outside /work)")
        table.add_row("memory", f"{self.runtime.memory.size()} bytes")
        enabled = sum(
            1
            for t in self.runtime.config.tasks
            if self.runtime.state.task_enabled(t.name, t.enabled)
        )
        table.add_row("tasks enabled", f"{enabled}/{len(self.runtime.config.tasks)}")
        table.add_row("email", self.runtime.mailer.describe())
        table.add_row("profile", str(self.runtime.profile))
        self.console.print(table)

    def _cmd_tasks(self, _rest: str = "") -> None:
        rows = self.runtime.scheduler.status_rows()
        if not rows:
            self.console.print("[dim]no tasks configured[/dim]")
            return
        table = Table(title="scheduled tasks")
        table.add_column("name")
        table.add_column("enabled")
        table.add_column("every")
        table.add_column("last run")
        table.add_column("next")
        for row in rows:
            next_due = "due" if row["next_due_in"] <= 1 else f"in {int(row['next_due_in'] // 60)}m"
            table.add_row(
                row["name"],
                "yes" if row["enabled"] else "no",
                f"{row['every_minutes']}m",
                _ts(row["last_run"]),
                next_due,
            )
        self.console.print(table)

    def _task_exists(self, name: str) -> bool:
        return any(t.name == name for t in self.runtime.config.tasks)

    def _cmd_enable(self, name: str) -> None:
        if not name or not self._task_exists(name):
            self.console.print("[red]usage: /enable <task-name>[/red]")
            return
        self.runtime.state.set_task_enabled(name, True)
        self.runtime.audit.record("task.toggle", task=name, enabled=True)
        self.console.print(f"enabled {name}")

    def _cmd_disable(self, name: str) -> None:
        if not name or not self._task_exists(name):
            self.console.print("[red]usage: /disable <task-name>[/red]")
            return
        self.runtime.state.set_task_enabled(name, False)
        self.runtime.audit.record("task.toggle", task=name, enabled=False)
        self.console.print(f"disabled {name}")

    def _cmd_roots(self, _rest: str = "") -> None:
        table = Table(title="visible roots")
        table.add_column("container")
        table.add_column("host", overflow="fold")
        table.add_column("mode")
        for container, host, mode in self.runtime.policy.describe():
            table.add_row(container, host, mode)
        self.console.print(table)

    def _reload(self, what: str) -> None:
        try:
            ok = self.runtime.reload_environment()
        except BusyError:
            self.console.print("[yellow]a run is in progress; try again in a moment[/yellow]")
            return
        self.console.print(f"{what}. sandbox: {'ready' if ok else 'unavailable'}")

    def _resolve_dir(self, path: str, command: str):
        if not path:
            self.console.print(f"[red]usage: {command} <folder>[/red]")
            return None
        try:
            resolved = normalize(path)
        except Exception as exc:
            self.console.print(f"[red]{exc}[/red]")
            return None
        if not resolved.is_dir():
            self.console.print("[red]not a directory[/red]")
            return None
        if is_sensitive_name(resolved):
            self.console.print(
                "[red]refused: that folder is in a sensitive location "
                "(AppData or a dotfile directory)[/red]"
            )
            return None
        return resolved

    def _cmd_add_root(self, path: str) -> None:
        resolved = self._resolve_dir(path, "/add-root")
        if resolved is None:
            return
        self.runtime.state.add_extra_root(str(resolved))
        self.runtime.audit.record("roots.add", path=str(resolved), access="ro")
        self._reload(f"added read-only root {resolved}")

    def _cmd_add_write_root(self, path: str) -> None:
        resolved = self._resolve_dir(path, "/add-write-root")
        if resolved is None:
            return
        self.runtime.state.add_extra_write_root(str(resolved))
        self.runtime.audit.record("roots.add", path=str(resolved), access="rw")
        self._reload(f"added READ-WRITE root {resolved} (writes still need confirmation)")

    def _cmd_remove_root(self, path: str) -> None:
        if not path:
            self.console.print("[red]usage: /remove-root <folder>[/red]")
            return
        try:
            resolved = normalize(path)
        except Exception as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        read_removed = self.runtime.state.remove_extra_root(str(resolved))
        write_removed = self.runtime.state.remove_extra_write_root(str(resolved))
        self.runtime.audit.record(
            "roots.remove", path=str(resolved), read=read_removed, write=write_removed
        )
        if not read_removed and not write_removed:
            self.console.print("[dim]that path was not a runtime grant[/dim]")
            return
        self._reload(f"removed grant {resolved}")

    def _cmd_memory(self, rest: str = "") -> None:
        action = (rest or "show").strip().lower()
        if action == "path":
            self.console.print(str(self.runtime.memory_path))
            return
        if action == "reset":
            self.runtime.memory.reset()
            self.runtime.audit.record("memory.reset")
            self.console.print("memory reset to empty (header kept)")
            return
        if action in ("clear", "wipe", "delete"):
            self.runtime.memory.clear()
            self.runtime.audit.record("memory.clear")
            self.console.print("memory file deleted")
            return
        text = self.runtime.memory.read()
        if not text:
            self.console.print("[dim]memory is empty[/dim]")
            return
        self.console.print(
            Panel(
                text[-4000:],
                title=f"memory ({len(text)} chars) - {self.runtime.memory_path}",
                border_style="dim",
            )
        )

    def _cmd_remember(self, rest: str = "") -> None:
        if not rest:
            self.console.print("[red]usage: /remember <text>[/red]")
            return
        self.runtime.memory.append_note(rest)
        self.runtime.audit.record("memory.note")
        self.console.print("noted in memory")

    def _cmd_email(self, rest: str = "") -> None:
        action = (rest or "status").strip().lower()
        if self.runtime.inbox is None:
            self.console.print("[dim]inbound email is not available[/dim]")
            return
        if action == "status":
            self.console.print(self.runtime.inbox.status())
            return
        if action == "check":
            self.console.print(self.runtime.inbox.check())
            return
        if action == "poll":
            self.console.print(self.runtime.inbox.poll_once())
            return
        self.console.print("[red]usage: /email [status|check|poll][/red]")

    def _cmd_new(self, _rest: str = "") -> None:
        self.runtime.reset_conversation()
        self.console.print("session context cleared (memory.md is kept)")

    def _cmd_hide_root(self, path: str) -> None:
        if not path:
            self.console.print("[red]usage: /hide-root <folder>[/red]")
            return
        try:
            resolved = normalize(path)
        except Exception as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        self.runtime.state.add_hidden_root(str(resolved))
        self.runtime.audit.record("roots.hide", path=str(resolved))
        self._reload(f"hid {resolved}")

    def _cmd_unhide_root(self, path: str) -> None:
        if not path:
            self.console.print("[red]usage: /unhide-root <folder>[/red]")
            return
        try:
            resolved = normalize(path)
        except Exception as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        removed = self.runtime.state.remove_hidden_root(str(resolved))
        self.runtime.audit.record("roots.unhide", path=str(resolved), removed=removed)
        in_config = str(resolved) in {
            str(normalize(p)) for p in self.runtime.config.hidden_roots
        }
        if in_config:
            self.console.print(
                "[yellow]note: it is also listed in config.toml hidden_roots; "
                "remove it there too.[/yellow]"
            )
        elif not removed:
            self.console.print("[dim]that path was not hidden[/dim]")
        self._reload(f"unhid {resolved}")

    def _cmd_log(self, rest: str) -> None:
        try:
            limit = int(rest) if rest else 20
        except ValueError:
            limit = 20
        entries = self.runtime.audit.tail(limit)
        if not entries:
            self.console.print("[dim]no activity yet[/dim]")
            return
        table = Table(title=f"last {len(entries)} events")
        table.add_column("time")
        table.add_column("event")
        table.add_column("detail")
        for entry in entries:
            detail = (
                entry.get("tool")
                or entry.get("task")
                or entry.get("reason")
                or entry.get("stopped")
                or entry.get("error")
                or ""
            )
            table.add_row(str(entry.get("ts", ""))[11:19], str(entry.get("event", "")), str(detail)[:80])
        self.console.print(table)

    def _cmd_wipe(self, _rest: str = "") -> None:
        try:
            answer = input("Delete the audit log, memory, and runtime state? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            return
        self.runtime.audit.wipe()
        self.runtime.memory.clear()
        self.runtime.state.wipe()
        self.console.print("deleted audit log, memory, and runtime state")

    def _cmd_sandbox(self, _rest: str = "") -> None:
        status = self.runtime.sandbox.status()
        self.console.print(status)

    def _cmd_path(self, _rest: str = "") -> None:
        self.console.print(
            f"profile:   {self.runtime.profile}\n"
            f"config:    {self.runtime.config_path}\n"
            f"audit:     {self.runtime.audit_path}\n"
            f"memory:    {self.runtime.memory_path}\n"
            f"workspace: {self.runtime.workspace_path}"
        )

    def _cmd_pause(self, _rest: str = "") -> None:
        self.runtime.scheduler.pause()
        self.console.print("paused scheduled cycles")

    def _cmd_resume(self, _rest: str = "") -> None:
        self.runtime.scheduler.resume()
        self.console.print("resumed scheduled cycles")

    def _cmd_stop(self, _rest: str = "") -> None:
        self.runtime.scheduler.stop_current()
        self.console.print("cancel requested")

    def _cmd_quit(self, _rest: str = "") -> None:
        self.console.print("shutting down...")
        self._quit = True
