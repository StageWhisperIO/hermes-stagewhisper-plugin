from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import IdempotencyCache
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


def _valid_body(task_id: str | None = None) -> dict[str, Any]:
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "session_id": "session-1",
        "reason": "chat_message",
        "occurred_at": "2026-05-23T14:32:11.123Z",
        "payload": {"text": "hi", "user_message_id": "umid-1"},
        "callback": {"url": "http://127.0.0.1:9999", "token": "callback-token-1234-abcdef"},
    }


async def _client_for(adapter: StageWhisperAdapter) -> TestClient:
    server = TestServer(build_app(adapter))
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_duplicate_inflight_returns_202_in_flight() -> None:
    adapter = _make_adapter()
    task_id = str(uuid.uuid4())
    adapter.inflight[task_id] = asyncio.Event()

    client = await _client_for(adapter)
    try:
        body = _valid_body(task_id=task_id)
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 202
        data = await response.json()
        assert data["status"] == "in_flight"
        assert data["task_id"] == task_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_duplicate_cached_returns_200_duplicate_with_payload() -> None:
    adapter = _make_adapter()
    task_id = str(uuid.uuid4())
    cached_payload = {
        "task_id": task_id,
        "session_id": "session-1",
        "status": "completed",
        "reply_text": "cached reply",
        "occurred_at": "2026-05-23T14:32:12.000Z",
    }
    adapter.idem.put(task_id, cached_payload)

    client = await _client_for(adapter)
    try:
        body = _valid_body(task_id=task_id)
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 200
        data = await response.json()
        assert data["status"] == "duplicate"
        assert data["result"]["reply_text"] == "cached reply"
    finally:
        await client.close()


def test_lru_evicts_oldest_when_over_capacity() -> None:
    cache = IdempotencyCache(capacity=3)
    cache.put("a", {"v": 1})
    cache.put("b", {"v": 2})
    cache.put("c", {"v": 3})
    cache.put("d", {"v": 4})
    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache
    assert "d" in cache
    assert len(cache) == 3


def test_lru_get_promotes_entry() -> None:
    cache = IdempotencyCache(capacity=2)
    cache.put("a", {"v": 1})
    cache.put("b", {"v": 2})
    assert cache.get("a") == {"v": 1}
    cache.put("c", {"v": 3})
    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache


def test_completed_payload_relays_reply_verbatim_without_pairing_fields():
    from hermes_stagewhisper_plugin.models import build_completed_payload

    deny_like = "Hi~ I don't recognize you yet! hermes pairing approve stagewhisper DWKZCLEW"
    p = build_completed_payload(task_id="t", session_id="s", user_message_id="u", reply_text=deny_like)
    assert p["status"] == "completed"
    assert p["reply_text"] == deny_like
    assert "pairing_required" not in p
    assert "pairing_code" not in p
    assert "approve_command" not in p
