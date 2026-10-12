from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.listener import build_app
from hermes_stagewhisper_plugin.llm_host import HostLlm, HostModel

TEST_TOKEN = "test-token-abcdef1234567890"
HOST_MODEL = "deepseek-chat"
AUTH_HEADERS = {"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "searchLibrary",
        "description": "Search the user's calls.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

RECORDED_AI_SDK_REQUEST: dict[str, Any] = {
    "model": "deepseek-chat",
    "messages": [
        {"role": "system", "content": "Answer only from tool results."},
        {"role": "user", "content": "What does Sealand need now?"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "searchLibrary", "arguments": '{"query":"Sealand"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc", "content": '{"results":[{"excerptId":"e1"}]}'},
        {"role": "user", "content": "And when?"},
    ],
    "tools": [SEARCH_TOOL],
    "tool_choice": "auto",
    "stream": True,
}


def text_completion(text: str, *, model: str = HOST_MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl-host-1",
        model=model,
        created=1791700000,
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content=text, tool_calls=None),
            )
        ],
    )


def tool_call_completion(*, text: str | None = None) -> SimpleNamespace:
    call = SimpleNamespace(
        id="call_host_1",
        type="function",
        function=SimpleNamespace(name="searchLibrary", arguments='{"query":"Sealand"}'),
    )
    return SimpleNamespace(
        id="chatcmpl-host-2",
        model=HOST_MODEL,
        created=1791700000,
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="tool_calls",
                message=SimpleNamespace(role="assistant", content=text, tool_calls=[call]),
            )
        ],
    )


class FakeHostModel:
    def __init__(self, replies: list[Any] | None = None) -> None:
        self.replies: list[Any] = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        task: str | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        main_runtime: dict[str, Any] | None = None,
        messages: list,
        tools: list | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_body: dict | None = None,
    ) -> Any:
        self.calls.append(
            {
                "task": task,
                "provider": provider,
                "model": model,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "timeout": timeout,
                "extra_body": extra_body,
            }
        )
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        return reply


def make_adapter() -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(extra={"token": TEST_TOKEN, "listen_port": 18765, "listen_host": "127.0.0.1"})
    )


def host_llm_with(fake: FakeHostModel, configured_model: str | None = HOST_MODEL) -> HostLlm:
    return HostLlm(loader=lambda: HostModel(fake, (lambda: configured_model) if configured_model is not None else None))


async def start_client(host_llm: HostLlm) -> TestClient:
    client = TestClient(TestServer(build_app(make_adapter(), host_llm=host_llm)))
    await client.start_server()
    return client


def sse_events(body: str) -> list[Any]:
    events: list[Any] = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events
