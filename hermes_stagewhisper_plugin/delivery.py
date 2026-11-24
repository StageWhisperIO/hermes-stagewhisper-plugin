from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Awaitable, Callable

from .callbacks import CallbackHandle
from .streams import ReplyStreams


CALLBACK_TIMEOUT_S = 5.0
CALLBACK_MAX_ATTEMPTS = 4
CALLBACK_RETRY_BACKOFF_S = 0.25


class CallbackAttemptOutcome(str, Enum):
    DELIVERED = "delivered"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


CallbackPost = Callable[
    [CallbackHandle, dict[str, Any]], Awaitable[CallbackAttemptOutcome]
]
CallbackSleep = Callable[[float], Awaitable[None]]


def classify_callback_status(status: int) -> CallbackAttemptOutcome:
    if 200 <= status < 300:
        return CallbackAttemptOutcome.DELIVERED
    if status in {408, 425, 429} or 500 <= status < 600:
        return CallbackAttemptOutcome.RETRYABLE_FAILURE
    return CallbackAttemptOutcome.PERMANENT_FAILURE


async def retry_callback(
    handle: CallbackHandle,
    payload: dict[str, Any],
    post_callback: CallbackPost,
    sleep: CallbackSleep = asyncio.sleep,
) -> bool:
    for attempt in range(CALLBACK_MAX_ATTEMPTS):
        try:
            outcome = await asyncio.wait_for(
                post_callback(handle, payload), timeout=CALLBACK_TIMEOUT_S
            )
        except Exception:
            outcome = CallbackAttemptOutcome.RETRYABLE_FAILURE
        if outcome is CallbackAttemptOutcome.DELIVERED:
            return True
        if outcome is CallbackAttemptOutcome.PERMANENT_FAILURE:
            return False
        if attempt + 1 < CALLBACK_MAX_ATTEMPTS:
            await sleep(CALLBACK_RETRY_BACKOFF_S * 2**attempt)
    return False


async def deliver_reply(
    handle: CallbackHandle,
    payload: dict[str, Any],
    streams: ReplyStreams,
    post_callback: CallbackPost,
) -> bool:
    if not handle.uses_callback:
        if streams.capture_durable(handle.session_id, payload):
            return True
        return streams.capture_durable(
            handle.session_id,
            {
                "task_id": payload.get("task_id", handle.task_id),
                "session_id": handle.session_id,
                "user_message_id": payload.get("user_message_id"),
                "status": "errored",
                "error_code": "reply_too_large",
                "error_message": "assistant reply exceeded the stream retention limit",
            },
        )
    return await retry_callback(handle, payload, post_callback)


async def deliver_progress(
    handle: CallbackHandle,
    payload: dict[str, Any],
    streams: ReplyStreams,
    post_callback: CallbackPost,
) -> bool:
    if not handle.uses_callback:
        return streams.publish_transient(handle.session_id, payload)
    try:
        outcome = await asyncio.wait_for(
            post_callback(handle, payload), timeout=CALLBACK_TIMEOUT_S
        )
    except Exception:
        return False
    return outcome is CallbackAttemptOutcome.DELIVERED
