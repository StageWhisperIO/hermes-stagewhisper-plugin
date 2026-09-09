from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any, Callable

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin import listener as listener_module
from hermes_stagewhisper_plugin.listener import _write_sse, build_app
from hermes_stagewhisper_plugin.streams import ReplyStreams


TEST_TOKEN = "test-token-abcdef1234567890"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_adapter() -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(
            extra={
                "token": TEST_TOKEN,
                "listen_port": _free_port(),
                "listen_host": "127.0.0.1",
            }
        )
    )


async def _client(adapter: StageWhisperAdapter) -> TestClient:
    client = TestClient(TestServer(build_app(adapter)))
    await client.start_server()
    return client


async def _read_one_event(
    response: Any,
    timeout: float = 2.0,
    accept: Callable[[str], bool] = lambda _frame: True,
) -> str:
    async def _pump() -> str:
        buffer = b""
        while True:
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                decoded = frame.decode("utf-8")
                if (
                    frame.strip()
                    and not frame.lstrip().startswith(b":")
                    and accept(decoded)
                ):
                    return decoded
            chunk = await response.content.read(64)
            if not chunk:
                return buffer.decode("utf-8")
            buffer += chunk

    return await asyncio.wait_for(_pump(), timeout=timeout)


@pytest.mark.asyncio
async def test_a_stalled_sse_write_is_disconnected_after_the_write_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled_forever = asyncio.Event()

    class StalledResponse:
        async def write(self, chunk: bytes) -> None:
            await stalled_forever.wait()

    monkeypatch.setattr(listener_module, "STREAM_WRITE_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await _write_sse(StalledResponse(), b": keep-alive\n\n")


@pytest.mark.asyncio
async def test_a_stream_request_without_a_bearer_is_refused() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", params={"session_id": "s1"})
        assert response.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_stream_request_with_the_wrong_bearer_is_refused() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get(
            "/v1/events",
            params={"session_id": "s1"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert response.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_stream_request_with_a_disallowed_host_is_refused() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get(
            "/v1/events",
            params={"session_id": "s1"},
            headers={**AUTH, "Host": "attacker.example"},
        )
        assert response.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_stream_request_without_a_session_is_refused() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", headers=AUTH)
        assert response.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_stream_announces_itself_before_any_reply_arrives() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        assert response.status == 200

        opening = await asyncio.wait_for(response.content.read(16), timeout=2.0)

        assert opening.startswith(b":")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_reply_reaches_a_client_holding_the_stream_open() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")

        for _ in range(50):
            if adapter.streams.has_listener("s1"):
                break
            await asyncio.sleep(0.01)

        adapter.streams.capture_durable("s1", {"task_id": "t1", "text": "answer"})
        frame = await _read_one_event(response)

        assert "answer" in frame
        assert not frame.startswith("event: progress")
        id_line, _, data_line = frame.partition("\n")
        assert id_line.startswith("id: ")
        assert data_line.startswith("data: ")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_reply_sent_while_nobody_listened_is_replayed_on_connect() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        adapter.streams.capture_durable(
            "s1", {"task_id": "t1", "text": "missed while away"}
        )

        response = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        frame = await _read_one_event(response)

        assert "missed while away" in frame
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_reconnect_carrying_the_last_event_id_is_not_sent_what_it_already_received() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        adapter.streams.capture_durable("s1", {"task_id": "t1", "text": "already seen"})

        first = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        frame = await _read_one_event(first)
        cursor = frame.partition("\n")[0][len("id: ") :]
        assert "already seen" in frame
        first.close()

        adapter.streams.capture_durable("s1", {"task_id": "t2", "text": "sent after the drop"})

        resumed = await client.get(
            "/v1/events",
            params={"session_id": "s1"},
            headers={**AUTH, "Last-Event-ID": cursor},
        )
        replayed = await _read_one_event(resumed)

        assert "sent after the drop" in replayed
        assert "already seen" not in replayed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_reply_for_another_session_never_appears_on_this_stream() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        for _ in range(50):
            if adapter.streams.has_listener("s1"):
                break
            await asyncio.sleep(0.01)

        adapter.streams.capture_durable("s2", {"task_id": "t1", "text": "not for you"})
        adapter.streams.capture_durable("s1", {"task_id": "t2", "text": "for you"})
        frame = await _read_one_event(response)

        assert "for you" in frame
        assert "not for you" not in frame
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_client_that_supplies_no_callback_still_receives_its_reply() -> None:
    from aiohttp import ClientSession, ClientTimeout

    adapter = _make_adapter()
    adapter._client = ClientSession(timeout=ClientTimeout(total=5.0))

    async def reply_handler(message: Any) -> None:
        await adapter.send(message.source.chat_id, "delivered over the open stream")

    adapter.handle_message = reply_handler
    client = await _client(adapter)
    try:
        response = await client.get(
            "/v1/events", params={"session_id": "stream-only"}, headers=AUTH
        )
        assert response.status == 200
        for _ in range(50):
            if adapter.streams.has_listener("stream-only"):
                break
            await asyncio.sleep(0.01)

        accepted = await client.post(
            "/v1/incoming",
            headers=AUTH,
            json={
                "task_id": str(uuid.uuid4()),
                "session_id": "stream-only",
                "reason": "chat_message",
                "occurred_at": "2026-01-01T00:00:00Z",
                "payload": {"text": "are you there", "user_message_id": "umid-stream-only"},
            },
        )
        assert accepted.status == 202

        frame = await _read_one_event(response, timeout=5.0)
        assert "delivered over the open stream" in frame
    finally:
        await client.close()
        await adapter._client.close()


@pytest.mark.asyncio
async def test_a_subscriber_that_overflows_its_queue_is_disconnected_so_it_can_reconnect_and_recover_the_backlog() -> None:
    adapter = _make_adapter()
    adapter.streams = ReplyStreams(backlog_per_session=1)
    client = await _client(adapter)
    try:
        response = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        for _ in range(50):
            if adapter.streams.has_listener("s1"):
                break
            await asyncio.sleep(0.01)

        adapter.streams.capture_durable("s1", {"task_id": "t1", "text": "first"})
        adapter.streams.capture_durable("s1", {"task_id": "t2", "text": "second"})

        for _ in range(50):
            if not adapter.streams.has_listener("s1"):
                break
            await asyncio.sleep(0.01)
        assert adapter.streams.has_listener("s1") is False

        reconnected = await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        frame = await _read_one_event(reconnected, accept=lambda frame: "second" in frame)
        assert "second" in frame
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_disconnecting_client_is_dropped_from_the_listener_set() -> None:
    adapter = _make_adapter()
    client = await _client(adapter)
    try:
        await client.get("/v1/events", params={"session_id": "s1"}, headers=AUTH)
        for _ in range(50):
            if adapter.streams.has_listener("s1"):
                break
            await asyncio.sleep(0.01)
        assert adapter.streams.has_listener("s1") is True
    finally:
        await client.close()

    for _ in range(50):
        if not adapter.streams.has_listener("s1"):
            break
        await asyncio.sleep(0.01)
    assert adapter.streams.has_listener("s1") is False
