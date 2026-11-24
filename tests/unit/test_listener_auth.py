from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.listener import build_app


TEST_TOKEN = "test-token-abcdef1234567890"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_config(port: int | None = None, token: str = TEST_TOKEN) -> Any:
    from gateway.config import PlatformConfig

    return PlatformConfig(
        extra={"token": token, "listen_port": port or _free_port(), "listen_host": "127.0.0.1"}
    )


def _make_adapter(port: int | None = None) -> StageWhisperAdapter:
    return StageWhisperAdapter(_make_config(port=port))


def _valid_body(*, reason: str = "transcript_chunk", task_id: str | None = None, cb_port: int = 9999) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": "hello"}
    if reason == "transcript_chunk":
        payload["is_final"] = True
    elif reason == "chat_message":
        payload["user_message_id"] = "user-msg-1"
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "session_id": "session-1",
        "reason": reason,
        "occurred_at": "2026-05-23T14:32:11.123Z",
        "payload": payload,
        "callback": {"url": f"http://127.0.0.1:{cb_port}", "token": "callback-token-abc-123"},
    }


async def _client_for(adapter: StageWhisperAdapter) -> TestClient:
    app = build_app(adapter)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_ping_missing_bearer_returns_401() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        response = await client.post("/v1/ping", headers={"Host": "127.0.0.1"})
        assert response.status == 401
        data = await response.json()
        assert data["error"] == "bad_bearer"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ping_with_wrong_bearer_returns_401() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        response = await client.post(
            "/v1/ping",
            headers={"Host": "127.0.0.1", "Authorization": "Bearer wrong-token"},
        )
        assert response.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ping_happy_path() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        response = await client.post(
            "/v1/ping",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert response.status == 200
        data = await response.json()
        assert data["ok"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_incoming_bad_host_header_returns_403() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "evil.com", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=_valid_body(),
        )
        assert response.status == 403
        data = await response.json()
        assert data["error"] == "bad_host"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_incoming_invalid_callback_url_rejected() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        body = _valid_body()
        body["callback"]["url"] = "http://example.com:8080"
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 400
        data = await response.json()
        assert data["detail"] == "invalid_callback_url"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_incoming_unknown_reason_rejected() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        body = _valid_body()
        body["reason"] = "garbage"
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 400
        data = await response.json()
        assert data["detail"] == "unknown_reason"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_incoming_short_callback_token_rejected() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        body = _valid_body()
        body["callback"]["token"] = "short"
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 400
        data = await response.json()
        assert data["detail"] == "invalid_callback_token"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_incoming_invalid_task_id_rejected() -> None:
    adapter = _make_adapter()
    client = await _client_for(adapter)
    try:
        body = _valid_body()
        body["task_id"] = "not-a-uuid"
        response = await client.post(
            "/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {TEST_TOKEN}"},
            json=body,
        )
        assert response.status == 400
        data = await response.json()
        assert data["detail"] == "invalid_task_id"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_constructor_rejects_non_loopback_host() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        extra={"token": TEST_TOKEN, "listen_port": 8765, "listen_host": "0.0.0.0"}
    )
    with pytest.raises(ValueError):
        StageWhisperAdapter(cfg)


@pytest.mark.asyncio
async def test_constructor_rejects_missing_token() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"listen_port": 8765})
    with pytest.raises(ValueError):
        StageWhisperAdapter(cfg)


@pytest.mark.asyncio
async def test_constructor_rejects_bad_port() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"token": TEST_TOKEN, "listen_port": 80})
    with pytest.raises(ValueError):
        StageWhisperAdapter(cfg)
