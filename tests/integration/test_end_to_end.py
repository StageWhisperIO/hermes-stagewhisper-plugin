from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter


TEST_TOKEN = "test-token-abcdef1234567890"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeDesktop:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.events = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0
        self.token = "desktop-callback-token-abcdef"

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            body = await request.json()
            self.received.append(body)
            task_id = request.match_info["task_id"]
            evt = self.events.setdefault(task_id, asyncio.Event())
            if body.get("status") in {"completed", "errored", "silent", "message"}:
                evt.set()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/tasks/{task_id}", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()
        sockets = list(self._site._server.sockets)
        self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    @property
    def callback_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def terminal_event(self, task_id: str) -> asyncio.Event:
        return self.events.setdefault(task_id, asyncio.Event())


class StubAdapter(StageWhisperAdapter):
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.reply_text = "stub-reply"
        self.received_events: list[Any] = []
        self.delay_before_send = 0.0

    async def handle_message(self, event: Any) -> None:
        self.received_events.append(event)
        if self.delay_before_send:
            await asyncio.sleep(self.delay_before_send)
        await self.send(event.source.chat_id, self.reply_text)


async def _make_adapter(desktop: FakeDesktop) -> StubAdapter:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        extra={
            "token": TEST_TOKEN,
            "listen_port": _free_port(),
            "listen_host": "127.0.0.1",
        }
    )
    adapter = StubAdapter(cfg)
    started = await adapter.connect()
    assert started
    return adapter


async def _post(adapter: StubAdapter, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{adapter._port}/v1/incoming",
            headers={
                "Host": "127.0.0.1",
                "Authorization": f"Bearer {TEST_TOKEN}",
            },
            json=body,
        ) as response:
            data = await response.json()
            return response.status, data


def _chat_body(desktop: FakeDesktop, *, session_id: str = "session-e2e") -> dict[str, Any]:
    return {
        "task_id": str(uuid.uuid4()),
        "session_id": session_id,
        "reason": "chat_message",
        "occurred_at": "2026-05-23T14:32:11.123Z",
        "payload": {"text": "hello agent", "user_message_id": "umid-1"},
        "callback": {"url": desktop.callback_url, "token": desktop.token},
    }


def _transcript_body(desktop: FakeDesktop, *, session_id: str = "session-e2e", is_final: bool) -> dict[str, Any]:
    return {
        "task_id": str(uuid.uuid4()),
        "session_id": session_id,
        "reason": "transcript_chunk",
        "occurred_at": "2026-05-23T14:32:11.123Z",
        "payload": {"text": "this is a transcript", "is_final": is_final},
        "callback": {"url": desktop.callback_url, "token": desktop.token},
    }


def _prelude_body(*, session_id: str = "session-e2e", text: str = "the user is calling about pricing") -> dict[str, Any]:
    return {
        "task_id": str(uuid.uuid4()),
        "session_id": session_id,
        "reason": "system_prelude",
        "occurred_at": "2026-05-23T14:32:11.123Z",
        "payload": {"text": text},
    }


@pytest.mark.asyncio
async def test_chat_message_round_trip() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        try:
            body = _chat_body(desktop)
            status, data = await _post(adapter, body)
            assert status == 202
            assert data["status"] == "accepted"
            await asyncio.wait_for(
                desktop.terminal_event(body["task_id"]).wait(), timeout=5.0
            )
            messages = [r for r in desktop.received if r.get("status") == "message"]
            assert len(messages) == 1
            assert messages[0]["reply_text"] == "stub-reply"
            assert messages[0]["user_message_id"] == "umid-1"
            assert messages[0]["message_id"]
            assert adapter.received_events[0].source.chat_id == "sw:session-e2e:chat"
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()


@pytest.mark.asyncio
async def test_transcript_partial_no_callback() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        try:
            body = _transcript_body(desktop, is_final=False)
            status, data = await _post(adapter, body)
            assert status == 202
            await asyncio.sleep(0.2)
            assert desktop.received == []
            assert adapter.received_events == []
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()


@pytest.mark.asyncio
async def test_transcript_final_routes_to_reasoning_chat_id() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        try:
            body = _transcript_body(desktop, is_final=True)
            status, _data = await _post(adapter, body)
            assert status == 202
            await asyncio.wait_for(
                desktop.terminal_event(body["task_id"]).wait(), timeout=5.0
            )
            assert adapter.received_events[0].source.chat_id == "sw:session-e2e:reasoning"
            assert desktop.received[-1]["status"] == "message"
            assert desktop.received[-1]["reply_text"] == "stub-reply"
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()


@pytest.mark.asyncio
async def test_system_prelude_prepended_once_and_cleared() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        try:
            prelude = _prelude_body(text="the user just signed up for trial")
            status, _data = await _post(adapter, prelude)
            assert status == 202
            assert "session-e2e" in adapter._preludes

            body1 = _chat_body(desktop)
            status, _data = await _post(adapter, body1)
            assert status == 202
            await asyncio.wait_for(
                desktop.terminal_event(body1["task_id"]).wait(), timeout=5.0
            )
            first_text = adapter.received_events[0].text
            assert first_text.startswith("[Context: the user just signed up for trial]\n\n")
            assert "session-e2e" not in adapter._preludes

            body2 = _chat_body(desktop)
            status, _data = await _post(adapter, body2)
            assert status == 202
            await asyncio.wait_for(
                desktop.terminal_event(body2["task_id"]).wait(), timeout=5.0
            )
            second_text = adapter.received_events[1].text
            assert "[Context:" not in second_text
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()


@pytest.mark.asyncio
async def test_duplicate_task_id_inflight_then_completed() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        adapter.delay_before_send = 0.5
        try:
            body = _chat_body(desktop)
            task_id = body["task_id"]

            status1, data1 = await _post(adapter, body)
            assert status1 == 202

            status2, data2 = await _post(adapter, body)
            assert status2 == 202
            assert data2["status"] == "in_flight"

            await asyncio.wait_for(
                desktop.terminal_event(task_id).wait(), timeout=5.0
            )

            status3, data3 = await _post(adapter, body)
            assert status3 == 200
            assert data3["status"] == "duplicate"
            assert data3["result"]["reply_text"] == "stub-reply"
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()


@pytest.mark.asyncio
async def test_interrupt_session_activity_sends_silent() -> None:
    desktop = FakeDesktop()
    await desktop.start()
    try:
        adapter = await _make_adapter(desktop)
        try:
            from hermes_stagewhisper_plugin.callbacks import CallbackHandle

            handle = CallbackHandle(
                task_id="task-int-1",
                session_id="session-int",
                user_message_id="umid-int",
                callback_url=desktop.callback_url,
                callback_token=desktop.token,
                chat_id="sw:session-int:chat",
            )
            from collections import deque
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.interrupt_session_activity(handle.session_id, handle.chat_id)
            assert len(desktop.received) == 1
            assert desktop.received[0]["status"] == "silent"
        finally:
            await adapter.disconnect()
    finally:
        await desktop.stop()
