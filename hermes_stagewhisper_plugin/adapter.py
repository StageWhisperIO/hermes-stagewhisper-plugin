from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import logging
import os
import re
import time
import uuid
from collections import deque
from typing import Any

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web

from .callbacks import CallbackHandle, ClosedTurns, IdempotencyCache
from .delivery import (
    CallbackAttemptOutcome,
    classify_callback_status,
    deliver_progress, deliver_reply, retry_callback,
)
from .draft_stream import StreamChunkTracker
from .reasoning_buffer import ReasoningReplyBuffer
from .streams import ReplyStreams
from .listener import build_app
from .models import (
    REASON_CHAT_MESSAGE,
    REASON_TRANSCRIPT_CHUNK,
    ValidatedEvent,
    build_errored_payload,
    build_message_payload,
    build_notice_payload,
    build_status_payload,
    build_stream_payload,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_DEDUP_CACHE_SIZE = 2048
AGENT_HARD_TIMEOUT_S = 300.0
TURN_KEEPALIVE_S = 15.0
TURN_QUIET_PERIOD_S = 2.0
TURN_IDLE_GRACE_S = 15.0
TURN_SETTLE_DEADLINE_S = 290.0
MAX_TERMINATED_FLUSH_ENTRIES = 256
MAX_NOTICE_MESSAGE_IDS = 256
INTERIM_SEND_METADATA_KEYS = ("notice", "_interim_send")


def _is_interim_send(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    return any(bool(metadata.get(key)) for key in INTERIM_SEND_METADATA_KEYS)


def _coerce_int(value: Any, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _resolve_token(extra: dict[str, Any]) -> str:
    return (
        extra.get("token")
        or os.environ.get("STAGEWHISPER_RELAY_TOKEN", "")
    ).strip()


def _resolve_host(extra: dict[str, Any]) -> str:
    return (
        extra.get("listen_host")
        or os.environ.get("STAGEWHISPER_LISTEN_HOST")
        or DEFAULT_HOST
    )


def _resolve_port(extra: dict[str, Any]) -> int:
    return _coerce_int(
        extra.get("listen_port") or os.environ.get("STAGEWHISPER_LISTEN_PORT"),
        DEFAULT_PORT,
    )


def _resolve_max_concurrent(extra: dict[str, Any]) -> int:
    return _coerce_int(
        extra.get("max_concurrent") or os.environ.get("STAGEWHISPER_MAX_CONCURRENT"),
        DEFAULT_MAX_CONCURRENT,
    )


def _resolve_dedup_size(extra: dict[str, Any]) -> int:
    return _coerce_int(
        extra.get("dedup_cache_size") or os.environ.get("STAGEWHISPER_DEDUP_CACHE_SIZE"),
        DEFAULT_DEDUP_CACHE_SIZE,
    )


def _load_base():
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    return BasePlatformAdapter, MessageEvent, MessageType, SendResult, Platform, PlatformConfig


try:
    (
        _BasePlatformAdapter,
        _MessageEvent,
        _MessageType,
        _SendResult,
        _Platform,
        _PlatformConfig,
    ) = _load_base()
    _GATEWAY_AVAILABLE = True
except Exception:
    _BasePlatformAdapter = object
    _MessageEvent = None
    _MessageType = None
    _SendResult = None
    _Platform = None
    _PlatformConfig = None
    _GATEWAY_AVAILABLE = False


def _message_event_accepts_metadata() -> bool:
    if _MessageEvent is None:
        return False
    try:
        return "metadata" in inspect.signature(_MessageEvent.__init__).parameters
    except (ValueError, TypeError):
        return False


_MESSAGE_EVENT_HAS_METADATA = _message_event_accepts_metadata()


_live_adapter: "StageWhisperAdapter | None" = None

_dispatch_task_id: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "stagewhisper_dispatch_task_id", default=None
)


class StageWhisperAdapter(_BasePlatformAdapter):
    PLATFORM_NAME = "stagewhisper"
    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config: Any) -> None:
        if _GATEWAY_AVAILABLE:
            super().__init__(config=config, platform=_Platform(self.PLATFORM_NAME))
        else:
            self.config = config

        global _live_adapter
        _live_adapter = self

        extra = getattr(config, "extra", None) or {}
        token = _resolve_token(extra)
        if not token:
            raise ValueError(
                "STAGEWHISPER_RELAY_TOKEN is required for the StageWhisper adapter"
            )

        self._token = token
        self.token_bytes = token.encode("utf-8")
        self._user_id = "sw-user-" + hashlib.sha256(self.token_bytes).hexdigest()[:32]
        self._host = _resolve_host(extra)
        if self._host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"listen host must be loopback; got {self._host!r}")
        self._port = _resolve_port(extra)
        if not (1024 <= self._port <= 65535):
            raise ValueError(f"listen port {self._port} out of range")

        max_concurrent = _resolve_max_concurrent(extra)
        dedup_size = _resolve_dedup_size(extra)

        self._sem = asyncio.Semaphore(max_concurrent)
        self.idem = IdempotencyCache(capacity=dedup_size)
        self.streams = ReplyStreams()
        self._callbacks: dict[str, deque[CallbackHandle]] = {}
        self._task_callbacks: dict[str, CallbackHandle] = {}
        self._closed_turns = ClosedTurns(capacity=dedup_size)
        self._task_callbacks_cap = dedup_size
        self.inflight: dict[str, asyncio.Event] = {}
        self._preludes: dict[str, str] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._active_handles: dict[str, CallbackHandle] = {}
        self._reasoning_pending = ReasoningReplyBuffer()
        self._stream_chunks = StreamChunkTracker()
        self._terminated_stream_flush: dict[str, str] = {}
        self._notice_message_ids: dict[str, None] = {}

        self._client: ClientSession | None = None
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._running = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if is_reconnect or self._runner is not None:
            await self.disconnect()
        try:
            app = build_app(self)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=self._host, port=self._port)
            await site.start()
            self._app = app
            self._runner = runner
            self._site = site

            self._client = ClientSession(timeout=ClientTimeout(total=None))
            self._running = True
            if hasattr(self, "_mark_connected"):
                self._mark_connected()
            logger.info(
                "StageWhisper adapter listening on http://%s:%d",
                self._host,
                self._port,
            )
            return True
        except Exception as exc:
            logger.exception("StageWhisper adapter failed to start: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._running = False

        if self.inflight:
            pending_task_ids = list(self.inflight.keys())
            pending = [evt.wait() for evt in self.inflight.values()]
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            for task_id in pending_task_ids:
                handle = self._task_callbacks.get(task_id)
                if handle is None or handle.terminated or handle.delivered:
                    continue
                payload = build_status_payload(
                    task_id=handle.task_id,
                    session_id=handle.session_id,
                    status="silent",
                    user_message_id=handle.user_message_id,
                )
                await self._terminate(handle, payload)

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._site = None
        self._runner = None
        self._app = None

        if self._client is not None:
            await self._client.close()
            self._client = None

        self._callbacks.clear()
        self._task_callbacks.clear()
        self.inflight.clear()
        self._preludes.clear()
        self._chat_locks.clear()
        self._active_handles.clear()
        self._reasoning_pending.clear()
        self._stream_chunks.clear()
        self._terminated_stream_flush.clear()
        self._notice_message_ids.clear()

        global _live_adapter
        if _live_adapter is self:
            _live_adapter = None

        if hasattr(self, "_mark_disconnected"):
            self._mark_disconnected()
        logger.info("StageWhisper adapter disconnected")

    def stash_prelude(self, session_id: str, text: str) -> None:
        self._preludes[session_id] = text

    def _consume_prelude(self, session_id: str) -> str | None:
        return self._preludes.pop(session_id, None)

    def accept_event(self, event: ValidatedEvent) -> None:
        chat_id = self._chat_id_for(event)
        handle = CallbackHandle(
            task_id=event.task_id,
            session_id=event.session_id,
            user_message_id=event.user_message_id,
            callback_url=event.callback_url or "",
            callback_token=event.callback_token or "",
            chat_id=chat_id,
            is_reasoning=event.reason == REASON_TRANSCRIPT_CHUNK,
        )
        self._callbacks.setdefault(chat_id, deque()).append(handle)
        self._task_callbacks[event.task_id] = handle
        self._evict_finished_task_callbacks()
        self.inflight[event.task_id] = asyncio.Event()
        asyncio.create_task(self._dispatch(event, handle))

    def _evict_finished_task_callbacks(self) -> None:
        for task_id in list(self._task_callbacks):
            if len(self._task_callbacks) <= self._task_callbacks_cap:
                break
            if task_id in self.inflight:
                continue
            self._task_callbacks.pop(task_id, None)
            self._reasoning_pending.forget(task_id)

    def _chat_id_for(self, event: ValidatedEvent) -> str:
        if event.reason == REASON_TRANSCRIPT_CHUNK:
            return f"sw:{event.session_id}:reasoning"
        return f"sw:{event.session_id}:chat"

    def _peek_callback(self, chat_id: str) -> "CallbackHandle | None":
        queue = self._callbacks.get(chat_id)
        if not queue:
            return None
        return queue[0]

    def _active_callback(self, chat_id: str) -> "CallbackHandle | None":
        return self._active_handles.get(chat_id)

    def _drop_handle(self, handle: "CallbackHandle") -> None:
        queue = self._callbacks.get(handle.chat_id)
        if queue is None:
            return
        try:
            queue.remove(handle)
        except ValueError:
            pass
        if not queue:
            self._callbacks.pop(handle.chat_id, None)

    def _release_task(self, handle: "CallbackHandle") -> None:
        self._drop_handle(handle)
        self._task_callbacks.pop(handle.task_id, None)
        self._reasoning_pending.forget(handle.task_id)
        if handle.terminated:
            self._closed_turns.remember(handle)

    def _signal_done(self, task_id: str) -> None:
        evt = self.inflight.pop(task_id, None)
        if evt is not None:
            evt.set()

    async def _deliver_reply(
        self, handle: "CallbackHandle", payload: dict[str, Any]
    ) -> bool:
        return await deliver_reply(handle, payload, self.streams, self._post)

    async def _flush_reasoning_reply(self, handle: "CallbackHandle") -> None:
        result = await self._reasoning_pending.flush(handle, self._deliver_reply)
        if result.payload is not None:
            handle.delivered = True
            self.idem.put(handle.task_id, result.payload)
            return
        if result.delivery_failed:
            payload = build_errored_payload(
                task_id=handle.task_id,
                session_id=handle.session_id,
                user_message_id=handle.user_message_id,
                error_code="callback_failed",
                error_message="buffered reasoning reply could not be delivered",
            )
            await self._terminate(handle, payload)

    async def _keepalive(self, handle: "CallbackHandle") -> None:
        while not handle.terminated:
            await asyncio.sleep(TURN_KEEPALIVE_S)
            if handle.terminated:
                return
            await self._emit_typing(handle)

    async def _settle_turn(self, handle: "CallbackHandle") -> None:
        deadline = time.monotonic() + TURN_SETTLE_DEADLINE_S
        while not handle.terminated and time.monotonic() < deadline:
            if handle.last_send_at:
                quiet_since = max(handle.last_send_at, handle.last_activity_at)
                quiet_for = time.monotonic() - quiet_since
                if quiet_for >= TURN_QUIET_PERIOD_S:
                    break
                await asyncio.sleep(TURN_QUIET_PERIOD_S - quiet_for)
                continue
            if time.monotonic() - handle.last_activity_at >= TURN_IDLE_GRACE_S:
                break
            await asyncio.sleep(0.25)
        if handle.terminated:
            return
        status = "completed" if handle.delivered else "silent"
        payload = build_status_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            status=status,
            user_message_id=handle.user_message_id,
        )
        await self._terminate(handle, payload)

    async def _terminate(self, handle: "CallbackHandle", payload: dict[str, Any]) -> bool:
        if handle.terminated:
            return False
        handle.terminated = True
        self._release_task(handle)
        try:
            ok = await self._deliver_reply(handle, payload)
        finally:
            self._signal_done(handle.task_id)
        if ok:
            self.idem.put(handle.task_id, payload)
        return ok

    async def _dispatch(self, event: ValidatedEvent, handle: CallbackHandle) -> None:
        _dispatch_task_id.set(event.task_id)
        chat_lock = self._chat_locks.setdefault(handle.chat_id, asyncio.Lock())
        async with chat_lock, self._sem:
            self._active_handles[handle.chat_id] = handle
            try:
                text = event.text
                prelude = self._consume_prelude(event.session_id)
                if prelude:
                    text = f"[Context: {prelude}]\n\n{text}"

                source = self._build_source_event(handle)
                if source is None:
                    logger.warning(
                        "StageWhisper gateway base classes unavailable; cannot dispatch message"
                    )
                    return

                event_kwargs: dict[str, Any] = dict(
                    text=text,
                    message_type=_MessageType.TEXT,
                    source=source,
                    message_id=event.task_id,
                    raw_message=event.raw,
                )
                if _MESSAGE_EVENT_HAS_METADATA:
                    metadata: dict[str, Any] = {}
                    if event.user_message_id:
                        metadata["user_message_id"] = event.user_message_id
                    if event.parent_message_id:
                        metadata["parent_message_id"] = event.parent_message_id
                    event_kwargs["metadata"] = metadata or None

                message_event = _MessageEvent(**event_kwargs)

                handle.last_activity_at = time.monotonic()
                await self._emit_typing(handle)
                keepalive = asyncio.create_task(self._keepalive(handle))
                try:
                    await asyncio.wait_for(
                        self.handle_message(message_event),
                        timeout=AGENT_HARD_TIMEOUT_S,
                    )
                    if handle.is_reasoning:
                        await self._flush_reasoning_reply(handle)
                    else:
                        await self._settle_turn(handle)
                finally:
                    keepalive.cancel()
            except asyncio.TimeoutError:
                logger.warning(
                    "StageWhisper agent hard timeout (%.0fs) for task %s",
                    AGENT_HARD_TIMEOUT_S,
                    event.task_id,
                )
                if not handle.terminated:
                    handle.terminated = True
                    self._release_task(handle)
                    payload = build_errored_payload(
                        task_id=handle.task_id,
                        session_id=handle.session_id,
                        user_message_id=handle.user_message_id,
                        error_code="agent_timeout",
                        error_message=f"agent did not reply within {AGENT_HARD_TIMEOUT_S:.0f}s",
                    )
                    if await self._deliver_reply(handle, payload):
                        self.idem.put(handle.task_id, payload)
            except Exception as exc:
                logger.exception(
                    "StageWhisper dispatch failed for task %s: %s",
                    event.task_id,
                    exc,
                )
                if not handle.terminated:
                    handle.terminated = True
                    self._release_task(handle)
                    payload = build_errored_payload(
                        task_id=handle.task_id,
                        session_id=handle.session_id,
                        user_message_id=handle.user_message_id,
                        error_code="agent_error",
                        error_message=str(exc),
                    )
                    if await self._deliver_reply(handle, payload):
                        self.idem.put(handle.task_id, payload)
            finally:
                self._reasoning_pending.discard(handle.task_id)
                self._stream_chunks.discard(handle.task_id)
                if self._active_handles.get(handle.chat_id) is handle:
                    del self._active_handles[handle.chat_id]
                if not handle.terminated and handle.task_id not in self.idem:
                    self.idem.put(
                        handle.task_id,
                        build_status_payload(
                            task_id=handle.task_id,
                            session_id=handle.session_id,
                            status="silent",
                            user_message_id=handle.user_message_id,
                        ),
                    )
                self._drop_handle(handle)
                self._signal_done(handle.task_id)

    def _build_source_event(self, handle: CallbackHandle):
        if not _GATEWAY_AVAILABLE or not hasattr(self, "build_source"):
            return None
        return self.build_source(
            chat_id=handle.chat_id,
            chat_name=f"Call {handle.session_id[:8]}",
            chat_type="dm",
            user_id=self._user_id,
            user_name="You",
        )

    async def _emit_typing(self, handle: CallbackHandle) -> None:
        if handle.uses_callback and not handle.callback_is_usable:
            return
        payload = build_status_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            status="typing",
            user_message_id=handle.user_message_id,
        )
        await deliver_progress(handle, payload, self.streams, self._post)

    def _resolve_send_handle(
        self, chat_id: str, reply_to: str | None
    ) -> CallbackHandle | None:
        if reply_to:
            return self._task_callbacks.get(reply_to) or self._closed_turns.by_task(
                reply_to
            )
        dispatch_task_id = _dispatch_task_id.get()
        if dispatch_task_id:
            return self._task_callbacks.get(
                dispatch_task_id
            ) or self._closed_turns.by_task(dispatch_task_id)
        return (
            self._active_callback(chat_id)
            or self._peek_callback(chat_id)
            or self._closed_turns.by_chat(chat_id)
        )

    async def _send_as_new_message(self, handle: CallbackHandle, content: str) -> Any:
        message_id = str(uuid.uuid4())
        payload = build_message_payload(
            task_id=str(uuid.uuid4()),
            session_id=handle.session_id,
            user_message_id=handle.user_message_id,
            message_id=message_id,
            reply_text=content,
        )
        ok = await self._deliver_reply(handle, payload)
        return self._send_result(
            ok,
            message_id=message_id,
            error=None if ok else "callback_failed",
        )

    def _remember_notice(self, message_id: str) -> None:
        if len(self._notice_message_ids) >= MAX_NOTICE_MESSAGE_IDS:
            self._notice_message_ids.pop(next(iter(self._notice_message_ids)))
        self._notice_message_ids[message_id] = None

    async def _send_notice(self, handle: CallbackHandle, content: str) -> Any:
        if handle.terminated:
            return self._send_result(False, error="turn_terminated")
        handle.last_activity_at = time.monotonic()
        message_id = str(uuid.uuid4())
        payload = build_notice_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            user_message_id=handle.user_message_id,
            message_id=message_id,
            reply_text=content,
        )
        ok = await self._deliver_reply(handle, payload)
        if ok:
            self._remember_notice(message_id)
        return self._send_result(
            ok,
            message_id=message_id,
            error=None if ok else "callback_failed",
        )

    async def _edit_notice(self, chat_id: str, message_id: str, content: str) -> Any:
        handle = self._resolve_send_handle(chat_id, None)
        if handle is None:
            return self._send_result(
                False, message_id=message_id, error="callback_expired"
            )
        return await self._send_notice(handle, content)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        handle = self._resolve_send_handle(chat_id, reply_to)
        if handle is None:
            return self._send_result(False, error="callback_expired")

        if handle.uses_callback and not handle.callback_is_usable:
            self._drop_handle(handle)
            return self._send_result(False, error="invalid_callback_url")

        if _is_interim_send(metadata):
            return await self._send_notice(handle, content)

        if handle.terminated:
            return await self._send_as_new_message(handle, content)

        if handle.is_reasoning and not self._reasoning_pending.has_flushed(handle.task_id):
            message_id = str(uuid.uuid4())
            self._reasoning_pending.capture(handle.task_id, content, message_id)
            return self._send_result(True, message_id=message_id)

        message_id = str(uuid.uuid4())
        payload = build_message_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            user_message_id=handle.user_message_id,
            message_id=message_id,
            reply_text=content,
        )
        ok = await self._deliver_reply(handle, payload)
        if ok:
            handle.delivered = True
            handle.last_send_at = time.monotonic()
            self.idem.put(handle.task_id, payload)
        return self._send_result(
            ok,
            message_id=message_id,
            error=None if ok else "callback_failed",
        )

    async def _emit_stream_chunks(
        self, handle: CallbackHandle, content: str, finalize: bool
    ) -> bool:
        chunks = self._stream_chunks.emit(handle.task_id, content, finalize=finalize)
        if not chunks:
            return True
        handle.last_activity_at = time.monotonic()
        delivered = True
        for chunk in chunks:
            payload = build_stream_payload(
                task_id=handle.task_id,
                session_id=handle.session_id,
                user_message_id=handle.user_message_id,
                chunk=chunk,
            )
            if not await self._deliver_reply(handle, payload):
                delivered = False
        return delivered

    async def _stream_request(
        self, chat_id: str, content: str, finalize: bool
    ) -> tuple[bool, str | None]:
        handle = self._resolve_send_handle(chat_id, None)
        if handle is None:
            return False, "callback_expired"
        if handle.uses_callback and not handle.callback_is_usable:
            self._drop_handle(handle)
            return False, "invalid_callback_url"
        if handle.terminated:
            return await self._flush_terminated_stream(handle, content)
        ok = await self._emit_stream_chunks(handle, content, finalize)
        return ok, None if ok else "callback_failed"

    async def _flush_terminated_stream(
        self, handle: CallbackHandle, content: str
    ) -> tuple[bool, str | None]:
        self._stream_chunks.discard(handle.task_id)
        if not content:
            return True, None
        if self._terminated_stream_flush.get(handle.task_id) == content:
            return True, None
        result = await self._send_as_new_message(handle, content)
        delivered = (
            bool(result.get("success"))
            if isinstance(result, dict)
            else bool(getattr(result, "success", False))
        )
        if delivered:
            if len(self._terminated_stream_flush) >= MAX_TERMINATED_FLUSH_ENTRIES:
                self._terminated_stream_flush.pop(
                    next(iter(self._terminated_stream_flush))
                )
            self._terminated_stream_flush[handle.task_id] = content
        return delivered, None if delivered else "callback_failed"

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> Any:
        if message_id in self._notice_message_ids:
            return await self._edit_notice(chat_id, message_id, content)
        ok, error = await self._stream_request(chat_id, content, finalize)
        return self._send_result(ok, message_id=message_id, error=error)

    def supports_draft_streaming(
        self,
        chat_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> bool:
        return True

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        ok, error = await self._stream_request(chat_id, content, finalize=False)
        return self._send_result(ok, message_id=str(draft_id), error=error)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        handle = self._active_callback(chat_id) or self._peek_callback(chat_id)
        if handle is None:
            return
        handle.last_activity_at = time.monotonic()
        if handle.uses_callback and not handle.callback_is_usable:
            return
        payload = build_status_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            status="typing",
            user_message_id=handle.user_message_id,
        )
        await deliver_progress(handle, payload, self.streams, self._post)

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def interrupt_session_activity(
        self, session_key: str, chat_id: str
    ) -> None:
        handle = self._peek_callback(chat_id)
        if handle is None:
            return
        payload = build_status_payload(
            task_id=handle.task_id,
            session_id=handle.session_id,
            status="silent",
            user_message_id=handle.user_message_id,
        )
        await self._terminate(handle, payload)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    async def _post(
        self, handle: CallbackHandle, payload: dict[str, Any]
    ) -> CallbackAttemptOutcome:
        if self._client is None:
            return CallbackAttemptOutcome.PERMANENT_FAILURE
        if not handle.callback_is_usable:
            logger.warning(
                "StageWhisper refusing to POST to non-loopback callback: %s",
                handle.callback_url,
            )
            return CallbackAttemptOutcome.PERMANENT_FAILURE
        url = f"{handle.callback_url.rstrip('/')}/tasks/{payload['task_id']}"
        headers = {
            "Authorization": f"Bearer {handle.callback_token}",
            "Content-Type": "application/json",
        }
        try:
            async with self._client.post(
                url, headers=headers, json=payload, allow_redirects=False
            ) as response:
                outcome = classify_callback_status(response.status)
                if outcome is not CallbackAttemptOutcome.DELIVERED:
                    body_text = await response.text()
                    logger.warning(
                        "StageWhisper callback %s returned HTTP %d: %s",
                        url,
                        response.status,
                        body_text[:200],
                    )
                return outcome
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("StageWhisper callback POST to %s failed: %s", url, exc)
            return CallbackAttemptOutcome.RETRYABLE_FAILURE

    def _send_result(self, success: bool, *, message_id: str | None = None, error: str | None = None):
        if _SendResult is not None:
            return _SendResult(success=success, message_id=message_id, error=error)
        return {"success": success, "message_id": message_id, "error": error}


def is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", None) or {}
    token = (extra.get("token") or os.environ.get("STAGEWHISPER_RELAY_TOKEN") or "").strip()
    return bool(token)
