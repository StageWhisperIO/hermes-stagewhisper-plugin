from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

LLM_MAX_BODY_BYTES = 196_608
LLM_MAX_MESSAGES = 200
LLM_MAX_TOOLS = 16
LLM_MAX_TOOL_BYTES = 8_192
LLM_MAX_TOOLS_TOTAL_BYTES = 32_768
LLM_MAX_OUTPUT_TOKENS = 8_192

MESSAGE_ROLES = {"system", "user", "assistant", "tool"}
ERROR_TYPES = {401: "authentication_error", 403: "permission_error", 404: "not_found_error", 429: "rate_limit_error"}


@dataclass(frozen=True)
class LlmRejection:
    status: int
    code: str
    message: str
    param: str | None = None


@dataclass(frozen=True)
class ChatRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_choice: Any
    stream: bool
    max_tokens: int | None


def openai_error_body(rejection: LlmRejection) -> dict[str, Any]:
    fallback_type = "server_error" if rejection.status >= 500 else "invalid_request_error"
    return {
        "error": {
            "message": rejection.message,
            "type": ERROR_TYPES.get(rejection.status, fallback_type),
            "param": rejection.param,
            "code": rejection.code,
        }
    }


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _is_text_only(content: Any) -> bool:
    if content is None or isinstance(content, str):
        return True
    return isinstance(content, list) and all(isinstance(part, dict) and part.get("type") == "text" for part in content)


def _messages_rejection(messages: Any) -> LlmRejection | None:
    if not isinstance(messages, list) or not messages:
        return LlmRejection(400, "invalid_value", "messages must be a non-empty list.", "messages")
    if len(messages) > LLM_MAX_MESSAGES:
        return LlmRejection(400, "too_many_messages", f"A request is limited to {LLM_MAX_MESSAGES} messages.", "messages")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in MESSAGE_ROLES:
            return LlmRejection(400, "invalid_value", "Each message needs a system, user, assistant or tool role.", f"messages.{index}")
        if not _is_text_only(message.get("content")):
            return LlmRejection(400, "unsupported_content", "Only text content is accepted.", f"messages.{index}.content")
    return None


def _tools_rejection(tools: list[Any]) -> LlmRejection | None:
    if len(tools) > LLM_MAX_TOOLS:
        return LlmRejection(400, "too_many_tools", f"A request is limited to {LLM_MAX_TOOLS} tools.", "tools")
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            return LlmRejection(400, "unsupported_tool", "Only function tools are accepted.", f"tools.{index}")
        if _encoded_size(tool) > LLM_MAX_TOOL_BYTES:
            return LlmRejection(400, "tool_schema_too_large", f"Each tool is limited to {LLM_MAX_TOOL_BYTES} bytes.", f"tools.{index}")
    if _encoded_size(tools) > LLM_MAX_TOOLS_TOTAL_BYTES:
        return LlmRejection(400, "tool_schema_too_large", f"All tools together are limited to {LLM_MAX_TOOLS_TOTAL_BYTES} bytes.", "tools")
    return None


def parse_chat_request(raw: bytes) -> ChatRequest | LlmRejection:
    try:
        body = json.loads(raw)
    except ValueError:
        return LlmRejection(400, "invalid_json", "The request body is not valid JSON.")
    if not isinstance(body, dict):
        return LlmRejection(400, "invalid_json", "The request body must be a JSON object.")
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        return LlmRejection(400, "invalid_value", "tools must be a list.", "tools")
    rejection = _messages_rejection(body.get("messages")) or _tools_rejection(tools)
    if rejection is not None:
        return rejection
    requested_tokens = body.get("max_tokens")
    wants_limit = isinstance(requested_tokens, int) and not isinstance(requested_tokens, bool) and requested_tokens > 0
    return ChatRequest(
        messages=body["messages"],
        tools=tools,
        tool_choice=body.get("tool_choice"),
        stream=body.get("stream") is True,
        max_tokens=min(requested_tokens, LLM_MAX_OUTPUT_TOKENS) if wants_limit else None,
    )
