from __future__ import annotations

import asyncio
import socket
from collections import deque
from typing import Any

import pytest
from aiohttp import web

from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter
from hermes_stagewhisper_plugin.callbacks import CallbackHandle


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


class CallbackCapture:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.paths: list[str] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            body = await request.json()
            self.received.append(body)
            self.headers.append(dict(request.headers))
            self.paths.append(request.path)
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


async def _open_client(adapter: StageWhisperAdapter) -> None:
    from aiohttp import ClientSession, ClientTimeout

    adapter._client = ClientSession(timeout=ClientTimeout(total=5.0))


@pytest.mark.asyncio
async def test_send_posts_completed_with_bearer_to_tasks_path() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-xyz-1",
                session_id="session-1",
                user_message_id="umid-1",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-bearer-token-xyz",
                chat_id="sw:session-1:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])

            result = await adapter.send(handle.chat_id, "hello reply")
            assert result.success is True
            assert len(capture.received) == 1
            body = capture.received[0]
            assert body["status"] == "message"
            assert body["message_id"]
            assert body["reply_text"] == "hello reply"
            assert body["task_id"] == "task-xyz-1"
            assert body["session_id"] == "session-1"
            assert body["user_message_id"] == "umid-1"
            assert capture.paths[0] == "/tasks/task-xyz-1"
            assert capture.headers[0]["Authorization"] == "Bearer callback-bearer-token-xyz"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_send_with_no_callback_returns_callback_expired() -> None:
    adapter = _make_adapter()
    await _open_client(adapter)
    try:
        result = await adapter.send("sw:nope:chat", "hi")
        assert result.success is False
        assert result.error == "callback_expired"
    finally:
        await adapter._client.close()


@pytest.mark.asyncio
async def test_send_typing_posts_typing_status() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-typing",
                session_id="session-typing",
                user_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-typing-zzz",
                chat_id="sw:session-typing:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.send_typing(handle.chat_id)
            assert len(capture.received) == 1
            assert capture.received[0]["status"] == "typing"
            assert capture.received[0]["task_id"] == "task-typing"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_interrupt_session_activity_posts_silent() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-silent",
                session_id="session-silent",
                user_message_id="umid-silent",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-silent-yyy",
                chat_id="sw:session-silent:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.interrupt_session_activity(handle.session_id, handle.chat_id)
            assert len(capture.received) == 1
            assert capture.received[0]["status"] == "silent"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_sequential_tasks_emit_ordered_posts() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:session-c:reasoning"
            for index, text in enumerate(["reply one", "reply two", "reply three"]):
                handle = CallbackHandle(
                    task_id=f"task-c-{index}",
                    session_id="session-c",
                    user_message_id=f"umid-c-{index}",
                    callback_url=f"http://127.0.0.1:{capture.port}",
                    callback_token="callback-token-chunk-aaa",
                    chat_id=chat_id,
                )
                adapter._callbacks[chat_id] = deque([handle])
                await adapter.send(chat_id, text)
            replies = [b["reply_text"] for b in capture.received]
            assert replies == ["reply one", "reply two", "reply three"]
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_failed_post_is_not_cached_in_idem() -> None:
    adapter = _make_adapter()
    await _open_client(adapter)
    try:
        handle = CallbackHandle(
            task_id="task-faildeliver",
            session_id="session-fd",
            user_message_id="umid-fd",
            callback_url="http://127.0.0.1:1",
            callback_token="callback-token-faildeliver-zz",
            chat_id="sw:session-fd:chat",
        )
        adapter._callbacks[handle.chat_id] = deque([handle])

        result = await adapter.send(handle.chat_id, "reply that cannot be delivered")

        assert result.success is False
        assert handle.task_id not in adapter.idem
    finally:
        await adapter._client.close()


