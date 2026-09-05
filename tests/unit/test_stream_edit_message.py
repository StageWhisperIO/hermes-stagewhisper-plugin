from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.models import ValidatedEvent


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


def _stream_chunks(adapter: StageWhisperAdapter, session_id: str) -> list[dict[str, Any]]:
    return [
        entry.payload["chunk"]
        for entry in adapter.streams.retained(session_id)
        if entry.payload["status"] == "stream"
    ]


def test_supports_draft_streaming_reports_true_so_the_consumer_prefers_the_draft_path() -> None:
    adapter = _make_adapter()

    assert adapter.supports_draft_streaming() is True


@pytest.mark.asyncio
async def test_a_plain_append_emits_one_delta_carrying_only_the_new_suffix() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await adapter.edit_message(event.source.chat_id, "m1", "Hello world")
        await adapter.edit_message(event.source.chat_id, "m1", "Hello world again")
        await adapter.send(event.source.chat_id, "Hello world again")

    adapter.handle_message = handler

    session_id = "session-append"
    event = _chat_dispatch_event("11111111-1111-1111-1111-111111111111", session_id)

    await _dispatch_and_wait(adapter, event)

    deltas = [c for c in _stream_chunks(adapter, session_id) if c["type"] == "text-delta"]
    assert [c["delta"] for c in deltas] == ["Hello", " world"]
    assert deltas[0]["id"] == deltas[1]["id"]


@pytest.mark.asyncio
async def test_a_non_prefix_rewrite_closes_the_current_part_and_opens_a_new_one() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await adapter.edit_message(event.source.chat_id, "m1", "Goodbye")
        await adapter.edit_message(event.source.chat_id, "m1", "Goodbye")
        await adapter.send(event.source.chat_id, "Goodbye")

    adapter.handle_message = handler

    session_id = "session-rewrite"
    event = _chat_dispatch_event("22222222-2222-2222-2222-222222222222", session_id)

    await _dispatch_and_wait(adapter, event)

    chunks = _stream_chunks(adapter, session_id)
    types = [c["type"] for c in chunks]
    assert types == [
        "start",
        "text-start",
        "text-delta",
        "text-end",
        "text-start",
        "text-delta",
    ]
    first_delta, text_end, second_start, second_delta = chunks[2], chunks[3], chunks[4], chunks[5]
    assert first_delta["id"] == text_end["id"]
    assert second_start["id"] == second_delta["id"]
    assert second_start["id"] != first_delta["id"]
    assert second_delta["delta"] == "Goodbye"


@pytest.mark.asyncio
async def test_the_first_delta_of_a_turn_emits_start_and_text_start_exactly_once() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hi")
        await adapter.edit_message(event.source.chat_id, "m1", "Hi there")
        await adapter.edit_message(event.source.chat_id, "m1", "Hi there!")
        await adapter.send(event.source.chat_id, "Hi there!")

    adapter.handle_message = handler

    session_id = "session-start-once"
    event = _chat_dispatch_event("33333333-3333-3333-3333-333333333333", session_id)

    await _dispatch_and_wait(adapter, event)

    chunks = _stream_chunks(adapter, session_id)
    assert sum(1 for c in chunks if c["type"] == "start") == 1
    assert sum(1 for c in chunks if c["type"] == "text-start") == 1
    assert chunks[0] == {"type": "start", "messageId": event.task_id}


@pytest.mark.asyncio
async def test_finalize_emits_text_end_then_finish() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await adapter.edit_message(event.source.chat_id, "m1", "Hello world", finalize=True)
        await adapter.send(event.source.chat_id, "Hello world")

    adapter.handle_message = handler

    session_id = "session-finalize"
    event = _chat_dispatch_event("44444444-4444-4444-4444-444444444444", session_id)

    await _dispatch_and_wait(adapter, event)

    chunks = _stream_chunks(adapter, session_id)
    assert chunks[-2]["type"] == "text-end"
    assert chunks[-1] == {"type": "finish", "finishReason": "stop"}


