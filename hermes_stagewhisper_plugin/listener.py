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

if TYPE_CHECKING:
    from .adapter import StageWhisperAdapter

logger = logging.getLogger(__name__)


MAX_BODY_BYTES = 256 * 1024
LOOPBACK_PEERS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
ALLOWED_HOST_NAMES = {"127.0.0.1", "localhost"}
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
    return app


def _peer_is_loopback(request: web.Request) -> bool:
    remote = request.remote
    if remote is None:
        return False
    return remote in LOOPBACK_PEERS


def _host_header_ok(request: web.Request) -> bool:
    host_header = request.headers.get("Host", "")
    if not host_header:
        return False
    host_name = host_header.split(":", 1)[0].strip().lower()
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

    if event.reason == REASON_TRANSCRIPT_CHUNK and event.is_final is not True:
        return web.json_response(
            {"status": "accepted", "task_id": event.task_id}, status=202
        )

    adapter.accept_event(event)
    return web.json_response(
        {"status": "accepted", "task_id": event.task_id}, status=202
    )