@pytest.mark.asyncio
async def test_send_appends_multiple_messages_per_task() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-once",
                session_id="session-once",
                user_message_id="umid-once",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-once-aaaaaa",
                chat_id="sw:session-once:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])

            first = await adapter.send(handle.chat_id, "first reply")
            second = await adapter.send(handle.chat_id, "second reply")

            assert first.success is True
            assert second.success is True
            assert first.message_id != second.message_id
            assert len(capture.received) == 2
            assert [b["reply_text"] for b in capture.received] == ["first reply", "second reply"]
            assert all(b["status"] == "message" for b in capture.received)
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_reasoning_path_streams_message() -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            async def per_event_handler(event: Any) -> None:
                await adapter.send(event.source.chat_id, "reasoning reply")

            adapter.handle_message = per_event_handler

            event = ValidatedEvent(
                task_id="task-reason",
                session_id="session-reason",
                reason="transcript_chunk",
                occurred_at="2026-01-01T00:00:00Z",
                text="analyze this",
                is_final=True,
                user_message_id="umid-reason",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-reason-aaaaaa",
                raw={},
            )
            handle = CallbackHandle(
                task_id=event.task_id,
                session_id=event.session_id,
                user_message_id=event.user_message_id,
                callback_url=event.callback_url or "",
                callback_token=event.callback_token or "",
                chat_id="sw:session-reason:reasoning",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            adapter.inflight[handle.task_id] = asyncio.Event()

            await adapter._dispatch(event, handle)

            messages = [b for b in capture.received if b["status"] == "message"]
            assert len(messages) == 1
            assert messages[0]["reply_text"] == "reasoning reply"
            assert adapter.inflight.get(handle.task_id) is None
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_finished_turn_handle_unreachable_via_fallback() -> None:
    from hermes_stagewhisper_plugin.adapter import _dispatch_task_id
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:

            async def handler(event: Any) -> None:
                await adapter.send(event.source.chat_id, "in-turn reply")

            adapter.handle_message = handler

            event = ValidatedEvent(
                task_id="task-stale",
                session_id="session-stale",
                reason="transcript_chunk",
                occurred_at="2026-01-01T00:00:00Z",
                text="hello",
                is_final=True,
                user_message_id="umid-stale",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-stale-aaaaaa",
                raw={},
            )
            handle = CallbackHandle(
                task_id=event.task_id,
                session_id=event.session_id,
                user_message_id=event.user_message_id,
                callback_url=event.callback_url or "",
                callback_token=event.callback_token or "",
                chat_id="sw:session-stale:reasoning",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            adapter.inflight[handle.task_id] = asyncio.Event()

            await adapter._dispatch(event, handle)

            assert not adapter._callbacks.get(handle.chat_id)
            assert adapter._task_callbacks.get(handle.task_id) is handle
            assert handle.terminated is False

            _dispatch_task_id.set(None)
            in_turn = len(capture.received)
            fallback = await adapter.send(handle.chat_id, "stray fallback send")
            assert fallback.success is False
            assert fallback.error == "callback_expired"
            assert len(capture.received) == in_turn

            deferred = await adapter.send(
                handle.chat_id, "deferred result", reply_to=handle.task_id
            )
            assert deferred.success is True
            assert capture.received[-1]["reply_text"] == "deferred result"
            assert capture.received[-1]["task_id"] == handle.task_id
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_dispatch_without_metadata_support_does_not_crash(monkeypatch) -> None:
    from dataclasses import dataclass

    from hermes_stagewhisper_plugin import adapter as adapter_mod
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    @dataclass
    class MetadatalessMessageEvent:
        text: str
        message_type: Any
        source: Any
        message_id: str
        raw_message: dict[str, Any] | None = None

    monkeypatch.setattr(adapter_mod, "_MessageEvent", MetadatalessMessageEvent)
    monkeypatch.setattr(adapter_mod, "_MESSAGE_EVENT_HAS_METADATA", False)

    adapter = _make_adapter()
    await _open_client(adapter)
    try:
        captured: list[Any] = []

        async def handler(event: Any) -> None:
            captured.append(event)
            await adapter.send(event.source.chat_id, "ok")

        adapter.handle_message = handler

        event = ValidatedEvent(
            task_id="task-nometa",
            session_id="session-nometa",
            reason="chat_message",
            occurred_at="2026-01-01T00:00:00Z",
            text="summarize this call",
            is_final=True,
            user_message_id="umid-nometa",
            parent_message_id="pmid-nometa",
            callback_url="http://127.0.0.1:1",
            callback_token="callback-token-nometa-aaaaaa",
            raw={},
        )
        handle = CallbackHandle(
            task_id=event.task_id,
            session_id=event.session_id,
            user_message_id=event.user_message_id,
            callback_url=event.callback_url or "",
            callback_token=event.callback_token or "",
            chat_id="sw:session-nometa:chat",
        )
        adapter._callbacks[handle.chat_id] = deque([handle])
        adapter._task_callbacks[handle.task_id] = handle
        adapter.inflight[handle.task_id] = asyncio.Event()

        await adapter._dispatch(event, handle)

        assert len(captured) == 1
        assert captured[0].text == "summarize this call"
        assert not hasattr(captured[0], "metadata")
    finally:
        await adapter._client.close()


def test_user_id_is_stable_per_token_not_per_session() -> None:
    from gateway.config import PlatformConfig

    a1 = _make_adapter()
    a2 = _make_adapter()
    assert a1._user_id.startswith("sw-user-")
    assert a1._user_id == a2._user_id
    assert TEST_TOKEN not in a1._user_id

    other = StageWhisperAdapter(
        PlatformConfig(extra={"token": "different-token-abcdef1234567890", "listen_port": _free_port()}),
    )
    assert other._user_id != a1._user_id


@pytest.mark.asyncio
async def test_system_bypass_prefix_forwarded_unchanged() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-bypass",
                session_id="session-b",
                user_message_id="umid-b",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-bypass-bbb",
                chat_id="sw:session-b:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.send(handle.chat_id, "⚡ urgent agent reply")
            assert capture.received[0]["reply_text"] == "⚡ urgent agent reply"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_send_rejects_non_loopback_callback_url() -> None:
    adapter = _make_adapter()
    await _open_client(adapter)
    try:
        handle = CallbackHandle(
            task_id="task-evil",
            session_id="session-evil",
            user_message_id=None,
            callback_url="http://evil.example.com:9000",
            callback_token="callback-token-evil-ccc",
            chat_id="sw:session-evil:chat",
        )
        adapter._callbacks[handle.chat_id] = deque([handle])
        result = await adapter.send(handle.chat_id, "should not send")
        assert result.success is False
        assert result.error == "invalid_callback_url"
    finally:
        await adapter._client.close()


@pytest.mark.asyncio
async def test_send_typing_accepts_metadata_kwarg() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-meta",
                session_id="session-meta",
                user_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-meta-mmm",
                chat_id="sw:session-meta:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.send_typing(handle.chat_id, metadata={"foo": "bar"})
            assert len(capture.received) == 1
            assert capture.received[0]["status"] == "typing"
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_stop_typing_is_noop_on_happy_path() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-stop",
                session_id="session-stop",
                user_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-stop-sss",
                chat_id="sw:session-stop:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            await adapter.stop_typing(handle.chat_id)
            assert capture.received == []
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


def test_is_connected_reflects_configured_token(monkeypatch) -> None:
    from gateway.config import PlatformConfig
    from hermes_stagewhisper_plugin.adapter import is_connected as is_connected_fn

    monkeypatch.delenv("STAGEWHISPER_RELAY_TOKEN", raising=False)
    assert is_connected_fn(PlatformConfig(extra={})) is False
    assert is_connected_fn(PlatformConfig(extra={"token": TEST_TOKEN})) is True

    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", TEST_TOKEN)
    assert is_connected_fn(PlatformConfig(extra={})) is True


@pytest.mark.asyncio
async def test_concurrent_same_session_messages_route_to_correct_callbacks() -> None:
    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:shared-session:chat"
            handle_a = CallbackHandle(
                task_id="task-A",
                session_id="shared-session",
                user_message_id="umid-A",
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-A-xxxxxxxxxxxx",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="shared-session",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-yyyyyyyyyyyy",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_a, handle_b])
            adapter._task_callbacks[handle_a.task_id] = handle_a
            adapter._task_callbacks[handle_b.task_id] = handle_b

            result_a = await adapter.send(chat_id, "reply to A", reply_to="task-A")
            result_b = await adapter.send(chat_id, "reply to B", reply_to="task-B")

            assert result_a.success is True
            assert result_b.success is True

            assert len(capture_a.received) == 1
            assert capture_a.received[0]["task_id"] == "task-A"
            assert capture_a.received[0]["user_message_id"] == "umid-A"
            assert capture_a.received[0]["reply_text"] == "reply to A"

            assert len(capture_b.received) == 1
            assert capture_b.received[0]["task_id"] == "task-B"
            assert capture_b.received[0]["user_message_id"] == "umid-B"
            assert capture_b.received[0]["reply_text"] == "reply to B"
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_first_event_timeout_does_not_corrupt_second_event_reply() -> None:
    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:shared-2:chat"
            handle_a = CallbackHandle(
                task_id="task-timeout",
                session_id="shared-2",
                user_message_id="umid-timeout",
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-timeout-aaaa",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-ok",
                session_id="shared-2",
                user_message_id="umid-ok",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-ok-bbbbbbbbbb",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_a, handle_b])

            adapter._drop_handle(handle_a)

            result = await adapter.send(chat_id, "reply to B only")

            assert result.success is True
            assert capture_a.received == []
            assert len(capture_b.received) == 1
            assert capture_b.received[0]["task_id"] == "task-ok"
            assert capture_b.received[0]["reply_text"] == "reply to B only"
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_active_handle_routing_survives_premature_queue_drop() -> None:
    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:overlap:chat"
            handle_a = CallbackHandle(
                task_id="task-A",
                session_id="overlap",
                user_message_id="umid-A",
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-A-overlap-aaaa",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="overlap",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-overlap-bbbb",
                chat_id=chat_id,
            )

            adapter._callbacks[chat_id] = deque([handle_b])
            adapter._active_handles[chat_id] = handle_a

            result = await adapter.send(chat_id, "reply from A's agent run")

            assert result.success is True
            assert len(capture_a.received) == 1
            assert capture_a.received[0]["task_id"] == "task-A"
            assert capture_a.received[0]["reply_text"] == "reply from A's agent run"
            assert capture_b.received == []
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_late_reply_routes_by_reply_to_not_active_handle() -> None:
    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:overlap-replyto:chat"
            handle_a = CallbackHandle(
                task_id="task-A",
                session_id="overlap-rt",
                user_message_id="umid-A",
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-A-replyto-aaaa",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="overlap-rt",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-replyto-bbbb",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_a, handle_b])
            adapter._task_callbacks[handle_a.task_id] = handle_a
            adapter._task_callbacks[handle_b.task_id] = handle_b
            adapter._active_handles[chat_id] = handle_b

            result = await adapter.send(chat_id, "reply from A", reply_to="task-A")

            assert result.success is True
            assert len(capture_a.received) == 1
            assert capture_a.received[0]["task_id"] == "task-A"
            assert capture_a.received[0]["reply_text"] == "reply from A"
            assert capture_b.received == []
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_late_background_send_without_reply_to_routes_via_dispatch_context() -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:bg-context:chat"
            release = asyncio.Event()
            background: dict[str, asyncio.Task] = {}

            async def handle_message_a(msg: Any) -> None:
                async def deferred_send() -> None:
                    await release.wait()
                    await adapter.send(msg.source.chat_id, "reply from A")

                background["task"] = asyncio.create_task(deferred_send())

            adapter.handle_message = handle_message_a

            event_a = ValidatedEvent(
                task_id="task-A",
                session_id="bg-context",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="from A",
                is_final=True,
                user_message_id="umid-A",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-A-bgctx-aaaa",
                raw={},
            )
            handle_a = CallbackHandle(
                task_id=event_a.task_id,
                session_id=event_a.session_id,
                user_message_id=event_a.user_message_id,
                callback_url=event_a.callback_url or "",
                callback_token=event_a.callback_token or "",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="bg-context",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-bgctx-bbbb",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_a, handle_b])
            adapter._task_callbacks[handle_a.task_id] = handle_a
            adapter._task_callbacks[handle_b.task_id] = handle_b
            adapter.inflight[handle_a.task_id] = asyncio.Event()

            await adapter._dispatch(event_a, handle_a)

            adapter._active_handles[chat_id] = handle_b

            release.set()
            await asyncio.wait_for(background["task"], timeout=5.0)

            a_messages = [b for b in capture_a.received if b["status"] == "message"]
            assert len(a_messages) == 1
            assert a_messages[0]["task_id"] == "task-A"
            assert a_messages[0]["reply_text"] == "reply from A"
            assert [b for b in capture_b.received if b["status"] == "message"] == []
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_send_with_expired_reply_to_fails_closed() -> None:
    capture_b = CallbackCapture()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:expired-rt:chat"
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="expired-rt",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-expired-bbbb",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_b])
            adapter._task_callbacks[handle_b.task_id] = handle_b
            adapter._active_handles[chat_id] = handle_b

            result = await adapter.send(chat_id, "reply for expired A", reply_to="task-A")

            assert result.success is False
            assert result.error == "callback_expired"
            assert capture_b.received == []
        finally:
            await adapter._client.close()
    finally:
        await capture_b.stop()


@pytest.mark.asyncio
async def test_stale_dispatch_context_fails_closed_against_newer_active() -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture_a = CallbackCapture()
    capture_b = CallbackCapture()
    await capture_a.start()
    await capture_b.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:stale-ctx:chat"
            release = asyncio.Event()
            result_box: dict[str, Any] = {}

            async def handle_message_a(msg: Any) -> None:
                async def deferred_send() -> None:
                    await release.wait()
                    result_box["result"] = await adapter.send(
                        msg.source.chat_id, "stale reply from A"
                    )

                result_box["task"] = asyncio.create_task(deferred_send())

            adapter.handle_message = handle_message_a

            event_a = ValidatedEvent(
                task_id="task-A",
                session_id="stale-ctx",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="from A",
                is_final=True,
                user_message_id="umid-A",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture_a.port}",
                callback_token="callback-token-A-stalectx-aaaa",
                raw={},
            )
            handle_a = CallbackHandle(
                task_id=event_a.task_id,
                session_id=event_a.session_id,
                user_message_id=event_a.user_message_id,
                callback_url=event_a.callback_url or "",
                callback_token=event_a.callback_token or "",
                chat_id=chat_id,
            )
            handle_b = CallbackHandle(
                task_id="task-B",
                session_id="stale-ctx",
                user_message_id="umid-B",
                callback_url=f"http://127.0.0.1:{capture_b.port}",
                callback_token="callback-token-B-stalectx-bbbb",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle_a])
            adapter._task_callbacks[handle_a.task_id] = handle_a
            adapter.inflight[handle_a.task_id] = asyncio.Event()

            await adapter._dispatch(event_a, handle_a)

            adapter._release_task(handle_a)
            adapter._callbacks[chat_id] = deque([handle_b])
            adapter._task_callbacks[handle_b.task_id] = handle_b
            adapter._active_handles[chat_id] = handle_b

            release.set()
            await asyncio.wait_for(result_box["task"], timeout=5.0)

            assert result_box["result"].success is False
            assert result_box["result"].error == "callback_expired"
            assert [b for b in capture_a.received if b["status"] == "message"] == []
            assert [b for b in capture_b.received if b["status"] == "message"] == []
        finally:
            await adapter._client.close()
    finally:
        await capture_a.stop()
        await capture_b.stop()


