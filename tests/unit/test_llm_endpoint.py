from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import llm_endpoint
from hermes_stagewhisper_plugin.llm_host import HostLlm
from hermes_stagewhisper_plugin.llm_replay import chat_completion_chunks, completion_from_host
from hermes_stagewhisper_plugin.llm_request import LLM_MAX_BODY_BYTES

from .llm_support import (
    AUTH_HEADERS,
    HOST_MODEL,
    RECORDED_AI_SDK_REQUEST,
    FakeHostModel,
    host_llm_with,
    sse_events,
    start_client,
    text_completion,
    tool_call_completion,
)

REPLAY_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "chat_completions_tool_call_replay.sse"
CHAT_PATH = "/v1/chat/completions"


class HostError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _request(**overrides):
    body = copy.deepcopy(RECORDED_AI_SDK_REQUEST)
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_the_ping_advertises_chat_completions_with_the_configured_model() -> None:
    fake = FakeHostModel()
    client = await start_client(host_llm_with(fake))
    try:
        data = await (await client.post("/v1/ping", headers=AUTH_HEADERS)).json()
        assert data["ok"] is True
        assert "version" in data
        assert data["llm"] == {"api": "chat_completions", "model": HOST_MODEL}
        assert fake.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_ping_omits_the_model_when_the_host_config_names_none() -> None:
    client = await start_client(host_llm_with(FakeHostModel(), configured_model=None))
    try:
        data = await (await client.post("/v1/ping", headers=AUTH_HEADERS)).json()
        assert data["llm"] == {"api": "chat_completions"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_host_without_a_usable_async_call_llm_says_no_model_is_offered_and_answers_404() -> None:
    def missing_module():
        raise ModuleNotFoundError("No module named 'agent'")

    client = await start_client(HostLlm(loader=missing_module))
    try:
        data = await (await client.post("/v1/ping", headers=AUTH_HEADERS)).json()
        assert data["llm"] == {"api": "none"}
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())
        assert response.status == 404
        assert (await response.json())["error"]["code"] == "unknown_url"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_requests_without_the_right_pairing_token_or_from_a_foreign_host_never_reach_the_host_model() -> None:
    fake = FakeHostModel()
    client = await start_client(host_llm_with(fake))
    try:
        missing = await client.post(CHAT_PATH, headers={"Host": "127.0.0.1"}, json=_request())
        wrong = await client.post(
            CHAT_PATH, headers={"Host": "127.0.0.1", "Authorization": "Bearer wrong-token"}, json=_request()
        )
        foreign = await client.post(CHAT_PATH, headers={**AUTH_HEADERS, "Host": "evil.example"}, json=_request())
        assert (missing.status, wrong.status, foreign.status) == (401, 401, 403)
        assert (await wrong.json()) == {"error": "bad_bearer"}
        assert fake.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_streamed_text_answer_replays_as_standard_chat_completion_chunks() -> None:
    client = await start_client(host_llm_with(FakeHostModel([text_completion("Sealand needs a new contract [e1].")])))
    try:
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())
        body = await response.text()
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        assert body.startswith(": keep-alive\n\n")
        events = sse_events(body)
        assert events[-1] == "[DONE]"
        chunks = events[:-1]
        assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "Sealand needs a new contract [e1]."}
        assert chunks[-1]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_streamed_tool_call_replays_as_one_tool_calls_delta_with_full_arguments() -> None:
    client = await start_client(host_llm_with(FakeHostModel([tool_call_completion()])))
    try:
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())
        chunks = sse_events(await response.text())[:-1]
        tool_deltas = [chunk["choices"][0]["delta"]["tool_calls"] for chunk in chunks if "tool_calls" in chunk["choices"][0]["delta"]]
        assert tool_deltas == [
            [
                {
                    "index": 0,
                    "id": "call_host_1",
                    "type": "function",
                    "function": {"name": "searchLibrary", "arguments": '{"query":"Sealand"}'},
                }
            ]
        ]
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    finally:
        await client.close()


def test_the_tool_call_replay_matches_the_fixture_checked_against_the_ai_sdk() -> None:
    completion = completion_from_host(tool_call_completion(text="Let me look through your calls."), HOST_MODEL)
    assert chat_completion_chunks(completion) == REPLAY_FIXTURE.read_bytes()


