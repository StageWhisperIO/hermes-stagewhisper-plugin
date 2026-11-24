from __future__ import annotations

import asyncio
import socket
from collections import deque
from typing import Any

import pytest

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import CallbackHandle
from hermes_stagewhisper_plugin.models import REASON_TRANSCRIPT_CHUNK, ValidatedEvent
from hermes_stagewhisper_plugin.reasoning_buffer import ReasoningReplyBuffer


TEST_TOKEN = "test-token-abcdef1234567890"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_adapter() -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(extra={"token": TEST_TOKEN, "listen_port": _free_port()})
    )


def _insight_dispatch_event(task_id: str, session_id: str) -> ValidatedEvent:
    return ValidatedEvent(
        task_id=task_id,
        session_id=session_id,
        reason=REASON_TRANSCRIPT_CHUNK,
        occurred_at="2026-01-01T00:00:00Z",
        text="the candidate mentioned pricing objections",
        is_final=True,
        user_message_id=None,
        parent_message_id=None,
        callback_url=None,
        callback_token=None,
        raw={},
    )


def _chat_dispatch_event(task_id: str, session_id: str) -> ValidatedEvent:
    return ValidatedEvent(
        task_id=task_id,
        session_id=session_id,
        reason="chat_message",
        occurred_at="2026-01-01T00:00:00Z",
        text="hello",
        is_final=None,
        user_message_id="umid-" + task_id,
        parent_message_id=None,
        callback_url=None,
        callback_token=None,
        raw={},
    )


async def _dispatch_and_wait(adapter: StageWhisperAdapter, event: ValidatedEvent) -> None:
    adapter.accept_event(event)
    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_only_the_last_send_before_an_insight_dispatch_completes_reaches_the_reply_stream() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "Redirected current run (iteration 1/90).")
        await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")

    adapter.handle_message = handler

    session_id = "insights:session-a"
    event = _insight_dispatch_event("11111111-1111-1111-1111-111111111111", session_id)

    await _dispatch_and_wait(adapter, event)

    retained = adapter.streams.retained(session_id)
    assert len(retained) == 1
    assert retained[0].payload["status"] == "message"
    assert retained[0].payload["reply_text"] == "Ask about their renewal timeline."
    assert retained[0].payload["task_id"] == event.task_id


@pytest.mark.asyncio
async def test_a_single_send_on_an_insight_dispatch_still_reaches_the_reply_stream_once_it_completes() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")

    adapter.handle_message = handler

    session_id = "insights:session-b"
    event = _insight_dispatch_event("22222222-2222-2222-2222-222222222222", session_id)

    await _dispatch_and_wait(adapter, event)

    retained = adapter.streams.retained(session_id)
    assert len(retained) == 1
    assert retained[0].payload["reply_text"] == "Ask about their renewal timeline."


@pytest.mark.asyncio
async def test_an_insight_dispatch_that_never_sends_delivers_no_reply_to_the_stream() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        return None

    adapter.handle_message = handler

    session_id = "insights:session-c"
    event = _insight_dispatch_event("33333333-3333-3333-3333-333333333333", session_id)

    await _dispatch_and_wait(adapter, event)

    assert adapter.streams.retained(session_id) == []


@pytest.mark.asyncio
async def test_a_chat_dispatch_still_delivers_every_send_immediately_and_is_not_buffered() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "let me check that")
        await adapter.send(event.source.chat_id, "here is the answer")

    adapter.handle_message = handler

    session_id = "session-d"
    event = _chat_dispatch_event("44444444-4444-4444-4444-444444444444", session_id)

    await _dispatch_and_wait(adapter, event)

    retained = adapter.streams.retained(session_id)
    assert [
        entry.payload["reply_text"]
        for entry in retained
        if entry.payload["status"] == "message"
    ] == [
        "let me check that",
        "here is the answer",
    ]
    assert retained[-1].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_a_reasoning_send_that_arrives_after_the_dispatch_has_flushed_is_delivered_immediately() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")

    adapter.handle_message = handler

    session_id = "insights:session-late"
    event = _insight_dispatch_event("55555555-5555-5555-5555-555555555555", session_id)

    await _dispatch_and_wait(adapter, event)

    handle = adapter._task_callbacks[event.task_id]
    late = await adapter.send(
        handle.chat_id, "Mention the trial extension option.", reply_to=event.task_id
    )

    assert late.success is True
    retained = adapter.streams.retained(session_id)
    assert [entry.payload["reply_text"] for entry in retained] == [
        "Ask about their renewal timeline.",
        "Mention the trial extension option.",
    ]


