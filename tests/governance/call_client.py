from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

TERMINAL_STATUSES = {"message", "completed", "errored", "silent"}
CALLBACK_TOKEN = "governance-demo-callback-token-" + uuid.uuid4().hex


class CallTimeoutError(Exception):
    pass


@dataclass
class CallbackCollector:
    received: list[dict[str, Any]] = field(default_factory=list)
    _events: dict[str, asyncio.Event] = field(default_factory=dict)
    _runner: web.AppRunner | None = field(default=None, init=False, repr=False)
    _port: int = field(default=0, init=False)

    async def __aenter__(self) -> "CallbackCollector":
        async def handler(request: web.Request) -> web.Response:
            body = await request.json()
            self.received.append(body)
            task_id = request.match_info["task_id"]
            event = self._events.setdefault(task_id, asyncio.Event())
            if body.get("status") in TERMINAL_STATUSES:
                event.set()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/tasks/{task_id}", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def wait_for_terminal(self, task_id: str, timeout: float) -> dict[str, Any]:
        event = self._events.setdefault(task_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise CallTimeoutError(
                f"no terminal callback for task {task_id} within {timeout}s "
                f"(received so far: {self.received})"
            ) from None
        for body in reversed(self.received):
            if body.get("task_id") == task_id and body.get("status") in TERMINAL_STATUSES:
                return body
        raise CallTimeoutError(f"terminal event set but no matching payload for {task_id}")


async def send_chat_message(
    *,
    port: int,
    token: str,
    session_id: str,
    text: str,
    callback_url: str,
    user_message_id: str | None = None,
) -> tuple[int, dict[str, Any], str]:
    task_id = str(uuid.uuid4())
    body = {
        "task_id": task_id,
        "session_id": session_id,
        "reason": "chat_message",
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "payload": {
            "text": text,
            "user_message_id": user_message_id or f"umid-{task_id}",
        },
        "callback": {"url": callback_url, "token": CALLBACK_TOKEN},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/incoming",
            headers={"Host": "127.0.0.1", "Authorization": f"Bearer {token}"},
            json=body,
        ) as response:
            return response.status, await response.json(), task_id
