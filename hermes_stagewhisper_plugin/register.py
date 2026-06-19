from __future__ import annotations

import logging
import os
from typing import Any

from . import config
from .adapter import (
    DEFAULT_CALLBACK_TIMEOUT_S,
    DEFAULT_DEDUP_CACHE_SIZE,
    DEFAULT_HOST,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PORT,
    StageWhisperAdapter,
    is_connected,
)

logger = logging.getLogger(__name__)


PLATFORM_HINT = (
    "You are talking to the user during a live call via StageWhisper. "
    "Replies tagged with chat_id ending ':reasoning' are background "
    "coaching signals triggered by transcribed speech, keep each to one "
    "short line, 12 words max, a direction or angle to try (never the "
    "exact words to read aloud), and only respond when you have something "
    "genuinely useful to say. Replies tagged with chat_id ending "
    "':chat' are direct user messages, answer them conversationally."
)


def check_requirements() -> bool:
    return bool(os.environ.get("STAGEWHISPER_RELAY_TOKEN"))


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", None) or {}
    token = (extra.get("token") or os.environ.get("STAGEWHISPER_RELAY_TOKEN") or "").strip()
    if not token:
        return False
    try:
        port = int(
            extra.get("listen_port")
            or os.environ.get("STAGEWHISPER_LISTEN_PORT")
            or DEFAULT_PORT
        )
    except (TypeError, ValueError):
        return False
    if not (1024 <= port <= 65535):
        return False
    host = extra.get("listen_host") or os.environ.get("STAGEWHISPER_LISTEN_HOST", DEFAULT_HOST)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    return True


def _env_enablement() -> dict[str, Any] | None:
    token = os.environ.get("STAGEWHISPER_RELAY_TOKEN")
    if not token:
        return None
    try:
        port = int(os.environ.get("STAGEWHISPER_LISTEN_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        max_concurrent = int(
            os.environ.get("STAGEWHISPER_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT))
        )
    except (TypeError, ValueError):
        max_concurrent = DEFAULT_MAX_CONCURRENT
    try:
        callback_timeout_s = float(
            os.environ.get(
                "STAGEWHISPER_CALLBACK_TIMEOUT_S", str(DEFAULT_CALLBACK_TIMEOUT_S)
            )
        )
    except (TypeError, ValueError):
        callback_timeout_s = DEFAULT_CALLBACK_TIMEOUT_S
    try:
        dedup_cache_size = int(
            os.environ.get(
                "STAGEWHISPER_DEDUP_CACHE_SIZE", str(DEFAULT_DEDUP_CACHE_SIZE)
            )
        )
    except (TypeError, ValueError):
        dedup_cache_size = DEFAULT_DEDUP_CACHE_SIZE
    return {
        "token": token,
        "listen_host": os.environ.get("STAGEWHISPER_LISTEN_HOST", DEFAULT_HOST),
        "listen_port": port,
        "max_concurrent": max_concurrent,
        "callback_timeout_s": callback_timeout_s,
        "dedup_cache_size": dedup_cache_size,
    }


def register(ctx) -> None:
    config.load_env_file()
    allowed_users = (os.environ.get("STAGEWHISPER_ALLOWED_USERS") or "").strip()
    allow_all = (os.environ.get("STAGEWHISPER_ALLOW_ALL_USERS") or "").strip()
    if not allowed_users and not allow_all:
        logger.warning(
            "StageWhisper has no user allow policy configured: set STAGEWHISPER_ALLOWED_USERS "
            "(comma-separated Hermes user ids) or STAGEWHISPER_ALLOW_ALL_USERS=1 to grant access. "
            "Until one is set the platform stays closed to all users."
        )
    if not hasattr(ctx, "register_platform"):
        logger.warning(
            "Hermes ctx does not expose register_platform; StageWhisper adapter not registered"
        )
        return

    kwargs: dict[str, Any] = dict(
        name="stagewhisper",
        label="StageWhisper",
        adapter_factory=lambda cfg: StageWhisperAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["STAGEWHISPER_RELAY_TOKEN"],
        install_hint="pipx install hermes-platform-stagewhisper && stagewhisper-hermes-install",
        env_enablement_fn=_env_enablement,
        allowed_users_env="STAGEWHISPER_ALLOWED_USERS",
        allow_all_env="STAGEWHISPER_ALLOW_ALL_USERS",
        platform_hint=PLATFORM_HINT,
        max_message_length=4000,
        pii_safe=True,
        allow_update_command=False,
        emoji="🎙️",
    )

    optional_keys = [
        "emoji",
        "allow_update_command",
        "pii_safe",
        "allow_all_env",
        "allowed_users_env",
        "env_enablement_fn",
        "install_hint",
        "is_connected",
    ]

    attempts = [dict(kwargs)]
    current = dict(kwargs)
    for key in optional_keys:
        if key in current:
            current = {k: v for k, v in current.items() if k != key}
            attempts.append(dict(current))

    last_exc: TypeError | None = None
    for attempt in attempts:
        try:
            ctx.register_platform(**attempt)
            return
        except TypeError as exc:
            last_exc = exc
            continue

    if last_exc is not None:
        raise last_exc