@pytest.mark.asyncio
async def test_no_reasoning_flush_state_remains_for_a_task_once_its_callback_handle_is_released() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")

    adapter.handle_message = handler

    session_id = "insights:session-cleanup"
    event = _insight_dispatch_event("66666666-6666-6666-6666-666666666666", session_id)

    await _dispatch_and_wait(adapter, event)
    handle = adapter._task_callbacks[event.task_id]
    assert adapter._reasoning_pending.has_flushed(event.task_id) is True

    adapter._release_task(handle)

    assert adapter._reasoning_pending.has_flushed(event.task_id) is False


@pytest.mark.asyncio
async def test_the_message_id_returned_by_send_matches_the_id_delivered_by_the_flush() -> None:
    adapter = _make_adapter()
    send_results: list[Any] = []

    async def handler(event: Any) -> None:
        result = await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")
        send_results.append(result)

    adapter.handle_message = handler

    session_id = "insights:session-idmatch"
    event = _insight_dispatch_event("99999999-9999-9999-9999-999999999991", session_id)

    await _dispatch_and_wait(adapter, event)

    retained = adapter.streams.retained(session_id)
    assert len(retained) == 1
    assert send_results[0].success is True
    assert retained[0].payload["message_id"] == send_results[0].message_id


@pytest.mark.asyncio
async def test_the_message_id_returned_by_a_late_post_flush_send_matches_the_id_actually_delivered() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send(event.source.chat_id, "Ask about their renewal timeline.")

    adapter.handle_message = handler

    session_id = "insights:session-idmatch-late"
    event = _insight_dispatch_event("99999999-9999-9999-9999-999999999992", session_id)

    await _dispatch_and_wait(adapter, event)

    handle = adapter._task_callbacks[event.task_id]
    late = await adapter.send(
        handle.chat_id, "Mention the trial extension option.", reply_to=event.task_id
    )

    retained = adapter.streams.retained(session_id)
    assert late.success is True
    assert retained[-1].payload["message_id"] == late.message_id


async def _run_interrupted_reasoning_turn(adapter: StageWhisperAdapter, task_id: str) -> None:
    session_id = "insights:session-interrupt-repeat"
    chat_id = f"sw:{session_id}:reasoning"
    event = _insight_dispatch_event(task_id, session_id)
    handle = CallbackHandle(
        task_id=event.task_id,
        session_id=event.session_id,
        user_message_id=event.user_message_id,
        callback_url="",
        callback_token="",
        chat_id=chat_id,
        is_reasoning=True,
    )
    adapter._callbacks[chat_id] = deque([handle])
    adapter._task_callbacks[handle.task_id] = handle
    adapter.inflight[handle.task_id] = asyncio.Event()

    started = asyncio.Event()
    resume = asyncio.Event()

    async def handler(_event: Any) -> None:
        await adapter.send(chat_id, "narrating the call")
        started.set()
        await resume.wait()

    adapter.handle_message = handler

    dispatch_task = asyncio.create_task(adapter._dispatch(event, handle))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await adapter.interrupt_session_activity(event.session_id, chat_id)
    resume.set()
    await asyncio.wait_for(dispatch_task, timeout=5.0)


