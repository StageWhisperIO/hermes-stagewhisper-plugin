from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.listener import build_app


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


async def _client_for(adapter: StageWhisperAdapter) -> TestClient:
    server = TestServer(build_app(adapter))
    client = TestClient(server)
    await client.start_server()
    return client


async def _wait_until_settled(
    adapter: StageWhisperAdapter, task_id: str, timeout: float = 5.0
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while task_id in adapter.inflight:
        if loop.time() > deadline:
            raise AssertionError(f"task {task_id} never settled")
        await asyncio.sleep(0.01)


def _incoming_headers() -> dict[str, str]:
    return {"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"}


def _transcript_body(
    *,
    text: str,
    is_final: bool,
    session_id: str = "insights:session-a",
    task_id: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "session_id": session_id,
        "reason": "transcript_chunk",
        "occurred_at": "2026-01-01T00:00:00Z",
        "payload": {"text": text, "is_final": is_final},
    }


@pytest.mark.asyncio
async def test_a_cue_request_registers_exactly_one_inflight_task_and_dispatches() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        adapter.handle_message_calls.append(event)

    adapter.handle_message = handler

    client = await _client_for(adapter)
    try:
        task_id = str(uuid.uuid4())
        body = _transcript_body(
            text="ask about the renewal",
            is_final=True,
            task_id=task_id,
        )
        response = await client.post("/v1/incoming", headers=_incoming_headers(), json=body)

        assert response.status == 202
        await _wait_until_settled(adapter, task_id)
        assert len(adapter.handle_message_calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_non_final_transcript_chunk_is_accepted_but_dropped_without_dispatching_or_buffering() -> None:
    adapter = _make_adapter()

    async def handler(event: Any) -> None:
        adapter.handle_message_calls.append(event)

    adapter.handle_message = handler

    client = await _client_for(adapter)
    try:
        dropped_response = await client.post(
            "/v1/incoming",
            headers=_incoming_headers(),
            json=_transcript_body(text="still talking", is_final=False),
        )
        assert dropped_response.status == 202
        await asyncio.sleep(0.05)
        assert adapter.inflight == {}
        assert adapter.handle_message_calls == []

        task_id = str(uuid.uuid4())
        cue_response = await client.post(
            "/v1/incoming",
            headers=_incoming_headers(),
            json=_transcript_body(
                text="ask about the timeline", is_final=True, task_id=task_id
            ),
        )
        assert cue_response.status == 202
        await _wait_until_settled(adapter, task_id)

        assert len(adapter.handle_message_calls) == 1
        assert adapter.handle_message_calls[0].text == "ask about the timeline"
    finally:
        await client.close()
