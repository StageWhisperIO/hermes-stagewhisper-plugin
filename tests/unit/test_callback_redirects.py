from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import CallbackHandle
from hermes_stagewhisper_plugin.delivery import CallbackAttemptOutcome


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "reply-stream-contract.json"
REDIRECT_STATUSES = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))[
    "callbackRetryContract"
]["redirectStatuses"]


def _adapter() -> StageWhisperAdapter:
    from gateway.config import PlatformConfig

    return StageWhisperAdapter(
        PlatformConfig(
            extra={
                "token": "test-token-abcdef1234567890",
                "listen_port": 0,
                "listen_host": "127.0.0.1",
            }
        )
    )


@pytest.mark.parametrize("status", REDIRECT_STATUSES, ids=lambda status: str(status))
@pytest.mark.asyncio
async def test_a_redirecting_callback_is_never_followed_off_the_allowed_host(
    status: int,
) -> None:
    sink_hits: list[str] = []

    async def sink(request: web.Request) -> web.Response:
        sink_hits.append(str(request.url))
        return web.json_response({})

    sink_app = web.Application()
    sink_app.router.add_route("*", "/{tail:.*}", sink)
    sink_runner = web.AppRunner(sink_app)
    await sink_runner.setup()
    sink_site = web.TCPSite(sink_runner, "127.0.0.1", 0)
    await sink_site.start()
    sink_port = sink_site._server.sockets[0].getsockname()[1]

    async def redirect_handler(request: web.Request) -> web.Response:
        return web.Response(
            status=status, headers={"Location": f"http://127.0.0.1:{sink_port}/stolen"}
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", redirect_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    adapter = _adapter()
    adapter._client = ClientSession(timeout=ClientTimeout(total=None))
    handle = CallbackHandle(
        task_id="task-a",
        session_id="session-a",
        user_message_id="message-a",
        chat_id="chat-a",
        callback_url=f"http://127.0.0.1:{port}",
        callback_token="callback-token-32-chars-aaaaaaaa",
    )

    try:
        outcome = await adapter._post(handle, {"task_id": "task-a"})
    finally:
        await adapter._client.close()
        await runner.cleanup()
        await sink_runner.cleanup()

    assert outcome is CallbackAttemptOutcome.PERMANENT_FAILURE
    assert sink_hits == []