@pytest.mark.asyncio
async def test_an_interrupted_reasoning_turn_leaves_no_entry_in_the_flushed_set() -> None:
    adapter = _make_adapter()
    task_id = "77777777-7777-7777-7777-777777777777"

    await _run_interrupted_reasoning_turn(adapter, task_id)

    assert adapter._reasoning_pending.has_flushed(task_id) is False


@pytest.mark.asyncio
async def test_repeated_interruptions_do_not_accumulate_entries_in_the_flushed_set() -> None:
    adapter = _make_adapter()
    task_ids = [
        "88888888-8888-8888-8888-888888888881",
        "88888888-8888-8888-8888-888888888882",
        "88888888-8888-8888-8888-888888888883",
        "88888888-8888-8888-8888-888888888884",
        "88888888-8888-8888-8888-888888888885",
    ]

    for task_id in task_ids:
        await _run_interrupted_reasoning_turn(adapter, task_id)

    assert all(
        adapter._reasoning_pending.has_flushed(task_id) is False for task_id in task_ids
    )
    assert len(adapter._reasoning_pending._flushed) == 0


def _reasoning_handle(task_id: str, terminated: bool = False) -> CallbackHandle:
    return CallbackHandle(
        task_id=task_id,
        session_id="insights:session-x",
        user_message_id=None,
        callback_url="",
        callback_token="",
        chat_id="sw:insights:session-x:reasoning",
        terminated=terminated,
        is_reasoning=True,
    )


@pytest.mark.asyncio
async def test_reasoning_reply_buffer_flushes_only_the_most_recently_captured_content() -> None:
    buffer = ReasoningReplyBuffer()
    handle = _reasoning_handle("task-1")
    delivered: list[dict[str, Any]] = []

    async def deliver(_handle: CallbackHandle, payload: dict[str, Any]) -> bool:
        delivered.append(payload)
        return True

    buffer.capture(handle.task_id, "interim narration", "message-id-interim")
    buffer.capture(handle.task_id, "final cue", "message-id-final")
    result = await buffer.flush(handle, deliver)

    assert result.payload is not None
    assert result.delivery_failed is False
    assert result.payload["reply_text"] == "final cue"
    assert result.payload["message_id"] == "message-id-final"
    assert delivered == [result.payload]


@pytest.mark.asyncio
async def test_reasoning_reply_buffer_flush_is_a_noop_when_nothing_was_captured() -> None:
    buffer = ReasoningReplyBuffer()
    handle = _reasoning_handle("task-2")

    async def deliver(_handle: CallbackHandle, _payload: dict[str, Any]) -> bool:
        raise AssertionError("deliver should not be called when nothing was captured")

    result = await buffer.flush(handle, deliver)

    assert result.payload is None
    assert result.delivery_failed is False


@pytest.mark.asyncio
async def test_reasoning_reply_buffer_flush_discards_captured_content_for_a_terminated_handle() -> None:
    buffer = ReasoningReplyBuffer()
    handle = _reasoning_handle("task-3", terminated=True)

    async def deliver(_handle: CallbackHandle, _payload: dict[str, Any]) -> bool:
        raise AssertionError("deliver should not be called for a terminated handle")

    buffer.capture(handle.task_id, "late content", "message-id-late")
    result = await buffer.flush(handle, deliver)

    assert result.payload is None
    assert result.delivery_failed is False
    assert buffer.has_flushed(handle.task_id) is False


@pytest.mark.asyncio
async def test_reasoning_reply_buffer_flush_reports_delivery_failure_for_captured_content() -> None:
    buffer = ReasoningReplyBuffer()
    handle = _reasoning_handle("task-4")

    async def deliver(_handle: CallbackHandle, _payload: dict[str, Any]) -> bool:
        return False

    buffer.capture(handle.task_id, "narrating the call", "message-id-fail")
    result = await buffer.flush(handle, deliver)

    assert result.payload is None
    assert result.delivery_failed is True
    assert buffer.has_flushed(handle.task_id) is True