@pytest.mark.asyncio
async def test_the_terminal_message_frame_still_carries_the_authoritative_final_text() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await adapter.edit_message(event.source.chat_id, "m1", "Hello world", finalize=True)
        await adapter.send(event.source.chat_id, "Hello world")

    adapter.handle_message = handler

    session_id = "session-terminal-message"
    event = _chat_dispatch_event("99999999-9999-9999-9999-999999999999", session_id)

    await _dispatch_and_wait(adapter, event)

    retained = adapter.streams.retained(session_id)
    message_entries = [entry for entry in retained if entry.payload["status"] == "message"]
    assert len(message_entries) == 1
    assert message_entries[0].payload["reply_text"] == "Hello world"


@pytest.mark.asyncio
async def test_send_draft_streams_deltas_the_same_way_as_edit_message() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.send_draft(event.source.chat_id, 1, "Hello")
        await adapter.send_draft(event.source.chat_id, 1, "Hello world")
        await adapter.send_draft(event.source.chat_id, 1, "Hello world again")
        await adapter.send(event.source.chat_id, "Hello world again")

    adapter.handle_message = handler

    session_id = "session-draft"
    event = _chat_dispatch_event("77777777-7777-7777-7777-777777777777", session_id)

    await _dispatch_and_wait(adapter, event)

    deltas = [c for c in _stream_chunks(adapter, session_id) if c["type"] == "text-delta"]
    assert [c["delta"] for c in deltas] == ["Hello", " world"]


@pytest.mark.asyncio
async def test_no_streaming_state_remains_for_a_task_once_its_turn_finalizes() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello", finalize=True)
        await adapter.send(event.source.chat_id, "Hello")

    adapter.handle_message = handler

    session_id = "session-cleanup"
    event = _chat_dispatch_event("55555555-5555-5555-5555-555555555555", session_id)

    await _dispatch_and_wait(adapter, event)

    assert event.task_id not in adapter._stream_chunks._turns


@pytest.mark.asyncio
async def test_a_turn_that_crashes_mid_stream_still_clears_its_streaming_state() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        raise RuntimeError("boom")

    adapter.handle_message = handler

    session_id = "session-error-cleanup"
    event = _chat_dispatch_event("66666666-6666-6666-6666-666666666666", session_id)

    await _dispatch_and_wait(adapter, event)

    assert event.task_id not in adapter._stream_chunks._turns


@pytest.mark.asyncio
async def test_a_turn_that_hits_the_agent_hard_timeout_still_clears_its_streaming_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_stagewhisper_plugin import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "AGENT_HARD_TIMEOUT_S", 0.05)
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        await adapter.edit_message(event.source.chat_id, "m1", "Hello")
        await asyncio.sleep(1.0)

    adapter.handle_message = handler

    session_id = "session-timeout-cleanup"
    event = _chat_dispatch_event("88888888-8888-8888-8888-888888888888", session_id)

    await _dispatch_and_wait(adapter, event)

    assert event.task_id not in adapter._stream_chunks._turns


@pytest.mark.asyncio
async def test_a_draft_rendered_with_a_trailing_cursor_streams_the_answer_exactly_once() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        chat_id = event.source.chat_id
        for snapshot in ("Emb ▉", "Embedding = ▉", "Embedding = a vector ▉"):
            await adapter.edit_message(chat_id, "m1", snapshot)
        await adapter.edit_message(chat_id, "m1", "Embedding = a vector", finalize=True)
        await adapter.send(chat_id, "Embedding = a vector")

    adapter.handle_message = handler

    session_id = "session-cursor"
    event = _chat_dispatch_event("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", session_id)

    await _dispatch_and_wait(adapter, event)

    chunks = _stream_chunks(adapter, session_id)
    assert sum(1 for c in chunks if c["type"] == "text-start") == 1
    streamed = "".join(c["delta"] for c in chunks if c["type"] == "text-delta")
    assert streamed == "Embedding = a vector"


