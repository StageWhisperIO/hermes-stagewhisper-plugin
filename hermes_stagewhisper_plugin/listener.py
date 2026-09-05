from __future__ import annotations

import asyncio
import hmac
import json
import logging
from importlib import metadata
from typing import TYPE_CHECKING

from aiohttp import web

from .models import (
    REASON_CHAT_MESSAGE,
    REASON_SYSTEM_PRELUDE,
    REASON_TRANSCRIPT_CHUNK,
    allowed_ingress_hosts,
    validate_incoming,
)
from .streams import QueuedReply

if TYPE_CHECKING:
    from .adapter import StageWhisperAdapter

logger = logging.getLogger(__name__)


MAX_BODY_BYTES = 256 * 1024
STREAM_HEARTBEAT_SECONDS = 20.0
STREAM_WRITE_TIMEOUT_SECONDS = 30.0
LOOPBACK_PEERS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
ALLOWED_HOST_NAMES = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}
try:
    PLUGIN_VERSION = metadata.version("hermes-platform-stagewhisper")
except metadata.PackageNotFoundError:
    PLUGIN_VERSION = "0.0.0"


def build_app(adapter: "StageWhisperAdapter") -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app["adapter"] = adapter
    app.router.add_post("/v1/incoming", handle_incoming)
    app.router.add_post("/v1/ping", handle_ping)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/events", handle_events)
    return app


def _peer_is_loopback(request: web.Request) -> bool:
    remote = request.remote
    if remote is None:
        return False
    return remote in LOOPBACK_PEERS


def _split_host_header(host_header: str) -> str:
    trimmed = host_header.strip().lower()
    if trimmed.startswith("["):
        close_idx = trimmed.find("]")
        if close_idx == -1:
            return trimmed
        return trimmed[1:close_idx]
    return trimmed.split(":", 1)[0]


def _host_header_ok(request: web.Request) -> bool:
    host_header = request.headers.get("Host", "")
    if not host_header:
        return False
    host_name = _split_host_header(host_header)
    if host_name in ALLOWED_HOST_NAMES:
        return True
    return host_name in allowed_ingress_hosts()


def _bearer_matches(request: web.Request, token_bytes: bytes) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    provided = header[len("Bearer "):].strip().encode("utf-8")
    return hmac.compare_digest(provided, token_bytes)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": PLUGIN_VERSION})


async def handle_ping(request: web.Request) -> web.Response:
    adapter: "StageWhisperAdapter" = request.app["adapter"]
    if not _peer_is_loopback(request):
        return web.json_response({"error": "loopback_only"}, status=403)
    if not _host_header_ok(request):
        return web.json_response({"error": "bad_host"}, status=403)
    if not _bearer_matches(request, adapter.token_bytes):
        return web.json_response({"error": "bad_bearer"}, status=401)
    return web.json_response({"ok": True, "version": PLUGIN_VERSION})


def _guard(request: web.Request) -> web.Response | None:
    adapter: "StageWhisperAdapter" = request.app["adapter"]
    if not _peer_is_loopback(request):
        return web.json_response({"error": "loopback_only"}, status=403)
    if not _host_header_ok(request):
        return web.json_response({"error": "bad_host"}, status=403)
    if not _bearer_matches(request, adapter.token_bytes):
        return web.json_response({"error": "bad_bearer"}, status=401)
    return None


async def _write_event(response: web.StreamResponse, entry: QueuedReply) -> None:
    await _write_sse(
        response,
        b"id: "
        + entry.event_id.encode("ascii")
        + b"\ndata: "
        + entry.serialized_payload
        + b"\n\n",
    )


async def _write_sse(response: web.StreamResponse, chunk: bytes) -> None:
    await asyncio.wait_for(
        response.write(chunk), timeout=STREAM_WRITE_TIMEOUT_SECONDS
    )


async def _write_transient(response: web.StreamResponse, payload: bytes) -> None:
    await _write_sse(response, b"event: progress\ndata: " + payload + b"\n\n")


async def handle_events(request: web.Request) -> web.StreamResponse:
    denied = _guard(request)
    if denied is not None:
        return denied

    adapter: "StageWhisperAdapter" = request.app["adapter"]
    session_id = (request.query.get("session_id") or "").strip()
    if not session_id:
        return web.json_response({"error": "session_id_required"}, status=400)

    last_event_id = (
        request.headers.get("Last-Event-ID")
        or request.query.get("last_event_id")
        or ""
    ).strip()
    subscriber = adapter.streams.subscribe(session_id, last_event_id or None)
    if subscriber is None:
        return web.json_response({"error": "too_many_streams"}, status=429)

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    try:
        await response.prepare(request)
        await _write_sse(response, b": open\n\n")
        while True:
            subscriber.wakeup.clear()
            drained = adapter.streams.drain(subscriber)
            for entry in drained.entries:
                await _write_event(response, entry)
            for payload in drained.transient_payloads:
                await _write_transient(response, payload)
            if subscriber.dropped:
                break
            try:
                await asyncio.wait_for(
                    subscriber.wakeup.wait(), timeout=STREAM_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                await _write_sse(response, b": keep-alive\n\n")
    except (asyncio.TimeoutError, ConnectionError, RuntimeError):
        pass
    finally:
        adapter.streams.unsubscribe(subscriber)
    return response


async def handle_incoming(request: web.Request) -> web.Response:
    adapter: "StageWhisperAdapter" = request.app["adapter"]

    if not _peer_is_loopback(request):
        return web.json_response({"error": "loopback_only"}, status=403)
    if not _host_header_ok(request):
        return web.json_response({"error": "bad_host"}, status=403)
    if not _bearer_matches(request, adapter.token_bytes):
        return web.json_response({"error": "bad_bearer"}, status=401)

    try:
        raw = await request.read()
    except asyncio.TimeoutError:
        return web.json_response({"error": "read_timeout"}, status=400)

    if len(raw) > MAX_BODY_BYTES:
        return web.json_response({"error": "body_too_large"}, status=413)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "invalid_json"}, status=400)

    event, error = validate_incoming(body)
    if event is None:
        return web.json_response(
            {"error": "validation_failed", "detail": error}, status=400
        )

    if event.reason == REASON_SYSTEM_PRELUDE:
        adapter.stash_prelude(event.session_id, event.text)
        return web.json_response(
            {"status": "accepted", "task_id": event.task_id}, status=202
        )

    if event.reason == REASON_TRANSCRIPT_CHUNK and event.is_final is not True:
        return web.json_response(
            {"status": "accepted", "task_id": event.task_id}, status=202
        )

    cached = adapter.idem.get(event.task_id)
    if cached is not None:
        return web.json_response(
            {
                "status": "duplicate",
                "task_id": event.task_id,
                "result": cached,
            },
            status=200,
        )

    if event.task_id in adapter.inflight:
        return web.json_response(
            {"status": "in_flight", "task_id": event.task_id}, status=202
        )

    adapter.accept_event(event)
    return web.json_response(
        {"status": "accepted", "task_id": event.task_id}, status=202
    )
