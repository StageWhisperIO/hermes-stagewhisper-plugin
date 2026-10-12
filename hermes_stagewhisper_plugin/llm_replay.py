from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

SSE_DONE = b"data: [DONE]\n\n"


@dataclass(frozen=True)
class Completion:
    id: str
    model: str
    created: int
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str


def completion_from_host(response: Any, fallback_model: str) -> Completion:
    message = response.choices[0].message
    tool_calls = [
        {
            "id": call.id or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"},
        }
        for call in message.tool_calls or []
    ]
    return Completion(
        id=getattr(response, "id", None) or f"chatcmpl-{uuid.uuid4().hex}",
        model=getattr(response, "model", None) or fallback_model,
        created=getattr(response, "created", None) or int(time.time()),
        content=message.content or "",
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


def without_tool_calls(completion: Completion) -> Completion:
    return replace(completion, tool_calls=[], finish_reason="stop")


def chat_completion_json(completion: Completion) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.content or None}
    if completion.tool_calls:
        message["tool_calls"] = completion.tool_calls
    return {
        "id": completion.id,
        "object": "chat.completion",
        "created": completion.created,
        "model": completion.model,
        "choices": [{"index": 0, "message": message, "finish_reason": completion.finish_reason}],
    }


def sse_frame(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n\n"


def chat_completion_chunks(completion: Completion) -> bytes:
    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        return sse_frame(
            {
                "id": completion.id,
                "object": "chat.completion.chunk",
                "created": completion.created,
                "model": completion.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
        )

    deltas = [{"role": "assistant", "content": completion.content}]
    deltas += [{"tool_calls": [{"index": index, **call}]} for index, call in enumerate(completion.tool_calls)]
    return b"".join(chunk(delta) for delta in deltas) + chunk({}, completion.finish_reason) + SSE_DONE
