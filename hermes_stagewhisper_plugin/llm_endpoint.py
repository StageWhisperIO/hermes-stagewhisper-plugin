from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .llm_host import HOST_LLM, HostLlm
from .llm_replay import SSE_DONE, Completion, chat_completion_chunks, chat_completion_json, sse_frame
from .llm_request import LLM_MAX_BODY_BYTES, ChatRequest, LlmRejection, openai_error_body, parse_chat_request

logger = logging.getLogger(__name__)

LLM_PATH = "/v1/chat/completions"
STREAM_HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
NOT_OFFERED = LlmRejection(404, "unknown_url", "This assistant does not offer a Library model endpoint.")
TOO_LARGE = LlmRejection(413, "request_too_large", f"The request body is limited to {LLM_MAX_BODY_BYTES} bytes.")
HOST_FAILURES = {
    401: LlmRejection(502, "upstream_auth_failed", "The host model refused the host's credentials."),
    403: LlmRejection(502, "upstream_auth_failed", "The host model refused the host's credentials."),
    404: NOT_OFFERED,
    429: LlmRejection(429, "rate_limit_exceeded", "The host model is rate limited. Try again shortly."),
}
HOST_ERROR = LlmRejection(502, "upstream_error", "The host model could not answer.")


def error_response(rejection: LlmRejection) -> web.Response:
    return web.json_response(openai_error_body(rejection), status=rejection.status)


async def _settle(host_llm: HostLlm, request: ChatRequest) -> Completion | LlmRejection:
    try:
        return await host_llm.complete(request)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        logger.warning("StageWhisper Library model call failed: %s %s", type(exc).__name__, status or "")
        if status in (401, 403, 404):
            host_llm.refused_by_host = True
        return HOST_FAILURES.get(status, HOST_ERROR)


async def _stream(request: web.Request, host_llm: HostLlm, chat: ChatRequest) -> web.StreamResponse:
    response = web.StreamResponse(headers=SSE_HEADERS)
    await response.prepare(request)
    task = asyncio.ensure_future(_settle(host_llm, chat))
    try:
        while not task.done():
            await response.write(b": keep-alive\n\n")
            await asyncio.wait({task}, timeout=STREAM_HEARTBEAT_SECONDS)
        outcome = task.result()
        if isinstance(outcome, LlmRejection):
            await response.write(sse_frame(openai_error_body(outcome)) + SSE_DONE)
        else:
            await response.write(chat_completion_chunks(outcome))
        await response.write_eof()
    except ConnectionError:
        pass
    finally:
        task.cancel()
    return response


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    host_llm = request.app[HOST_LLM]
    if not host_llm.available:
        return error_response(NOT_OFFERED)
    if (request.content_length or 0) > LLM_MAX_BODY_BYTES:
        return error_response(TOO_LARGE)
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return error_response(TOO_LARGE)
    if len(body) > LLM_MAX_BODY_BYTES:
        return error_response(TOO_LARGE)
    chat = parse_chat_request(body)
    if isinstance(chat, LlmRejection):
        return error_response(chat)
    if chat.stream:
        return await _stream(request, host_llm, chat)
    outcome = await _settle(host_llm, chat)
    if isinstance(outcome, LlmRejection):
        return error_response(outcome)
    return web.json_response(chat_completion_json(outcome))