@pytest.mark.asyncio
async def test_silent_turn_does_not_error() -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            async def silent_handler(_msg) -> None:
                return None

            adapter.handle_message = silent_handler

            event = ValidatedEvent(
                task_id="task-silentturn",
                session_id="session-st",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="hi",
                is_final=True,
                user_message_id="umid-st",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-silentturn-aa",
                raw={},
            )
            handle = CallbackHandle(
                task_id=event.task_id,
                session_id=event.session_id,
                user_message_id=event.user_message_id,
                callback_url=event.callback_url or "",
                callback_token=event.callback_token or "",
                chat_id="sw:session-st:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            adapter.inflight[handle.task_id] = asyncio.Event()

            await adapter._dispatch(event, handle)

            assert not any(b["status"] == "errored" for b in capture.received)
            assert handle.task_id in adapter.idem
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_long_turn_not_truncated() -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            async def slow_then_reply(msg) -> None:
                await asyncio.sleep(0.2)
                await adapter.send(msg.source.chat_id, "done after a long tool run")

            adapter.handle_message = slow_then_reply

            event = ValidatedEvent(
                task_id="task-longturn",
                session_id="session-lt",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="please run the long tool",
                is_final=True,
                user_message_id="umid-lt",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-longturn-bb",
                raw={},
            )
            handle = CallbackHandle(
                task_id=event.task_id,
                session_id=event.session_id,
                user_message_id=event.user_message_id,
                callback_url=event.callback_url or "",
                callback_token=event.callback_token or "",
                chat_id="sw:session-lt:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            adapter.inflight[handle.task_id] = asyncio.Event()

            await adapter._dispatch(event, handle)

            messages = [b for b in capture.received if b["status"] == "message"]
            assert len(messages) == 1
            assert messages[0]["reply_text"] == "done after a long tool run"
            assert not any(b["status"] == "errored" for b in capture.received)
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_hung_handle_message_times_out_and_frees_session_lane(monkeypatch) -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent
    import hermes_stagewhisper_plugin.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "AGENT_HARD_TIMEOUT_S", 0.1)

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:session-hung:chat"

            async def hung_handler(_msg) -> None:
                await asyncio.sleep(3600)

            adapter.handle_message = hung_handler

            hung_event = ValidatedEvent(
                task_id="task-hung",
                session_id="session-hung",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="hi",
                is_final=True,
                user_message_id="umid-hung",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-hung-aaaaaa",
                raw={},
            )
            hung_handle = CallbackHandle(
                task_id=hung_event.task_id,
                session_id=hung_event.session_id,
                user_message_id=hung_event.user_message_id,
                callback_url=hung_event.callback_url or "",
                callback_token=hung_event.callback_token or "",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([hung_handle])
            adapter._task_callbacks[hung_handle.task_id] = hung_handle
            adapter.inflight[hung_handle.task_id] = asyncio.Event()

            await asyncio.wait_for(
                adapter._dispatch(hung_event, hung_handle), timeout=5.0
            )

            assert adapter._chat_locks[chat_id].locked() is False
            assert hung_handle not in adapter._callbacks.get(chat_id, deque())
            assert adapter.inflight.get(hung_handle.task_id) is None
            errored = [b for b in capture.received if b["status"] == "errored"]
            assert len(errored) == 1
            assert errored[0]["error_code"] == "agent_timeout"

            async def quick_reply(msg) -> None:
                await adapter.send(msg.source.chat_id, "second turn delivered")

            adapter.handle_message = quick_reply

            next_event = ValidatedEvent(
                task_id="task-after-hung",
                session_id="session-hung",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:01Z",
                text="are you there",
                is_final=True,
                user_message_id="umid-after",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-after-bbbbbb",
                raw={},
            )
            next_handle = CallbackHandle(
                task_id=next_event.task_id,
                session_id=next_event.session_id,
                user_message_id=next_event.user_message_id,
                callback_url=next_event.callback_url or "",
                callback_token=next_event.callback_token or "",
                chat_id=chat_id,
            )
            adapter._callbacks.setdefault(chat_id, deque()).append(next_handle)
            adapter._task_callbacks[next_handle.task_id] = next_handle
            adapter.inflight[next_handle.task_id] = asyncio.Event()

            await asyncio.wait_for(
                adapter._dispatch(next_event, next_handle), timeout=5.0
            )

            delivered = [b for b in capture.received if b["status"] == "message"]
            assert any(b["reply_text"] == "second turn delivered" for b in delivered)
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_late_send_after_timeout_is_rejected(monkeypatch) -> None:
    from hermes_stagewhisper_plugin.models import ValidatedEvent
    import hermes_stagewhisper_plugin.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "AGENT_HARD_TIMEOUT_S", 0.1)

    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:session-late:chat"

            async def hung_handler(_msg) -> None:
                await asyncio.sleep(3600)

            adapter.handle_message = hung_handler

            event = ValidatedEvent(
                task_id="task-late",
                session_id="session-late",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="hi",
                is_final=True,
                user_message_id="umid-late",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-late-aaaaaa",
                raw={},
            )
            handle = CallbackHandle(
                task_id=event.task_id,
                session_id=event.session_id,
                user_message_id=event.user_message_id,
                callback_url=event.callback_url or "",
                callback_token=event.callback_token or "",
                chat_id=chat_id,
            )
            adapter._callbacks[chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            adapter.inflight[handle.task_id] = asyncio.Event()

            await asyncio.wait_for(adapter._dispatch(event, handle), timeout=5.0)

            errored = [b for b in capture.received if b["status"] == "errored"]
            assert len(errored) == 1
            assert errored[0]["error_code"] == "agent_timeout"

            assert handle.terminated is True
            assert adapter._task_callbacks.get(handle.task_id) is None

            result = await adapter.send(chat_id, "late reply", reply_to=handle.task_id)
            assert result.success is False
            assert result.error == "callback_expired"

            messages = [b for b in capture.received if b["status"] == "message"]
            assert messages == []
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_task_callbacks_cache_bounded_when_oldest_task_inflight() -> None:
    adapter = _make_adapter()
    adapter._task_callbacks_cap = 3

    def _handle(task_id: str) -> CallbackHandle:
        return CallbackHandle(
            task_id=task_id,
            session_id="s",
            user_message_id=None,
            callback_url="http://127.0.0.1:9999",
            callback_token="callback-token-bounded-aaaa",
            chat_id="sw:s:chat",
        )

    adapter._task_callbacks["task-oldest"] = _handle("task-oldest")
    adapter.inflight["task-oldest"] = asyncio.Event()

    for i in range(20):
        task_id = f"task-{i}"
        adapter._task_callbacks[task_id] = _handle(task_id)
        adapter._evict_finished_task_callbacks()

    assert len(adapter._task_callbacks) <= adapter._task_callbacks_cap
    assert "task-oldest" in adapter._task_callbacks
    assert "task-19" in adapter._task_callbacks


@pytest.mark.asyncio
async def test_disconnect_emits_silent_for_inflight_tasks() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-disc",
                session_id="session-disc",
                user_message_id="umid-disc",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-disc-ggg",
                chat_id="sw:session-disc:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            evt = asyncio.Event()
            evt.set()
            adapter.inflight[handle.task_id] = evt

            await adapter.disconnect()

            assert len(capture.received) >= 1
            assert capture.received[-1]["status"] == "silent"
            assert capture.received[-1]["task_id"] == "task-disc"
            assert adapter._callbacks == {}
            assert adapter._task_callbacks == {}
        finally:
            if adapter._client is not None:
                await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_disconnect_skips_silent_for_delivered_task() -> None:
    capture = CallbackCapture()
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-deliv",
                session_id="session-deliv",
                user_message_id="umid-deliv",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-deliv-hhhh",
                chat_id="sw:session-deliv:chat",
            )
            handle.delivered = True
            adapter._callbacks[handle.chat_id] = deque([handle])
            adapter._task_callbacks[handle.task_id] = handle
            evt = asyncio.Event()
            evt.set()
            adapter.inflight[handle.task_id] = evt

            await adapter.disconnect()

            assert not any(b["status"] == "silent" for b in capture.received)
        finally:
            if adapter._client is not None:
                await adapter._client.close()
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_dispatch_serializes_concurrent_same_chat_id_events() -> None:
    capture_slow = CallbackCapture()
    capture_fast = CallbackCapture()
    await capture_slow.start()
    await capture_fast.start()
    try:
        from hermes_stagewhisper_plugin.models import ValidatedEvent

        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            chat_id = "sw:ooo-session:chat"

            call_count = {"value": 0}

            async def per_event_handler(event: Any) -> None:
                call_count["value"] += 1
                is_first = call_count["value"] == 1
                if is_first:
                    await asyncio.sleep(0.3)
                reply = "slow-reply" if is_first else "fast-reply"
                await adapter.send(event.source.chat_id, reply)

            adapter.handle_message = per_event_handler

            event_slow = ValidatedEvent(
                task_id="task-slow",
                session_id="ooo-session",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="from slow",
                is_final=True,
                user_message_id="umid-slow",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture_slow.port}",
                callback_token="callback-token-slow-aaaaaaaa",
                raw={},
            )
            event_fast = ValidatedEvent(
                task_id="task-fast",
                session_id="ooo-session",
                reason="chat_message",
                occurred_at="2026-01-01T00:00:00Z",
                text="from fast",
                is_final=True,
                user_message_id="umid-fast",
                parent_message_id=None,
                callback_url=f"http://127.0.0.1:{capture_fast.port}",
                callback_token="callback-token-fast-bbbbbbbb",
                raw={},
            )

            adapter.accept_event(event_slow)
            await asyncio.sleep(0.02)
            adapter.accept_event(event_fast)

            await asyncio.wait_for(
                asyncio.gather(
                    adapter.inflight[event_slow.task_id].wait(),
                    adapter.inflight[event_fast.task_id].wait(),
                ),
                timeout=5.0,
            )

            slow_messages = [b for b in capture_slow.received if b["status"] == "message"]
            assert len(slow_messages) == 1
            assert slow_messages[0]["task_id"] == "task-slow"
            assert slow_messages[0]["reply_text"] == "slow-reply"
            assert slow_messages[0]["user_message_id"] == "umid-slow"

            fast_messages = [b for b in capture_fast.received if b["status"] == "message"]
            assert len(fast_messages) == 1
            assert fast_messages[0]["task_id"] == "task-fast"
            assert fast_messages[0]["reply_text"] == "fast-reply"
            assert fast_messages[0]["user_message_id"] == "umid-fast"
        finally:
            await adapter._client.close()
    finally:
        await capture_slow.stop()
        await capture_fast.stop()


