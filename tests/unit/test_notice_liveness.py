from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import CallbackHandle
from hermes_stagewhisper_plugin.models import (
    REASON_CHAT_MESSAGE,
    REASON_TRANSCRIPT_CHUNK,
    ValidatedEvent,
)


TEST_TOKEN = "test-token-abcdef1234567890"


def _make_adapter(port: int) -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(extra={"token": TEST_TOKEN, "listen_port": port})
    )


def _chat_event(*, task_id: str, session_id: str, text: str) -> ValidatedEvent:
    return ValidatedEvent(
        task_id=task_id,
        session_id=session_id,
        reason=REASON_CHAT_MESSAGE,
        occurred_at="2026-01-01T00:00:00Z",
        text=text,
        is_final=True,
        user_message_id=f"umid-{task_id}",
        parent_message_id=None,
        callback_url=None,
        callback_token=None,
        raw={},
    )


@pytest.mark.asyncio
async def test_a_notice_extends_the_turns_idle_window_by_updating_last_activity_at() -> None:
    adapter = _make_adapter(18761)
    handle = CallbackHandle(
        task_id="task-notice-activity",
        session_id="session-notice-activity",
        user_message_id="umid-notice-activity",
        callback_url="",
        callback_token="",
        chat_id="sw:session-notice-activity:chat",
    )
    adapter._callbacks[handle.chat_id] = deque([handle])

    before = handle.last_activity_at
    await asyncio.sleep(0.05)
    result = await adapter.send(
        handle.chat_id,
        "Compressing context, hang tight",
        metadata={"notice": True},
    )

    assert result.success is True
    assert handle.last_activity_at > before


@pytest.mark.asyncio
async def test_a_turn_with_no_activity_at_all_still_settles_as_silent_after_the_grace_period(
    monkeypatch,
) -> None:
    import hermes_stagewhisper_plugin.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "TURN_IDLE_GRACE_S", 0.2)

    adapter = _make_adapter(18762)

    async def silent_handler(_msg) -> None:
        return None

    adapter.handle_message = silent_handler

    event = _chat_event(
        task_id="task-fully-silent", session_id="session-fully-silent", text="hi"
    )
    adapter.accept_event(event)
    handle = adapter._task_callbacks[event.task_id]

    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

    assert handle.terminated is True
    assert handle.delivered is False
    retained = adapter.streams.retained(event.session_id)
    assert retained[-1].payload["status"] == "silent"


@pytest.mark.asyncio
async def test_a_notice_followed_by_a_real_reply_within_the_extended_window_delivers_the_real_reply(
    monkeypatch,
) -> None:
    import hermes_stagewhisper_plugin.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "TURN_IDLE_GRACE_S", 0.5)
    monkeypatch.setattr(adapter_mod, "TURN_QUIET_PERIOD_S", 0.2)

    adapter = _make_adapter(18763)
    background: dict[str, asyncio.Task] = {}

    async def handler(event: Any) -> None:
        async def deferred() -> None:
            await asyncio.sleep(0.3)
            await adapter.send(
                event.source.chat_id,
                "Compressing context, hang tight",
                metadata={"notice": True},
            )
            await asyncio.sleep(0.3)
            await adapter.send(
                event.source.chat_id, "the compacted answer is ready"
            )

        background["task"] = asyncio.create_task(deferred())

    adapter.handle_message = handler

    event = _chat_event(
        task_id="task-notice-then-reply",
        session_id="session-notice-then-reply",
        text="please compact and answer",
    )
    adapter.accept_event(event)
    handle = adapter._task_callbacks[event.task_id]

    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)
    await asyncio.wait_for(background["task"], timeout=5.0)

    retained = adapter.streams.retained(event.session_id)
    messages = [entry.payload for entry in retained if entry.payload["status"] == "message"]
    assert len(messages) == 1
    assert messages[0]["reply_text"] == "the compacted answer is ready"
    assert messages[0]["task_id"] == handle.task_id

    terminals = [
        entry.payload
        for entry in retained
        if entry.payload["status"] in ("completed", "errored", "silent")
    ]
    assert [t["status"] for t in terminals] == ["completed"]


def _reasoning_event(*, task_id: str, session_id: str) -> ValidatedEvent:
    return ValidatedEvent(
        task_id=task_id,
        session_id=session_id,
        reason=REASON_TRANSCRIPT_CHUNK,
        occurred_at="2026-01-01T00:00:00Z",
        text="ask about the renewal",
        is_final=True,
        user_message_id=None,
        parent_message_id=None,
        callback_url=None,
        callback_token=None,
        raw={},
    )


@pytest.mark.asyncio
async def test_send_with_notice_metadata_emits_a_notice_status_payload() -> None:
    adapter = _make_adapter(18764)

    async def handler(event: Any) -> None:
        await adapter.send(
            event.source.chat_id,
            "Compressing context, your message is queued.",
            metadata={"notice": True},
        )

    adapter.handle_message = handler

    session_id = "insights:session-notice"
    event = _reasoning_event(
        task_id="11111111-1111-1111-1111-111111111111", session_id=session_id
    )
    adapter.accept_event(event)
    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

    retained = adapter.streams.retained(session_id)
    assert len(retained) == 1
    assert retained[0].payload["status"] == "notice"
    assert retained[0].payload["reply_text"] == "Compressing context, your message is queued."


@pytest.mark.asyncio
async def test_a_gateway_heartbeat_marked_interim_send_emits_a_notice_not_a_terminal_message() -> (
    None
):
    adapter = _make_adapter(18766)

    async def handler(event: Any) -> None:
        await adapter.send(
            event.source.chat_id,
            "Working, 3 min, iteration 3/90, receiving stream response",
            metadata={"_interim_send": True},
        )

    adapter.handle_message = handler

    session_id = "session-heartbeat"
    event = _reasoning_event(
        task_id="33333333-3333-3333-3333-333333333333", session_id=session_id
    )
    adapter.accept_event(event)
    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

    retained = adapter.streams.retained(session_id)
    assert len(retained) == 1
    assert retained[0].payload["status"] == "notice"


@pytest.mark.asyncio
async def test_editing_a_heartbeat_notice_emits_another_notice_rather_than_answer_stream_chunks() -> (
    None
):
    adapter = _make_adapter(18767)

    async def handler(event: Any) -> None:
        first = await adapter.send(
            event.source.chat_id,
            "Working, 3 min",
            metadata={"_interim_send": True},
        )
        await adapter.edit_message(
            event.source.chat_id,
            first.message_id,
            "Working, 6 min",
        )

    adapter.handle_message = handler

    session_id = "session-heartbeat-edit"
    event = _reasoning_event(
        task_id="44444444-4444-4444-4444-444444444444", session_id=session_id
    )
    adapter.accept_event(event)
    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

    retained = adapter.streams.retained(session_id)
    assert [entry.payload["status"] for entry in retained] == ["notice", "notice"]
    assert retained[1].payload["reply_text"] == "Working, 6 min"


@pytest.mark.asyncio
async def test_a_notice_send_does_not_mark_the_handle_delivered() -> None:
    adapter = _make_adapter(18765)
    handle_ref: list[Any] = []

    async def handler(event: Any) -> None:
        handle_ref.append(adapter._task_callbacks[event.message_id])
        await adapter.send(
            event.source.chat_id, "Redirected current run.", metadata={"notice": True}
        )

    adapter.handle_message = handler

    session_id = "insights:session-notice-2"
    event = _reasoning_event(
        task_id="22222222-2222-2222-2222-222222222222", session_id=session_id
    )
    adapter.accept_event(event)
    await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

    assert handle_ref[0].delivered is False
