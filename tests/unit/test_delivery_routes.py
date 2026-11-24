from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock

import pytest

from hermes_stagewhisper_plugin import delivery as delivery_module
from hermes_stagewhisper_plugin.delivery import CallbackAttemptOutcome
from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import CallbackHandle
from hermes_stagewhisper_plugin.streams import ReplyStreams


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _adapter() -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(
            extra={
                "token": "test-token-abcdef1234567890",
                "listen_port": _free_port(),
                "listen_host": "127.0.0.1",
            }
        )
    )


def _handle(callback: bool) -> CallbackHandle:
    return CallbackHandle(
        task_id="task-a",
        session_id="session-a",
        user_message_id="message-a",
        callback_url="http://127.0.0.1:9876" if callback else "",
        callback_token="callback-token-32-chars-aaaaaaaa" if callback else "",
        chat_id="sw:session-a:chat",
    )


def _payload() -> dict[str, str]:
    return {"task_id": "task-a", "session_id": "session-a", "status": "message"}


@pytest.mark.asyncio
async def test_a_request_without_a_callback_is_captured_without_a_live_listener() -> None:
    adapter = _adapter()

    delivered = await adapter._deliver_reply(_handle(callback=False), _payload())

    subscriber = adapter.streams.subscribe("session-a")
    drained = adapter.streams.drain(subscriber)
    assert delivered is True
    assert [entry.payload for entry in drained.entries] == [_payload()]


@pytest.mark.asyncio
async def test_a_callback_selected_by_the_request_never_crosses_to_an_open_stream() -> None:
    adapter = _adapter()
    callback_post = AsyncMock(return_value=CallbackAttemptOutcome.DELIVERED)
    adapter._post = callback_post
    subscriber = adapter.streams.subscribe("session-a")

    delivered = await adapter._deliver_reply(_handle(callback=True), _payload())

    assert delivered is True
    callback_post.assert_awaited_once()
    assert adapter.streams.drain(subscriber).entries == []


@pytest.mark.asyncio
async def test_an_ambiguous_callback_failure_never_falls_across_to_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "CALLBACK_RETRY_BACKOFF_S", 0)
    adapter = _adapter()
    callback_post = AsyncMock(return_value=CallbackAttemptOutcome.RETRYABLE_FAILURE)
    adapter._post = callback_post
    subscriber = adapter.streams.subscribe("session-a")

    delivered = await adapter._deliver_reply(_handle(callback=True), _payload())

    assert delivered is False
    assert callback_post.await_count == delivery_module.CALLBACK_MAX_ATTEMPTS
    assert adapter.streams.drain(subscriber).entries == []


@pytest.mark.asyncio
async def test_an_oversized_stream_reply_becomes_a_bounded_error() -> None:
    adapter = _adapter()
    adapter.streams = ReplyStreams(
        backlog_bytes_per_session=256, max_event_bytes=256
    )
    subscriber = adapter.streams.subscribe("session-a")

    delivered = await adapter._deliver_reply(
        _handle(callback=False), {**_payload(), "reply_text": "x" * 1024}
    )

    drained = adapter.streams.drain(subscriber)
    assert delivered is True
    assert drained.entries[0].payload["error_code"] == "reply_too_large"


@pytest.mark.asyncio
async def test_a_typing_indicator_is_attempted_once_and_never_retried() -> None:
    adapter = _adapter()
    callback_post = AsyncMock(return_value=CallbackAttemptOutcome.RETRYABLE_FAILURE)
    adapter._post = callback_post

    await adapter._emit_typing(_handle(callback=True))

    callback_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_stream_typing_indicator_is_live_only_and_refreshable() -> None:
    adapter = _adapter()
    subscriber = adapter.streams.subscribe("session-a")

    await adapter._emit_typing(_handle(callback=False))

    drained = adapter.streams.drain(subscriber)
    payloads = [json.loads(payload) for payload in drained.transient_payloads]
    assert len(payloads) == 1
    assert payloads[0]["task_id"] == "task-a"
    assert payloads[0]["session_id"] == "session-a"
    assert payloads[0]["user_message_id"] == "message-a"
    assert payloads[0]["status"] == "typing"
    assert drained.entries == []
