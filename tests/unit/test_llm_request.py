from __future__ import annotations

import copy
import json

from hermes_stagewhisper_plugin.llm_request import (
    LLM_MAX_MESSAGES,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_MAX_TOOL_BYTES,
    LLM_MAX_TOOLS_TOTAL_BYTES,
    ChatRequest,
    LlmRejection,
    openai_error_body,
    parse_chat_request,
)

from .llm_support import RECORDED_AI_SDK_REQUEST, SEARCH_TOOL


def _parse(**overrides) -> ChatRequest | LlmRejection:
    body = copy.deepcopy(RECORDED_AI_SDK_REQUEST)
    body.update(overrides)
    return parse_chat_request(json.dumps(body).encode("utf-8"))


def test_the_recorded_ai_sdk_request_parses_with_its_tool_history_intact() -> None:
    parsed = _parse()
    assert isinstance(parsed, ChatRequest)
    assert parsed.messages == RECORDED_AI_SDK_REQUEST["messages"]
    assert parsed.tools == [SEARCH_TOOL]
    assert parsed.tool_choice == "auto"
    assert parsed.stream is True
    assert parsed.max_tokens is None


def test_more_messages_than_the_cap_are_refused() -> None:
    parsed = _parse(messages=[{"role": "user", "content": "hi"}] * (LLM_MAX_MESSAGES + 1))
    assert isinstance(parsed, LlmRejection)
    assert (parsed.status, parsed.code) == (400, "too_many_messages")


def test_a_tool_schema_over_the_cap_is_refused() -> None:
    big = {"type": "function", "function": {"name": "searchLibrary", "description": "x" * LLM_MAX_TOOL_BYTES}}
    parsed = _parse(tools=[big])
    assert isinstance(parsed, LlmRejection)
    assert (parsed.code, parsed.param) == ("tool_schema_too_large", "tools.0")


def test_tools_over_the_total_schema_cap_are_refused() -> None:
    tool = {"type": "function", "function": {"name": "searchLibrary", "description": "x" * (LLM_MAX_TOOL_BYTES - 200)}}
    parsed = _parse(tools=[tool] * (LLM_MAX_TOOLS_TOTAL_BYTES // LLM_MAX_TOOL_BYTES + 1))
    assert isinstance(parsed, LlmRejection)
    assert (parsed.code, parsed.param) == ("tool_schema_too_large", "tools")


def test_image_parts_are_refused_because_only_text_is_accepted() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "http://169.254.169.254/"}},
            ],
        }
    ]
    parsed = _parse(messages=messages)
    assert isinstance(parsed, LlmRejection)
    assert (parsed.code, parsed.param) == ("unsupported_content", "messages.0.content")


def test_non_function_tools_and_unknown_roles_are_refused() -> None:
    assert _parse(tools=[{"type": "web_search"}]).code == "unsupported_tool"
    assert _parse(messages=[{"role": "function", "content": "x"}]).param == "messages.0"


def test_max_tokens_is_clamped_to_the_output_cap() -> None:
    parsed = _parse(max_tokens=1_000_000)
    assert isinstance(parsed, ChatRequest)
    assert parsed.max_tokens == LLM_MAX_OUTPUT_TOKENS


def test_invalid_json_and_non_object_bodies_are_refused() -> None:
    assert parse_chat_request(b"{not json").code == "invalid_json"
    assert parse_chat_request(b"[]").code == "invalid_json"
    assert parse_chat_request(b'{"messages": []}').param == "messages"


def test_errors_use_the_openai_error_envelope() -> None:
    body = openai_error_body(LlmRejection(413, "request_too_large", "Too big."))
    assert body == {
        "error": {"message": "Too big.", "type": "invalid_request_error", "param": None, "code": "request_too_large"}
    }
    assert openai_error_body(LlmRejection(429, "rate_limit_exceeded", "Slow down."))["error"]["type"] == "rate_limit_error"
    assert openai_error_body(LlmRejection(502, "upstream_error", "No."))["error"]["type"] == "server_error"
