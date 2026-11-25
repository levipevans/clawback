"""Wires the pieces together and owns their lifecycle."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import paths
from .agent import Agent, AgentResult
from .approvals import ApprovalBroker
from .audit import AuditLog
from .config import AppConfig, ConfigError, load_config, openrouter_api_key
from .controller import Controller, Status
from .inbound import EmailInbox
from .llm.openrouter import OpenRouterClient
from .memory import Conversation, MemoryStore
from .policy import Policy, build_policy
from .sandbox.docker import DockerSandbox, SandboxUnavailable
from .scheduler import Scheduler
from .state import StateStore
from .subagents import SubagentManager
from .tools.dispatch import ToolDispatcher
from .tools.email import Mailer, build_mailer

AUDIT_FILE = "audit.jsonl"
STATE_FILE = "state.json"


class BusyError(RuntimeError):
    pass


class Runtime:
    def __init__(self, profile: Path | None = None):
        self.profile = profile or paths.profile_dir()
        paths.ensure_layout(self.profile)
        self.config: AppConfig = load_config(self.profile)
        self.state = StateStore(self.profile / STATE_FILE)
        self.audit = AuditLog(self.profile / AUDIT_FILE)
        self.memory = MemoryStore(self.profile / "memory.md")
        self.conversation = Conversation()
        self.controller = Controller()
        self.gate = threading.Lock()
        self.cancel_event = threading.Event()
        self.sandbox_ok = False
        self.llm: OpenRouterClient | None = None
        self.agent: Agent | None = None
        self.inbox: EmailInbox | None = None
        self._output: Callable[[str], None] | None = None
        self._env_lock = threading.RLock()

        self.mailer: Mailer = build_mailer(self.config.email)
        self.policy: Policy
        self.sandbox: DockerSandbox
        self.dispatcher: ToolDispatcher
        self.subagent_manager = SubagentManager(
            self._run_subagent,
            on_event=self._emit,
            max_running=self.config.budgets.max_running_subagents,
        )
        self._build_environment()
        self.scheduler = Scheduler(self, on_event=lambda text: self._emit(text))

    # -- construction -----------------------------------------------------
    def _build_environment(self) -> None:
        self.policy = build_policy(self.config, self.state, self.profile)
        self.sandbox = DockerSandbox(self.policy)
        self.dispatcher = ToolDispatcher(
            policy=self.policy,
            sandbox=self.sandbox,
            audit=self.audit,
            config=self.config,
            mailer=self.mailer,
            memory=self.memory,
            state=self.state,
            subagent_manager=self.subagent_manager,
            on_event=self._emit,
        )

    def connect(self) -> None:
        key = openrouter_api_key()
        self.llm = OpenRouterClient(key, self.config.model)
        self.agent = Agent(
            llm=self.llm,
            dispatcher=self.dispatcher,
            policy=self.policy,
            config=self.config,
            audit=self.audit,
            memory=self.memory,
        )
        self.inbox = EmailInbox(
            config=self.config,
            state=self.state,
            audit=self.audit,
            mailer=self.mailer,
            agent=self.agent,
            llm=self.llm,
            memory=self.memory,
            controller=self.controller,
            gate=self.gate,
            cancel_event=self.cancel_event,
            on_event=self._emit,
        )
        self.dispatcher.llm = self.llm
        self.dispatcher.state = self.state
        self.audit.record("app.connect", model=self.config.model)

    def start_inbox(self) -> bool:
        if self.inbox is None:
            return False
        return self.inbox.start()

    def set_output(self, fn: Callable[[str], None] | None) -> None:
        self._output = fn

    def _emit(self, text: str) -> None:
        if self._output:
            try:
                self._output(text)
            except Exception:
                pass

    # -- lifecycle --------------------------------------------------------
    def start_sandbox(self) -> bool:
        with self._env_lock:
            try:
                self.sandbox.start()
                self.sandbox_ok = True
                self.audit.record("sandbox.ready", workspace=str(self.policy.sandbox))
                if self.controller.snapshot().status in (Status.STARTING, Status.SANDBOX_UNAVAILABLE):
                    self.controller.set(Status.IDLE)
                return True
            except SandboxUnavailable as exc:
                self.sandbox_ok = False
                self.audit.record("sandbox.unavailable", error=str(exc)[:1000])
                self.controller.set(Status.SANDBOX_UNAVAILABLE, str(exc)[:160])
                return False

    def reload_environment(self) -> bool:
        """Rebuild mounts after a root change. Requires the gate."""
        if not self.gate.acquire(blocking=False):
            raise BusyError("a run is in progress")
        try:
            with self._env_lock:
                self.sandbox.stop()
                self.policy = build_policy(self.config, self.state, self.profile)
                self.sandbox = DockerSandbox(self.policy)
                self.dispatcher = ToolDispatcher(
                    policy=self.policy,
                    sandbox=self.sandbox,
                    audit=self.audit,
                    config=self.config,
                    mailer=self.mailer,
                    memory=self.memory,
                    state=self.state,
                    llm=self.llm,
                    subagent_manager=self.subagent_manager,
                    on_event=self._emit,
                )
                if self.agent is not None:
                    self.agent.dispatcher = self.dispatcher
                    self.agent.policy = self.policy
                self.sandbox_ok = False
                return self.start_sandbox()
        finally:
            self.gate.release()

    def run_interactive(
        self,
        prompt: str,
        *,
        broker: ApprovalBroker,
        on_event: Callable[[str], None] | None = None,
    ) -> AgentResult:
        if self.agent is None:
            raise ConfigError("model is not connected")
        if not self.gate.acquire(blocking=False):
            raise BusyError("a scheduled cycle is running")
        try:
            self.cancel_event.clear()
            budget_seconds = self.config.budgets.max_seconds
            deadline = (
                time.monotonic() + budget_seconds if budget_seconds > 0 else None
            )
            result = self.agent.run(
                prompt,
                mode="interactive",
                cycle_id=uuid.uuid4().hex[:12],
                broker=broker,
                deadline=deadline,
                on_event=on_event,
                cancel=self.cancel_event,
                history=self.conversation.messages(),
            )
            self.conversation.add_user(prompt)
            self.conversation.add_assistant(result.text)
            try:
                self.memory.append_interaction(
                    prompt, result.text, result.tools_used, mode="interactive"
                )
            except OSError:
                pass
            return result
        except KeyboardInterrupt:
            self.cancel_event.set()
            raise
        finally:
            try:
                self.subagent_manager.wait_all(timeout_seconds=5)
            finally:
                self.gate.release()

    def reset_conversation(self) -> None:
        self.conversation.reset()

    def _run_subagent(
        self,
        task: str,
        max_seconds: int,
        parent_deadline: float | None,
        depth: int,
        permission_mode: str,
        broker: ApprovalBroker,
    ) -> dict:
        if self.llm is None:
            return {"ok": False, "error": "model_not_connected"}
        deadline = time.monotonic() + max_seconds
        if parent_deadline is not None:
            deadline = min(deadline, parent_deadline)
        dispatcher = ToolDispatcher(
            policy=self.policy,
            sandbox=self.sandbox,
            audit=self.audit,
            config=self.config,
            mailer=self.mailer,
            memory=self.memory,
            state=self.state,
            llm=self.llm,
            subagent_manager=self.subagent_manager,
            depth=depth,
            on_event=self._emit,
        )
        agent = Agent(
            llm=self.llm,
            dispatcher=dispatcher,
            policy=self.policy,
            config=self.config,
            audit=self.audit,
            memory=self.memory,
        )
        cycle_id = f"subagent-{uuid.uuid4().hex[:8]}"
        self.audit.record(
            "subagent.start",
            cycle_id=cycle_id,
            depth=depth,
            task=task[:500],
            permission_mode=permission_mode,
        )
        result = agent.run(
            task,
            mode="subagent",
            permission_mode=permission_mode,
            cycle_id=cycle_id,
            broker=broker,
            deadline=deadline,
            history=[],
            cancel=self.cancel_event,
        )
        self.audit.record(
            "subagent.finish",
            cycle_id=cycle_id,
            depth=depth,
            stopped=result.stopped_reason,
            tool_calls=result.tool_calls,
            tools=result.tools_used,
        )
        return {
            "ok": True,
            "text": result.text,
            "tools": result.tools_used,
            "stopped": result.stopped_reason,
        }

    def shutdown(self) -> None:
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            if self.inbox is not None:
                self.inbox.stop()
        except Exception:
            pass
        try:
            self.subagent_manager.wait_all(timeout_seconds=5)
        except Exception:
            pass
        try:
            self.sandbox.stop()
        except Exception:
            pass
        try:
            if self.llm is not None:
                self.llm.close()
        except Exception:
            pass
        self.controller.set(Status.STOPPED)
        self.audit.record("app.stop")

    # -- locations --------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.profile / "config.toml"

    @property
    def audit_path(self) -> Path:
        return self.profile / AUDIT_FILE

    @property
    def workspace_path(self) -> Path:
        return self.policy.sandbox

    @property
    def memory_path(self) -> Path:
        return self.memory.path
