"""Tool dispatch: validate -> check policy -> confirm -> execute -> audit."""

from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import jsonschema

from ..approvals import ApprovalBroker
from ..audit import AuditLog
from ..config import AppConfig
from ..llm.openrouter import LLMError, OpenRouterClient
from ..memory import MemoryStore
from ..policy import Policy, PolicyError
from ..sandbox.docker import DockerSandbox
from ..state import StateStore
from ..web import WebError, fetch_url
from . import schemas
from .email import EmailError, Mailer

EventFn = Callable[[str], None]

# argument name -> whether the path must be writable
_PATH_ARGS: dict[str, dict[str, bool]] = {
    "list_dir": {"path": False},
    "read_file": {"path": False},
    "file_info": {"path": False},
    "search_files": {"path": False},
    "write_file": {"path": True},
    "append_file": {"path": True},
    "make_dir": {"path": True},
    "move_path": {"src": True, "dst": True},
    "copy_path": {"src": False, "dst": True},
    "delete_path": {"path": True},
}


@dataclass
class ToolOutcome:
    ok: bool
    tool: str
    decision: str
    content: str
    duration: float = 0.0
    scopes: list[str] = field(default_factory=list)


class ToolDispatcher:
    def __init__(
        self,
        *,
        policy: Policy,
        sandbox: DockerSandbox,
        audit: AuditLog,
        config: AppConfig,
        mailer: Mailer | None = None,
        memory: MemoryStore | None = None,
        state: StateStore | None = None,
        llm: OpenRouterClient | None = None,
        subagent_runner: Callable | None = None,
        depth: int = 0,
        on_event: EventFn | None = None,
    ):
        self.policy = policy
        self.sandbox = sandbox
        self.audit = audit
        self.config = config
        self.mailer = mailer
        self.memory = memory
        self.state = state
        self.llm = llm
        self.subagent_runner = subagent_runner
        self.depth = max(0, int(depth))
        self._subagent_count = 0
        self.on_event = on_event

    def begin_run(self) -> None:
        self._subagent_count = 0

    # -- model-facing specs ----------------------------------------------
    def _spec_map(self, mode: str) -> dict[str, dict]:
        include_email = self._email_enabled() and mode != "email"
        specs = schemas.by_name(include_email)
        if (
            self.subagent_runner is None
            or self.depth >= self.config.budgets.max_subagent_depth
        ):
            specs.pop("spawn_subagent", None)
        if self.state is None:
            specs.pop("schedule_wakeup", None)
        if self.llm is None:
            specs.pop("web_search", None)
        return specs

    def tool_specs(self, mode: str = "interactive") -> list[dict]:
        return list(self._spec_map(mode).values())

    def _email_enabled(self) -> bool:
        return bool(self.mailer and self.mailer.available())

    # -- execution --------------------------------------------------------
    def execute(
        self,
        name: str,
        args: dict | None,
        *,
        broker: ApprovalBroker,
        cycle_id: str,
        mode: str,
        deadline: float | None = None,
    ) -> ToolOutcome:
        started = time.time()
        specs = self._spec_map(mode)
        spec = specs.get(name)
        if spec is None:
            return self._finish(
                name, args or {}, "error", {"ok": False, "error": f"unknown tool {name!r}"},
                started, cycle_id, mode, [],
            )
        if not isinstance(args, dict):
            return self._finish(
                name, {}, "error", {"ok": False, "error": "arguments must be a JSON object"},
                started, cycle_id, mode, [],
            )

        try:
            jsonschema.validate(instance=args, schema=spec["function"]["parameters"])
        except jsonschema.ValidationError as exc:
            return self._finish(
                name,
                args,
                "error",
                {"ok": False, "error": "invalid_arguments", "detail": exc.message},
                started,
                cycle_id,
                mode,
                [],
            )

        if name in ("read_memory", "append_memory"):
            return self._memory_tool(name, args, cycle_id, mode, started)
        if name == "web_search":
            return self._web_search(args, cycle_id, mode, started)
        if name == "web_fetch":
            return self._web_fetch(args, cycle_id, mode, started)
        if name == "schedule_wakeup":
            return self._schedule_wakeup(args, cycle_id, mode, started)
        if name == "spawn_subagent":
            return self._spawn_subagent(args, broker, deadline, cycle_id, mode, started)

        scopes: list[str] = []
        try:
            scopes = self._check_paths(name, args)
        except PolicyError as exc:
            return self._finish(
                name, args, "denied", {"ok": False, "error": "policy_denied", "detail": str(exc)},
                started, cycle_id, mode, [],
            )

        if name in schemas.CONSEQUENTIAL:
            preview = self._preview(name, args)
            if self.on_event:
                self.on_event(f"confirmation needed: {name}")
            result = broker.request(name, args, preview)
            self.audit.record(
                "approval.decision",
                cycle_id=cycle_id,
                mode=mode,
                tool=name,
                request_id=result.request_id,
                digest=result.digest,
                approved=result.approved,
                reason=result.reason,
            )
            if not result.approved:
                needs_owner = result.reason == "needs_approval" or mode == "scheduled"
                message = (
                    "The owner did not approve this action. Do not retry it; "
                    "report what you were trying to do and why."
                )
                if needs_owner:
                    if mode.startswith("email"):
                        message = (
                            "This action requires terminal confirmation, and email "
                            "sessions cannot ask for it. It was not executed. Report it "
                            "as pending approval."
                        )
                    else:
                        message = (
                            "This action needs the owner's approval, and scheduled cycles "
                            "cannot ask for it. It was not executed. Report it as pending approval."
                        )
                return self._finish(
                    name,
                    args,
                    "needs_approval" if needs_owner else "denied",
                    {"ok": False, "error": "needs_approval" if needs_owner else result.reason, "message": message},
                    started,
                    cycle_id,
                    mode,
                    scopes,
                )

        payload = self._dispatch(name, args, deadline=deadline)
        decision = "approved" if name in schemas.CONSEQUENTIAL else "allow"
        ok = bool(payload.get("ok"))
        return self._finish(name, args, decision, payload, started, cycle_id, mode, scopes, ok=ok)

    # -- research tools ---------------------------------------------------
    def _web_search(
        self, args: dict, cycle_id: str, mode: str, started: float
    ) -> ToolOutcome:
        if self.llm is None:
            return self._finish(
                "web_search",
                args,
                "error",
                {"ok": False, "error": "web_search_unavailable"},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        query = str(args.get("query", "")).strip()
        if not query:
            return self._finish(
                "web_search",
                args,
                "error",
                {"ok": False, "error": "query_required"},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        try:
            max_results = int(args.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 10))
        try:
            payload = self.llm.web_search(query, max_results)
        except LLMError as exc:
            return self._finish(
                "web_search",
                args,
                "error",
                {"ok": False, "error": str(exc)},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        return self._finish("web_search", args, "allow", payload, started, cycle_id, mode, ["web"])

    def _web_fetch(
        self, args: dict, cycle_id: str, mode: str, started: float
    ) -> ToolOutcome:
        url = str(args.get("url", "")).strip()
        if not url:
            return self._finish(
                "web_fetch",
                args,
                "error",
                {"ok": False, "error": "url_required"},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        try:
            max_chars = int(args.get("max_chars", 20_000))
        except (TypeError, ValueError):
            max_chars = 20_000
        max_chars = max(1_000, min(max_chars, 60_000))
        try:
            payload = fetch_url(url, max_chars)
        except WebError as exc:
            return self._finish(
                "web_fetch",
                args,
                "error",
                {"ok": False, "error": str(exc)},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        except Exception as exc:
            return self._finish(
                "web_fetch",
                args,
                "error",
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                started,
                cycle_id,
                mode,
                ["web"],
            )
        return self._finish("web_fetch", args, "allow", payload, started, cycle_id, mode, ["web"])

    def _spawn_subagent(
        self,
        args: dict,
        broker: ApprovalBroker,
        deadline: float | None,
        cycle_id: str,
        mode: str,
        started: float,
    ) -> ToolOutcome:
        task = str(args.get("task", "")).strip()
        if not task or len(task) > 4_000:
            return self._finish(
                "spawn_subagent",
                args,
                "error",
                {"ok": False, "error": "task_required"},
                started,
                cycle_id,
                mode,
                ["subagent"],
            )
        if self.subagent_runner is None:
            return self._finish(
                "spawn_subagent",
                args,
                "error",
                {"ok": False, "error": "subagent_unavailable"},
                started,
                cycle_id,
                mode,
                ["subagent"],
            )
        if self.depth >= self.config.budgets.max_subagent_depth:
            return self._finish(
                "spawn_subagent",
                args,
                "error",
                {"ok": False, "error": "max_subagent_depth"},
                started,
                cycle_id,
                mode,
                ["subagent"],
            )
        if self._subagent_count >= self.config.budgets.max_subagents:
            return self._finish(
                "spawn_subagent",
                args,
                "error",
                {"ok": False, "error": "max_subagents_reached"},
                started,
                cycle_id,
                mode,
                ["subagent"],
            )
        try:
            max_seconds = int(args.get("max_seconds", self.config.budgets.subagent_seconds))
        except (TypeError, ValueError):
            max_seconds = self.config.budgets.subagent_seconds
        max_seconds = max(10, min(max_seconds, 180))
        self._subagent_count += 1
        try:
            payload = self.subagent_runner(
                task, max_seconds, deadline, self.depth + 1, mode, broker
            )
        except Exception as exc:
            return self._finish(
                "spawn_subagent",
                args,
                "error",
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                started,
                cycle_id,
                mode,
                ["subagent"],
            )
        if not isinstance(payload, dict):
            payload = {"ok": True, "text": str(payload)}
        return self._finish(
            "spawn_subagent", args, "allow", payload, started, cycle_id, mode, ["subagent"]
        )

    def _schedule_wakeup(
        self, args: dict, cycle_id: str, mode: str, started: float
    ) -> ToolOutcome:
        if self.state is None:
            return self._finish(
                "schedule_wakeup",
                args,
                "error",
                {"ok": False, "error": "wakeups_unavailable"},
                started,
                cycle_id,
                mode,
                ["wakeup"],
            )
        try:
            delay = int(args.get("delay_minutes", 0))
        except (TypeError, ValueError):
            delay = 0
        if delay < 1 or delay > 10_080:
            return self._finish(
                "schedule_wakeup",
                args,
                "error",
                {"ok": False, "error": "delay_out_of_range"},
                started,
                cycle_id,
                mode,
                ["wakeup"],
            )
        prompt = str(args.get("prompt", "")).strip()
        if not prompt or len(prompt) > 4_000:
            return self._finish(
                "schedule_wakeup",
                args,
                "error",
                {"ok": False, "error": "prompt_required"},
                started,
                cycle_id,
                mode,
                ["wakeup"],
            )
        due = time.time() + delay * 60
        try:
            wake = self.state.add_wakeup(due, prompt)
        except ValueError as exc:
            return self._finish(
                "schedule_wakeup",
                args,
                "error",
                {"ok": False, "error": str(exc)},
                started,
                cycle_id,
                mode,
                ["wakeup"],
            )
        due_iso = datetime.fromtimestamp(due, tz=timezone.utc).isoformat(timespec="seconds")
        return self._finish(
            "schedule_wakeup",
            args,
            "allow",
            {"ok": True, "id": wake["id"], "due_at": due_iso, "delay_minutes": delay},
            started,
            cycle_id,
            mode,
            ["wakeup"],
        )

    # -- helpers ----------------------------------------------------------
    def _memory_tool(
        self, name: str, args: dict, cycle_id: str, mode: str, started: float
    ) -> ToolOutcome:
        if self.memory is None:
            return self._finish(
                name,
                args,
                "error",
                {"ok": False, "error": "memory_unavailable"},
                started,
                cycle_id,
                mode,
                ["memory"],
            )
        if name == "read_memory":
            try:
                limit = int(args.get("max_chars", 20_000) or 20_000)
            except (TypeError, ValueError):
                limit = 20_000
            limit = max(1_000, min(limit, 60_000))
            text = self.memory.read()
            truncated = len(text) > limit
            if truncated:
                text = "..." + text[-limit:]
            return self._finish(
                name,
                args,
                "allow",
                {"ok": True, "text": text, "truncated": truncated},
                started,
                cycle_id,
                mode,
                ["memory"],
            )

        text = str(args.get("text", ""))
        if not text.strip():
            return self._finish(
                name,
                args,
                "error",
                {"ok": False, "error": "text_required"},
                started,
                cycle_id,
                mode,
                ["memory"],
            )
        if len(text) > 4_000:
            return self._finish(
                name,
                args,
                "error",
                {"ok": False, "error": "text_too_large"},
                started,
                cycle_id,
                mode,
                ["memory"],
            )
        self.memory.append_assistant(text)
        return self._finish(
            name,
            args,
            "allow",
            {"ok": True, "bytes": len(text.encode("utf-8"))},
            started,
            cycle_id,
            mode,
            ["memory"],
        )

    def _check_paths(self, name: str, args: dict) -> list[str]:
        scopes: list[str] = []
        for key, writable in _PATH_ARGS.get(name, {}).items():
            raw = args.get(key)
            if raw is None:
                raw = "/work"
                args[key] = raw
            resolved = self.policy.resolve(raw, for_write=writable)
            args[key] = resolved.path
            scope = f"{resolved.root}:{'rw' if resolved.writable else 'ro'}"
            if scope not in scopes:
                scopes.append(scope)
        if name == "run_bash":
            scopes = ["work:rw"] + [
                f"{root.slug}:{'rw' if root.writable else 'ro'}" for root in self.policy.roots
            ]
        if name == "send_email":
            scopes = ["email"]
        return scopes

    def _timeout(self, name: str, args: dict, deadline: float | None) -> float:
        if name == "run_bash":
            requested = int(args.get("timeout_seconds", 20))
        else:
            requested = self.config.budgets.tool_timeout_seconds
        timeout = min(requested, self.config.budgets.tool_timeout_seconds)
        timeout = max(1, timeout)
        if deadline is not None:
            remaining = max(1.0, deadline - time.monotonic())
            timeout = min(timeout, remaining)
        return float(timeout)

    def _dispatch(self, name: str, args: dict, *, deadline: float | None) -> dict:
        timeout = self._timeout(name, args, deadline)
        max_out = self.config.budgets.max_tool_output_bytes

        if name == "send_email":
            if not self.mailer or not self.mailer.available():
                return {"ok": False, "error": "email_not_configured"}
            try:
                return self.mailer.send(
                    str(args.get("to", "")), str(args.get("subject", "")), str(args.get("body", ""))
                )
            except EmailError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if name == "run_bash":
            result = self.sandbox.run_bash(
                str(args.get("command", "")), timeout=timeout, max_output_bytes=max_out
            )
            return result.as_dict()

        op = schemas.OPS.get(name)
        if op is None:
            return {"ok": False, "error": f"no operation for {name!r}"}

        # Keep only the arguments the in-container op understands.
        allowed = {
            "path", "src", "dst", "depth", "max_entries", "max_bytes", "glob",
            "pattern", "max_results", "content", "create_parents", "overwrite",
        }
        payload = {k: v for k, v in args.items() if k in allowed}
        result = self.sandbox.exec_ops(op, payload, timeout=timeout, max_output_bytes=max_out)
        if not result.ok and not result.stdout:
            return {"ok": False, "error": result.stderr or "sandbox_error"}
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid sandbox response", "raw": result.stdout[:500]}
        if result.truncated:
            parsed["truncated"] = True
        if result.timed_out:
            parsed["timed_out"] = True
        return parsed

    def _read_existing(self, path: str) -> str:
        try:
            result = self.sandbox.exec_ops(
                "read",
                {"path": path, "max_bytes": 30_000},
                timeout=self.config.budgets.tool_timeout_seconds,
                max_output_bytes=self.config.budgets.max_tool_output_bytes,
            )
            if result.ok:
                parsed = json.loads(result.stdout)
                return str(parsed.get("text", ""))
        except Exception:
            pass
        return ""

    def _preview(self, name: str, args: dict) -> str:
        if name == "run_bash":
            command = str(args.get("command", ""))
            return f"Run INSIDE the sandbox (cwd /work, no network):\n\n{command[:4000]}"
        if name == "write_file":
            path = str(args.get("path"))
            new = str(args.get("content", ""))
            old = self._read_existing(path)
            if old:
                diff = list(
                    difflib.unified_diff(
                        old.splitlines(),
                        new.splitlines(),
                        fromfile=f"{path} (current)",
                        tofile=f"{path} (new)",
                        lineterm="",
                    )
                )
                body = "\n".join(diff[:80])
                if len(diff) > 80:
                    body += f"\n... ({len(diff) - 80} more diff lines)"
            else:
                lines = new.splitlines()[:40]
                body = "(new file)\n" + "\n".join(lines)
                if len(new.splitlines()) > 40:
                    body += "\n..."
            return f"Write {path} ({len(new.encode('utf-8'))} bytes):\n\n{body}"
        if name == "append_file":
            path = str(args.get("path"))
            text = str(args.get("content", ""))
            return f"Append {len(text.encode('utf-8'))} bytes to {path}:\n\n{text[:2000]}"
        if name == "delete_path":
            path = str(args.get("path"))
            info = self.sandbox.exec_ops(
                "stat",
                {"path": path},
                timeout=10,
                max_output_bytes=20_000,
            )
            detail = info.stdout if info.ok else "(not found)"
            return f"Move to the trash folder in its writable root: {path}\n\n{detail}"
        if name == "move_path":
            return f"Move {args.get('src')} -> {args.get('dst')}"
        if name == "copy_path":
            return f"Copy {args.get('src')} -> {args.get('dst')}"
        if name == "make_dir":
            return f"Create directory {args.get('path')}"
        if name == "send_email":
            return (
                f"Send email to {args.get('to')}\n"
                f"Subject: {args.get('subject')}\n\n{str(args.get('body', ''))[:3000]}"
            )
        return f"{name} {json.dumps(args, ensure_ascii=False)[:2000]}"

    @staticmethod
    def _audit_args(name: str, args: dict) -> dict:
        """Keep the activity log concise: metadata, not file/email contents."""
        if name == "web_search":
            return {"query": str(args.get("query", ""))[:200], "max_results": args.get("max_results")}
        if name == "web_fetch":
            return {"url": str(args.get("url", ""))[:500]}
        if name == "spawn_subagent":
            return {
                "task": str(args.get("task", ""))[:200],
                "max_seconds": args.get("max_seconds"),
            }
        if name == "schedule_wakeup":
            return {
                "delay_minutes": args.get("delay_minutes"),
                "bytes": len(str(args.get("prompt", "")).encode("utf-8")),
            }
        if name == "append_memory":
            return {"bytes": len(str(args.get("text", "")).encode("utf-8"))}
        if name == "read_memory":
            return {"max_chars": args.get("max_chars")}
        if name in ("write_file", "append_file"):
            content = str(args.get("content", ""))
            return {"path": args.get("path"), "bytes": len(content.encode("utf-8"))}
        if name == "send_email":
            body = str(args.get("body", ""))
            return {
                "to": args.get("to"),
                "subject": args.get("subject"),
                "bytes": len(body.encode("utf-8")),
            }
        return args

    def _finish(
        self,
        name: str,
        args: dict,
        decision: str,
        payload: dict,
        started: float,
        cycle_id: str,
        mode: str,
        scopes: list[str],
        *,
        ok: bool | None = None,
    ) -> ToolOutcome:
        duration = time.time() - started
        success = bool(payload.get("ok")) if ok is None else ok
        content = json.dumps(payload, ensure_ascii=False)
        self.audit.record(
            "tool.call",
            cycle_id=cycle_id,
            mode=mode,
            tool=name,
            args=self._audit_args(name, args),
            decision=decision,
            ok=success,
            duration_ms=round(duration * 1000),
            scopes=scopes,
        )
        if self.on_event:
            marker = "ok" if success else decision
            self.on_event(f"{name} [{marker}] {round(duration, 2)}s")
        return ToolOutcome(
            ok=success,
            tool=name,
            decision=decision,
            content=content,
            duration=duration,
            scopes=scopes,
        )