def _messages(adapter: StageWhisperAdapter, session_id: str) -> list[dict[str, Any]]:
    return [
        entry.payload
        for entry in adapter.streams.retained(session_id)
        if entry.payload["status"] == "message"
    ]


@pytest.mark.asyncio
async def test_a_terminated_turn_still_delivers_the_final_answer_as_a_message() -> None:
    adapter = _make_adapter()
    task_id = "33333333-3333-3333-3333-333333333333"
    session_id = "session-terminated"

    async def handler(event: Any) -> None:
        adapter._task_callbacks[task_id].terminated = True
        await adapter.edit_message(
            event.source.chat_id, "m1", "The final answer", finalize=True
        )

    adapter.handle_message = handler

    await _dispatch_and_wait(adapter, _chat_dispatch_event(task_id, session_id))

    assert [m["reply_text"] for m in _messages(adapter, session_id)] == [
        "The final answer"
    ]


@pytest.mark.asyncio
async def test_a_terminated_turn_emits_no_stream_chunks_because_the_ui_turn_is_closed() -> None:
    adapter = _make_adapter()
    task_id = "44444444-4444-4444-4444-444444444444"
    session_id = "session-terminated-nochunks"

    async def handler(event: Any) -> None:
        adapter._task_callbacks[task_id].terminated = True
        await adapter.edit_message(
            event.source.chat_id, "m1", "The final answer", finalize=True
        )

    adapter.handle_message = handler

    await _dispatch_and_wait(adapter, _chat_dispatch_event(task_id, session_id))

    assert _stream_chunks(adapter, session_id) == []


@pytest.mark.asyncio
async def test_repeating_the_same_terminated_content_delivers_only_one_message() -> None:
    adapter = _make_adapter()
    task_id = "55555555-5555-5555-5555-555555555555"
    session_id = "session-terminated-repeat"

    async def handler(event: Any) -> None:
        adapter._task_callbacks[task_id].terminated = True
        chat_id = event.source.chat_id
        await adapter.edit_message(chat_id, "m1", "The final answer", finalize=False)
        await adapter.edit_message(chat_id, "m1", "The final answer", finalize=True)

    adapter.handle_message = handler

    await _dispatch_and_wait(adapter, _chat_dispatch_event(task_id, session_id))

    assert [m["reply_text"] for m in _messages(adapter, session_id)] == [
        "The final answer"
    ]


@pytest.mark.asyncio
async def test_terminated_content_that_grows_delivers_the_extended_answer_too() -> None:
    adapter = _make_adapter()
    task_id = "66666666-6666-6666-6666-666666666666"
    session_id = "session-terminated-grow"

    async def handler(event: Any) -> None:
        adapter._task_callbacks[task_id].terminated = True
        chat_id = event.source.chat_id
        await adapter.edit_message(chat_id, "m1", "Partial", finalize=False)
        await adapter.edit_message(chat_id, "m1", "Partial and complete", finalize=True)

    adapter.handle_message = handler

    await _dispatch_and_wait(adapter, _chat_dispatch_event(task_id, session_id))

    assert [m["reply_text"] for m in _messages(adapter, session_id)] == [
        "Partial",
        "Partial and complete",
    ]


@pytest.mark.asyncio
async def test_empty_terminated_content_delivers_nothing() -> None:
    adapter = _make_adapter()
    task_id = "77777777-7777-7777-7777-777777777777"
    session_id = "session-terminated-empty"

    async def handler(event: Any) -> None:
        adapter._task_callbacks[task_id].terminated = True
        await adapter.edit_message(event.source.chat_id, "m1", "", finalize=True)

    adapter.handle_message = handler

    await _dispatch_and_wait(adapter, _chat_dispatch_event(task_id, session_id))

    assert _messages(adapter, session_id) == []
