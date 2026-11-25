"""OpenRouter client (OpenAI-compatible chat completions with tools)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        max_retries: int = 2,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "Clawback",
            },
            timeout=httpx.Timeout(120.0, connect=20.0),
        )

    def close(self) -> None:
        self._client.close()

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        timeout: float = 90.0,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=httpx.Timeout(max(5.0, timeout), connect=20.0),
                )
            except httpx.TimeoutException:
                last_error = "request timed out"
            except httpx.HTTPError as exc:
                last_error = f"network error: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return self._parse(response.json())
                body = response.text[:500]
                last_error = f"HTTP {response.status_code}: {body}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(last_error)
            if attempt < self.max_retries:
                time.sleep(0.5 * (2**attempt))
        raise LLMError(last_error)

    def _parse(self, data: dict) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("provider returned no choices") from exc
        message = choice.get("message") or {}
        tool_calls: list[dict] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments)
            name = function.get("name")
            if not name:
                continue
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments or "{}"},
                }
            )
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
        )
