from __future__ import annotations

import threading
import time

from clawback.agent import Agent
from clawback.approvals import ApprovalBroker
from clawback.audit import AuditLog
from clawback.config import AppConfig, Budgets
from clawback.llm.openrouter import LLMResponse
from clawback.tools.dispatch import ToolOutcome


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, *, timeout=90.0):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("unexpected extra model call")
        return self.responses.pop(0)


class RecordingDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def tool_specs(self, mode="interactive"):
        return [{"type": "function", "function": {"name": "read_file"}}]

    def execute(self, name, args, *, broker, cycle_id, mode, deadline=None):
        self.calls.append((name, args))
        return ToolOutcome(True, name, "allow", '{"ok": true}')


class StubPolicy:
    def roots_manifest(self):
        return "/work (rw)\n/ro/documents (ro)"


def _agent(tmp_path, responses, **budget_overrides):
    config = AppConfig(
        budgets=Budgets(
            max_seconds=budget_overrides.pop("max_seconds", 5),
            max_tool_calls=budget_overrides.pop("max_tool_calls", 5),
            max_model_rounds=budget_overrides.pop("max_model_rounds", 5),
        )
    )
    dispatcher = RecordingDispatcher()
    agent = Agent(
        llm=ScriptedLLM(responses),
        dispatcher=dispatcher,
        policy=StubPolicy(),
        config=config,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    return agent, dispatcher


def _tool_call(name="read_file", arguments='{"path": "/work/a.txt"}', call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _broker():
    return ApprovalBroker(interactive=False, timeout_seconds=1)


def test_tool_then_answer(tmp_path):
    agent, dispatcher = _agent(
        tmp_path,
        [
            LLMResponse(None, [_tool_call()]),
            LLMResponse("done", []),
        ],
    )
    result = agent.run("read it", mode="interactive", cycle_id="c1", broker=_broker())
    assert result.text == "done"
    assert result.tool_calls == 1
    assert result.rounds == 2
    assert dispatcher.calls == [("read_file", {"path": "/work/a.txt"})]


def test_invalid_json_arguments_are_rejected_without_dispatch(tmp_path):
    agent, dispatcher = _agent(
        tmp_path,
        [
            LLMResponse(None, [_tool_call(arguments="not json")]),
            LLMResponse("gave up", []),
        ],
    )
    result = agent.run("read it", mode="interactive", cycle_id="c1", broker=_broker())
    assert dispatcher.calls == []
    assert result.text == "gave up"
    tool_messages = [m for m in agent.llm.calls[1] if m.get("role") == "tool"]
    assert "invalid JSON arguments" in tool_messages[0]["content"]


def test_tool_budget_is_enforced(tmp_path):
    calls = [_tool_call(call_id=f"c{i}") for i in range(3)]
    agent, dispatcher = _agent(
        tmp_path,
        [LLMResponse(None, calls), LLMResponse("stopped", [])],
        max_tool_calls=1,
    )
    result = agent.run("go", mode="interactive", cycle_id="c1", broker=_broker())
    assert len(dispatcher.calls) == 1
    assert result.stopped_reason == "tool_budget"


def test_expired_deadline_stops_before_calling_the_model(tmp_path):
    agent, dispatcher = _agent(tmp_path, [])
    result = agent.run(
        "go",
        mode="scheduled",
        cycle_id="c1",
        broker=_broker(),
        deadline=time.monotonic() - 1,
    )
    assert result.stopped_reason == "deadline"
    assert agent.llm.calls == []
    assert dispatcher.calls == []


def test_cancel_event_stops_the_loop(tmp_path):
    agent, dispatcher = _agent(tmp_path, [])
    cancel = threading.Event()
    cancel.set()
    result = agent.run(
        "go", mode="interactive", cycle_id="c1", broker=_broker(), cancel=cancel
    )
    assert result.stopped_reason == "cancelled"
    assert agent.llm.calls == []


def test_zero_wall_clock_is_unlimited(tmp_path):
    agent, _ = _agent(tmp_path, [LLMResponse("done", [])], max_seconds=0)
    result = agent.run("go", mode="interactive", cycle_id="c1", broker=_broker())
    assert result.stopped_reason == "completed"


def test_system_prompt_mentions_budget(tmp_path):
    agent, _ = _agent(
        tmp_path, [], max_seconds=0, max_tool_calls=200, max_model_rounds=200
    )
    prompt = agent.system_prompt("interactive")
    assert "200 tool calls" in prompt
    assert "no wall-clock cap" in prompt


def test_history_is_prepended_to_the_conversation(tmp_path):
    agent, _ = _agent(tmp_path, [LLMResponse("ok", [])])
    agent.run(
        "second",
        mode="interactive",
        cycle_id="c1",
        broker=_broker(),
        history=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ],
    )
    messages = agent.llm.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "first"
    assert messages[2]["content"] == "answer"
    assert messages[3]["content"] == "second"


def test_audit_records_start_and_finish(tmp_path):
    agent, _ = _agent(tmp_path, [LLMResponse("hi", [])])
    agent.run("hello", mode="interactive", cycle_id="c1", broker=_broker())
    events = [e["event"] for e in agent.audit.tail(10)]
    assert "agent.start" in events
    assert "agent.finish" in events
