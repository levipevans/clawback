"""Command line entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__, paths
from .audit import AuditLog
from .config import ConfigError, imap_settings, load_config
from .console import ConsoleUI
from .memory import MemoryStore
from .paths import is_sensitive_name, normalize
from .runtime import Runtime
from .sandbox.docker import SandboxUnavailable
from .state import StateStore
from .tray import Tray


def _table(rows: list[tuple[str, str]], title: str) -> None:
    table = Table(title=title, show_header=False, box=None)
    table.add_column("k", style="bold")
    table.add_column("v", overflow="fold")
    for key, value in rows:
        table.add_row(key, value)
    Console().print(table)


def cmd_run(args: argparse.Namespace) -> int:
    runtime = Runtime()
    runtime.connect()
    runtime.start_sandbox()
    if not runtime.sandbox_ok:
        Console().print(
            "[yellow]Sandbox unavailable: tools are disabled until Docker Desktop is running.[/yellow]"
        )
    ui = ConsoleUI(runtime)
    runtime.set_output(ui._event)
    if not getattr(args, "no_scheduler", False):
        runtime.scheduler.start()
    if runtime.config.email.inbound:
        if runtime.start_inbox():
            Console().print("[dim]inbound email polling started[/dim]")
        else:
            Console().print(
                "[yellow]inbound email enabled but not ready; run `clawback email status`.[/yellow]"
            )
    tray = None
    if not getattr(args, "no_tray", False):
        tray = Tray(runtime, on_quit=runtime.shutdown)
        tray.start()
    try:
        return ui.run()
    finally:
        runtime.shutdown()


def cmd_chat(args: argparse.Namespace) -> int:
    runtime = Runtime()
    runtime.connect()
    runtime.start_sandbox()
    ui = ConsoleUI(runtime)
    runtime.set_output(ui._event)
    try:
        if args.prompt:
            return ui.run_once(" ".join(args.prompt))
        return ui.run()
    finally:
        runtime.shutdown()


def cmd_status(_args: argparse.Namespace) -> int:
    runtime = Runtime()
    status = runtime.sandbox.status()
    rows = [
        ("profile", str(runtime.profile)),
        ("config", str(runtime.config_path)),
        ("workspace", str(runtime.policy.sandbox)),
        ("model", runtime.config.model),
        ("visible roots", "\n".join(c for c, _h, _m in runtime.policy.describe())),
        (
            "grants",
            f"{len(runtime.policy.roots)} mounted, "
            f"{len(runtime.policy.writable_roots())} writable outside /work",
        ),
        ("memory", f"{runtime.memory.size()} bytes"),
        ("tasks", ", ".join(t.name for t in runtime.config.tasks) or "(none)"),
        ("email", runtime.mailer.describe()),
        ("docker", "available" if status.get("available") else "unavailable"),
        ("sandbox", "running" if status.get("running") else "not running"),
    ]
    _table(rows, f"Clawback {__version__}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    audit = AuditLog(profile / "audit.jsonl")
    entries = audit.tail(args.lines)
    if not entries:
        print("no activity yet")
        return 0
    for entry in entries:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    if not args.yes:
        answer = input("Delete the audit log and runtime state? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return 1
    AuditLog(profile / "audit.jsonl").wipe()
    MemoryStore(profile / "memory.md").clear()
    StateStore(profile / "state.json").wipe()
    print("deleted audit log, memory, and runtime state")
    if args.all:
        config = load_config(profile)
        workspace = paths.workspace_dir(profile, config.sandbox_dir)
        if not args.yes:
            answer = input(f"Also delete the workspace {workspace}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("kept workspace")
                return 0
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        print(f"deleted workspace {workspace}")
    return 0


def cmd_roots(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    state = StateStore(profile / "state.json")
    if args.action == "list":
        runtime = Runtime(profile)
        rows = [(c, f"{h} ({m})") for c, h, m in runtime.policy.describe()]
        _table(rows, "visible roots")
        return 0
    if not args.path:
        print(
            "usage: clawback roots add|remove|hide|unhide <folder> [--write]",
            file=sys.stderr,
        )
        return 2
    resolved = normalize(args.path)
    if not resolved.exists():
        print(f"not found: {resolved}", file=sys.stderr)
        return 2
    if is_sensitive_name(resolved):
        print(
            "refused: that folder is in a sensitive location (AppData or a dotfile directory)",
            file=sys.stderr,
        )
        return 2
    if args.action == "add":
        if args.write:
            state.add_extra_write_root(str(resolved))
            print(f"added read-write root: {resolved} (writes still need confirmation)")
        else:
            state.add_extra_root(str(resolved))
            print(f"added read-only root: {resolved}")
    elif args.action == "remove":
        read_removed = state.remove_extra_root(str(resolved))
        write_removed = state.remove_extra_write_root(str(resolved))
        if not read_removed and not write_removed:
            print(f"not a runtime grant: {resolved}")
            return 1
        print(f"removed grant: {resolved}")
    elif args.action == "hide":
        state.add_hidden_root(str(resolved))
        print(f"hid: {resolved}")
    else:
        removed = state.remove_hidden_root(str(resolved))
        print(f"{'unhid' if removed else 'was not hidden'}: {resolved}")
    print("restart Clawback (or use the /roots commands in the running app) to apply.")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    memory = MemoryStore(profile / "memory.md")
    if args.action == "path":
        print(memory.path)
        return 0
    if args.action == "clear":
        memory.clear()
        print("memory cleared")
        return 0
    text = memory.read()
    print(text if text else "(memory is empty)")
    return 0


def cmd_email(args: argparse.Namespace) -> int:
    runtime = Runtime()
    email = runtime.config.email
    if args.action == "status":
        imap = imap_settings() or {}
        rows = [
            ("enabled", str(email.enabled)),
            ("inbound", str(email.inbound)),
            ("address", email.address or "(unset)"),
            ("owner", email.recipient or "(unset)"),
            ("trusted", ", ".join(sorted(email.allowed_recipients())) or "(none)"),
            ("smtp", "configured" if runtime.mailer.available() else "missing"),
            ("imap", imap.get("host", "missing")),
            ("poll", f"{email.poll_minutes}m"),
            ("auto_reply", str(email.auto_reply)),
            ("delete_malicious", str(email.delete_malicious)),
        ]
        _table(rows, "email")
        return 0

    runtime.connect()
    runtime.start_sandbox()
    try:
        if runtime.inbox is None:
            print("inbound email is not available", file=sys.stderr)
            return 1
        if args.action == "check":
            print(json.dumps(runtime.inbox.check(), indent=2))
        else:
            print(json.dumps(runtime.inbox.poll_once(), indent=2))
        return 0
    finally:
        runtime.shutdown()


def cmd_tasks(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    config = load_config(profile)
    state = StateStore(profile / "state.json")
    if args.action == "list":
        rows = []
        for task in config.tasks:
            enabled = state.task_enabled(task.name, task.enabled)
            rows.append((task.name, f"{'enabled' if enabled else 'disabled'}, every {task.every_minutes}m"))
        if not rows:
            rows = [("(none)", "no tasks configured")]
        _table(rows, "scheduled tasks")
        return 0
    if not args.name:
        print("usage: clawback tasks enable|disable <name>", file=sys.stderr)
        return 2
    if not any(t.name == args.name for t in config.tasks):
        print(f"unknown task: {args.name}", file=sys.stderr)
        return 2
    state.set_task_enabled(args.name, args.action == "enable")
    print(f"{args.action}d {args.name}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    checks: list[tuple[str, bool, str]] = []
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    checks.append(("OPENROUTER_API_KEY", bool(key), "set" if key else "missing"))
    try:
        config = load_config(profile)
        checks.append(("config", True, str(profile / "config.toml")))
    except ConfigError as exc:
        checks.append(("config", False, str(exc)))
        config = None
    docker = shutil.which("docker")
    checks.append(("docker CLI", bool(docker), docker or "not on PATH"))
    if config is not None:
        try:
            runtime = Runtime(profile)
            status = runtime.sandbox.status()
            checks.append(("docker daemon", bool(status.get("available")), str(status.get("error", ""))))
            checks.append(("sandbox container", bool(status.get("running")), status.get("container", "")))
            if not status.get("running"):
                checks[-1] = (
                    "sandbox container",
                    False,
                    "not running (starts on `run` or `sandbox start`)",
                )
            checks.append(
                (
                    "visible roots",
                    True,
                    f"{len(runtime.policy.roots)} mounted, "
                    f"{len(runtime.policy.writable_roots())} writable, "
                    f"{len(runtime.policy.hidden)} hidden",
                )
            )
            checks.append(("memory", True, f"{runtime.memory.size()} bytes at {runtime.memory_path}"))
            checks.append(("email", True, runtime.mailer.describe()))
            imap = imap_settings() or {}
            checks.append(("imap", bool(imap.get("host")), imap.get("host", "not configured")))
            checks.append(("inbound", True, "on" if runtime.config.email.inbound else "off"))
            checks.append(("audit", True, str(runtime.audit_path)))
        except Exception as exc:
            checks.append(("runtime", False, f"{type(exc).__name__}: {exc}"))
    table = Table(title="doctor")
    table.add_column("check")
    table.add_column("ok")
    table.add_column("detail", overflow="fold")
    for name, ok, detail in checks:
        table.add_row(name, "[green]yes[/green]" if ok else "[red]no[/red]", str(detail))
    Console().print(table)
    required = {"OPENROUTER_API_KEY", "config", "docker CLI", "docker daemon"}
    return 0 if all(ok for name, ok, _detail in checks if name in required) else 1


def cmd_config(args: argparse.Namespace) -> int:
    profile = paths.profile_dir()
    config = load_config(profile)
    if args.action == "show":
        print(config.model_dump_json(indent=2))
        return 0
    print(f"profile:   {profile}")
    print(f"config:    {profile / 'config.toml'}")
    print(f"audit:     {profile / 'audit.jsonl'}")
    print(f"memory:    {profile / 'memory.md'}")
    print(f"state:     {profile / 'state.json'}")
    print(f"workspace: {paths.workspace_dir(profile, config.sandbox_dir)}")
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    runtime = Runtime()
    if args.action == "start":
        ok = runtime.start_sandbox()
        print("sandbox started" if ok else "sandbox unavailable")
        return 0 if ok else 1
    if args.action == "stop":
        runtime.sandbox.stop()
        print("sandbox stopped")
        return 0
    print(json.dumps(runtime.sandbox.status(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clawback", description="A small, security-first local assistant.")
    parser.add_argument("--version", action="version", version=f"Clawback {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the assistant (terminal + tray)")
    run.add_argument("--no-tray", action="store_true")
    run.add_argument("--no-scheduler", action="store_true")

    chat = sub.add_parser("chat", help="interactive chat without scheduler or tray")
    chat.add_argument("prompt", nargs="*")

    sub.add_parser("status", help="show configuration and sandbox status")

    log = sub.add_parser("log", help="print recent activity")
    log.add_argument("-n", "--lines", type=int, default=30)

    wipe = sub.add_parser("wipe", help="delete audit log and runtime state")
    wipe.add_argument("--yes", action="store_true")
    wipe.add_argument("--all", action="store_true", help="also delete the workspace")

    roots = sub.add_parser("roots", help="list or change folder grants")
    roots.add_argument(
        "action", nargs="?", choices=["list", "add", "remove", "hide", "unhide"], default="list"
    )
    roots.add_argument("path", nargs="?")
    roots.add_argument(
        "--write", action="store_true", help="grant read-write access instead of read-only"
    )

    tasks = sub.add_parser("tasks", help="list or toggle scheduled tasks")
    tasks.add_argument("action", nargs="?", choices=["list", "enable", "disable"], default="list")
    tasks.add_argument("name", nargs="?")

    sub.add_parser("doctor", help="check the environment")

    memory = sub.add_parser("memory", help="show or clear long-term memory")
    memory.add_argument("action", nargs="?", choices=["show", "clear", "path"], default="show")

    email_parser = sub.add_parser("email", help="inbound email status, check, or one poll")
    email_parser.add_argument(
        "action", nargs="?", choices=["status", "check", "poll"], default="status"
    )

    config = sub.add_parser("config", help="show configuration")
    config.add_argument("action", nargs="?", choices=["show", "path"], default="show")

    sandbox = sub.add_parser("sandbox", help="sandbox lifecycle")
    sandbox.add_argument("action", nargs="?", choices=["check", "start", "stop"], default="check")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if args.command is None:
        args.no_tray = False
        args.no_scheduler = False
    try:
        if command == "run":
            return cmd_run(args)
        if command == "chat":
            return cmd_chat(args)
        if command == "status":
            return cmd_status(args)
        if command == "log":
            return cmd_log(args)
        if command == "wipe":
            return cmd_wipe(args)
        if command == "roots":
            return cmd_roots(args)
        if command == "tasks":
            return cmd_tasks(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "memory":
            return cmd_memory(args)
        if command == "email":
            return cmd_email(args)
        if command == "config":
            return cmd_config(args)
        if command == "sandbox":
            return cmd_sandbox(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except SandboxUnavailable as exc:
        print(f"sandbox error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
