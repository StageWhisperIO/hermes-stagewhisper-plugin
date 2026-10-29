from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web

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


async def _open_client(adapter: StageWhisperAdapter) -> None:
    adapter._client = ClientSession(timeout=ClientTimeout(total=5.0))


class RejectingCapture:
    def __init__(self, reject_statuses: set[str]) -> None:
        self.reject_statuses = reject_statuses
        self.received: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            body = await request.json()
            self.received.append(body)
            if body.get("status") in self.reject_statuses:
                return web.json_response({"ok": False}, status=400)
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/tasks/{task_id}", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()
        self.port = list(self._site._server.sockets)[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None


@pytest.mark.asyncio
async def test_a_buffered_reasoning_reply_whose_delivery_permanently_fails_still_delivers_a_terminal_errored_reply_and_releases_the_task() -> None:
    capture = RejectingCapture(reject_statuses={"message"})
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            async def handler(event: Any) -> None:
                await adapter.send(event.source.chat_id, "narrating the call")

            adapter.handle_message = handler

            event = ValidatedEvent(
                task_id="99999999-9999-9999-9999-999999999999",
                session_id="session-permfail",
                reason="transcript_chunk",
                occurred_at="2026-01-01T00:00:00Z",
                text="analyze this",
                is_final=True,
                user_message_id="umid-permfail",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-permfail-aaaaaa",
                raw={},
            )

            adapter.accept_event(event)
            await asyncio.wait_for(adapter.inflight[event.task_id].wait(), timeout=5.0)

            statuses = [body["status"] for body in capture.received]
            assert "message" in statuses
            assert statuses.count("errored") == 1
            errored = next(body for body in capture.received if body["status"] == "errored")
            assert errored["task_id"] == event.task_id
            assert adapter._task_callbacks.get(event.task_id) is None
            assert event.task_id not in adapter.inflight
            assert adapter.idem.get(event.task_id)["status"] == "errored"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()
