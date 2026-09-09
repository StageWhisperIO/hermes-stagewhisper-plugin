from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .callbacks import CallbackHandle
from .models import build_message_payload

DeliverReply = Callable[[CallbackHandle, dict[str, Any]], Awaitable[bool]]


@dataclass
class FlushResult:
    payload: dict[str, Any] | None
    delivery_failed: bool


class ReasoningReplyBuffer:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, str]] = {}
        self._flushed: set[str] = set()

    def capture(self, task_id: str, content: str, message_id: str) -> None:
        self._pending[task_id] = (content, message_id)

    def has_flushed(self, task_id: str) -> bool:
        return task_id in self._flushed

    def discard(self, task_id: str) -> None:
        self._pending.pop(task_id, None)

    def forget(self, task_id: str) -> None:
        self._pending.pop(task_id, None)
        self._flushed.discard(task_id)

    def clear(self) -> None:
        self._pending.clear()
        self._flushed.clear()

    async def flush(
        self, handle: CallbackHandle, deliver: DeliverReply
    ) -> FlushResult:
        pending = self._pending.pop(handle.task_id, None)
        if handle.terminated:
            return FlushResult(payload=None, delivery_failed=False)
        self._flushed.add(handle.task_id)
        if pending is None:
            return FlushResult(payload=None, delivery_failed=False)
        content, message_id = pending
        payload = build_message_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            user_message_id=handle.user_message_id,
            message_id=message_id,
            reply_text=content,
        )
        delivered = await deliver(handle, payload)
        if delivered:
            return FlushResult(payload=payload, delivery_failed=False)
        return FlushResult(payload=None, delivery_failed=True)