class FlakyCapture:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.received: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port = 0

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            self.attempts += 1
            if self.attempts <= self.fail_times:
                return web.json_response({"ok": False}, status=503)
            self.received.append(await request.json())
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
async def test_terminal_callback_retries_until_success() -> None:
    capture = FlakyCapture(fail_times=2)
    await capture.start()
    try:
        adapter = _make_adapter()
        await _open_client(adapter)
        try:
            handle = CallbackHandle(
                task_id="task-retry",
                session_id="session-retry",
                user_message_id="umid-retry",
                callback_url=f"http://127.0.0.1:{capture.port}",
                callback_token="callback-token-retry-rrrrrrrr",
                chat_id="sw:session-retry:chat",
            )
            adapter._callbacks[handle.chat_id] = deque([handle])

            result = await adapter.send(handle.chat_id, "eventual reply")

            assert result.success is True
            assert capture.attempts == 3
            assert len(capture.received) == 1
            assert capture.received[0]["reply_text"] == "eventual reply"
            assert handle.task_id in adapter.idem
        finally:
            await adapter._client.close()
    finally:
        await capture.stop()


def test_get_chat_info_returns_dm_dict() -> None:
    adapter = _make_adapter()
    info = adapter.get_chat_info("sw:abc-123:chat")
    assert info == {"name": "sw:abc-123:chat", "type": "dm"}
