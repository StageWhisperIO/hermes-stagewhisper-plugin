from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


REASON_TRANSCRIPT_CHUNK = "transcript_chunk"
REASON_CHAT_MESSAGE = "chat_message"
REASON_SYSTEM_PRELUDE = "system_prelude"
ALLOWED_REASONS = {REASON_TRANSCRIPT_CHUNK, REASON_CHAT_MESSAGE, REASON_SYSTEM_PRELUDE}

MAX_TEXT_LENGTH = 16000
MAX_SESSION_ID_LENGTH = 128
MIN_CALLBACK_TOKEN_LENGTH = 16

TASK_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


def _normalize_origin(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    netloc = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{netloc}"


def allowed_callback_origins() -> set[str]:
    raw = os.environ.get("STAGEWHISPER_ALLOW_CALLBACK_URLS")
    if not raw:
        return set()
    origins = (_normalize_origin(entry) for entry in raw.split(",") if entry.strip())
    return {origin for origin in origins if origin is not None}


def allowed_ingress_hosts() -> set[str]:
    raw = os.environ.get("STAGEWHISPER_ALLOW_INGRESS_HOSTS")
    if not raw:
        return set()
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def is_allowed_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.path not in ("", "/"):
        return False
    if parsed.query or parsed.fragment:
        return False
    origin = _normalize_origin(url)
    if origin is None:
        return False
    if origin in allowed_callback_origins():
        return True
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host in ("127.0.0.1", "localhost") and not allowed_ingress_hosts():
        return True
    return False


@dataclass
class ValidatedEvent:
    task_id: str
    session_id: str
    reason: str
    occurred_at: str
    text: str
    is_final: bool | None
    user_message_id: str | None
    parent_message_id: str | None
    callback_url: str | None
    callback_token: str | None
    raw: dict[str, Any]


def validate_incoming(body: Any) -> tuple[ValidatedEvent | None, str | None]:
    if not isinstance(body, dict):
        return None, "body_must_be_object"

    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.match(task_id):
        return None, "invalid_task_id"
    try:
        uuid.UUID(task_id)
    except (ValueError, TypeError):
        return None, "invalid_task_id"

    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > MAX_SESSION_ID_LENGTH:
        return None, "invalid_session_id"

    reason = body.get("reason")
    if reason not in ALLOWED_REASONS:
        return None, "unknown_reason"

    occurred_at = body.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at:
        return None, "invalid_occurred_at"

    payload = body.get("payload")
    if not isinstance(payload, dict):
        return None, "payload_must_be_object"

    text = payload.get("text")
    if not isinstance(text, str):
        return None, "invalid_text"
    if len(text) > MAX_TEXT_LENGTH:
        return None, "text_too_long"

    is_final = payload.get("is_final")
    user_message_id = payload.get("user_message_id")
    parent_message_id = payload.get("parent_message_id")

    if reason == REASON_TRANSCRIPT_CHUNK:
        if "is_final" not in payload:
            return None, "is_final_required"
        if not isinstance(is_final, bool):
            return None, "is_final_must_be_bool"

    if reason == REASON_CHAT_MESSAGE:
        if not isinstance(user_message_id, str) or not user_message_id:
            return None, "user_message_id_required"

    callback_block = body.get("callback")
    callback_url: str | None = None
    callback_token: str | None = None

    if reason != REASON_SYSTEM_PRELUDE:
        if not isinstance(callback_block, dict):
            return None, "callback_required"
        callback_url = callback_block.get("url")
        callback_token = callback_block.get("token")
        if not isinstance(callback_url, str) or not is_allowed_callback_url(callback_url):
            return None, "invalid_callback_url"
        if not isinstance(callback_token, str) or len(callback_token) < MIN_CALLBACK_TOKEN_LENGTH:
            return None, "invalid_callback_token"

    return (
        ValidatedEvent(
            task_id=task_id,
            session_id=session_id,
            reason=reason,
            occurred_at=occurred_at,
            text=text,
            is_final=is_final if isinstance(is_final, bool) else None,
            user_message_id=user_message_id if isinstance(user_message_id, str) else None,
            parent_message_id=parent_message_id if isinstance(parent_message_id, str) else None,
            callback_url=callback_url,
            callback_token=callback_token,
            raw=body,
        ),
        None,
    )


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_completed_payload(
    *,
    task_id: str,
    session_id: str,
    user_message_id: str | None,
    reply_text: str,
    model: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "user_message_id": user_message_id,
        "status": "completed",
        "reply_text": reply_text,
        "error_code": None,
        "error_message": None,
        "model": model,
        "occurred_at": utc_now_iso(),
    }
    return payload


def build_message_payload(
    *,
    task_id: str,
    session_id: str,
    user_message_id: str | None,
    message_id: str,
    reply_text: str,
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "session_id": session_id,
        "message_id": message_id,
        "user_message_id": user_message_id,
        "status": "message",
        "reply_text": reply_text,
        "error_code": None,
        "error_message": None,
        "model": model,
        "occurred_at": utc_now_iso(),
    }


def build_errored_payload(
    *,
    task_id: str,
    session_id: str,
    user_message_id: str | None,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "user_message_id": user_message_id,
        "status": "errored",
        "reply_text": None,
        "error_code": error_code,
        "error_message": error_message,
        "model": None,
        "occurred_at": utc_now_iso(),
    }
    return payload


def build_status_payload(
    *,
    task_id: str,
    session_id: str,
    status: str,
    user_message_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "status": status,
        "occurred_at": utc_now_iso(),
    }
    if user_message_id is not None:
        payload["user_message_id"] = user_message_id
    return payload