@pytest.mark.asyncio
async def test_tool_calls_and_tool_results_in_history_reach_the_host_model_unchanged() -> None:
    fake = FakeHostModel([text_completion("Next Tuesday.")])
    client = await start_client(host_llm_with(fake))
    try:
        await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(model="gpt-5-pro"))
        sent = fake.calls[0]
        assert sent["messages"] == RECORDED_AI_SDK_REQUEST["messages"]
        assert sent["tools"] == RECORDED_AI_SDK_REQUEST["tools"]
        assert sent["extra_body"] is None
        assert sent["model"] is None and sent["provider"] is None and sent["task"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tool_choice_none_is_passed_on_and_a_tool_call_the_host_still_makes_is_dropped() -> None:
    fake = FakeHostModel([tool_call_completion(text="Here is what I found.")])
    client = await start_client(host_llm_with(fake))
    try:
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(tool_choice="none"))
        assert fake.calls[0]["extra_body"] == {"tool_choice": "none"}
        chunks = sse_events(await response.text())[:-1]
        assert not any("tool_calls" in chunk["choices"][0]["delta"] for chunk in chunks)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_non_streamed_request_gets_one_chat_completion_with_the_host_model_name() -> None:
    client = await start_client(host_llm_with(FakeHostModel([tool_call_completion()])))
    try:
        body = await (await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(stream=False))).json()
        assert body["object"] == "chat.completion"
        assert body["model"] == HOST_MODEL
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "searchLibrary"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_over_cap_body_is_refused_with_an_openai_style_413_before_the_host_model_runs() -> None:
    fake = FakeHostModel()
    client = await start_client(host_llm_with(fake))
    try:
        oversized = _request()
        oversized["messages"][1]["content"] = "x" * LLM_MAX_BODY_BYTES
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=oversized)
        assert response.status == 413
        assert (await response.json())["error"] == {
            "message": f"The request body is limited to {LLM_MAX_BODY_BYTES} bytes.",
            "type": "invalid_request_error",
            "param": None,
            "code": "request_too_large",
        }
        assert fake.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_too_many_tools_are_refused_with_an_openai_style_400() -> None:
    client = await start_client(host_llm_with(FakeHostModel()))
    try:
        tools = [{"type": "function", "function": {"name": f"tool_{index}"}} for index in range(17)]
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(tools=tools))
        assert response.status == 400
        error = (await response.json())["error"]
        assert error["code"] == "too_many_tools" and error["param"] == "tools"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_slow_host_model_keeps_the_stream_alive_with_heartbeats_before_the_answer(monkeypatch) -> None:
    monkeypatch.setattr(llm_endpoint, "STREAM_HEARTBEAT_SECONDS", 0.05)

    async def slow_answer():
        await asyncio.sleep(0.3)
        return text_completion("Done.")

    client = await start_client(host_llm_with(FakeHostModel([slow_answer])))
    try:
        body = await (await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())).text()
        assert body.count(": keep-alive\n\n") > 1
        events = sse_events(body)
        assert events[0]["choices"][0]["delta"]["content"] == "Done."
        assert events[-1] == "[DONE]"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_host_failure_while_streaming_arrives_as_an_in_stream_error_without_the_providers_text() -> None:
    client = await start_client(host_llm_with(FakeHostModel([HostError("rate limited for key sk-live-123", 429)])))
    try:
        response = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())
        body = await response.text()
        events = sse_events(body)
        assert events[0]["error"]["code"] == "rate_limit_exceeded"
        assert events[-1] == "[DONE]"
        assert "sk-live-123" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_host_auth_failure_turns_the_endpoint_off_until_the_next_ping() -> None:
    fake = FakeHostModel([HostError("invalid api key", 401), text_completion("Back.")])
    client = await start_client(host_llm_with(fake))
    try:
        refused = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(stream=False))
        assert refused.status == 502
        assert (await refused.json())["error"]["code"] == "upstream_auth_failed"
        while_off = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(stream=False))
        assert while_off.status == 404
        assert len(fake.calls) == 1
        ping = await (await client.post("/v1/ping", headers=AUTH_HEADERS)).json()
        assert "llm" in ping
        after_ping = await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request(stream=False))
        assert after_ping.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_bodies_never_reach_the_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = await start_client(host_llm_with(FakeHostModel([HostError("boom", 500)])))
    try:
        await client.post(CHAT_PATH, headers=AUTH_HEADERS, json=_request())
        assert "Sealand" not in caplog.text
    finally:
        await client.close()
