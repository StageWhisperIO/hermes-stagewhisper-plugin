from __future__ import annotations

import os
from typing import Any

import pytest

from hermes_stagewhisper_plugin.adapter import is_connected
from hermes_stagewhisper_plugin.register import (
    PLATFORM_HINT,
    _env_enablement,
    check_requirements,
    register,
    validate_config,
)


class FakeCtx:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def register_platform(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "STAGEWHISPER_RELAY_TOKEN",
        "STAGEWHISPER_LISTEN_PORT",
        "STAGEWHISPER_LISTEN_HOST",
        "STAGEWHISPER_MAX_CONCURRENT",
        "STAGEWHISPER_DEDUP_CACHE_SIZE",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_register_passes_every_kwarg_to_ctx() -> None:
    ctx = FakeCtx()
    register(ctx)
    assert ctx.kwargs is not None
    assert ctx.kwargs["name"] == "stagewhisper"
    assert ctx.kwargs["label"] == "StageWhisper"
    assert callable(ctx.kwargs["adapter_factory"])
    assert ctx.kwargs["check_fn"] is check_requirements
    assert ctx.kwargs["validate_config"] is validate_config
    assert ctx.kwargs["required_env"] == ["STAGEWHISPER_RELAY_TOKEN"]
    assert "pipx" in ctx.kwargs["install_hint"]
    assert ctx.kwargs["env_enablement_fn"] is _env_enablement
    assert ctx.kwargs["is_connected"] is is_connected
    assert ctx.kwargs["allowed_users_env"] == "STAGEWHISPER_ALLOWED_USERS"
    assert ctx.kwargs["allow_all_env"] == "STAGEWHISPER_ALLOW_ALL_USERS"
    assert ":reasoning" in ctx.kwargs["platform_hint"]
    assert ctx.kwargs["max_message_length"] == 4000
    assert ctx.kwargs["pii_safe"] is True
    assert ctx.kwargs["allow_update_command"] is False
    assert ctx.kwargs["emoji"]


def test_register_does_not_auto_grant_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGEWHISPER_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("STAGEWHISPER_ALLOW_ALL_USERS", raising=False)
    ctx = FakeCtx()
    register(ctx)
    assert os.environ.get("STAGEWHISPER_ALLOW_ALL_USERS") in (None, "")
    assert ctx.kwargs is not None
    assert ctx.kwargs["allowed_users_env"] == "STAGEWHISPER_ALLOWED_USERS"
    assert ctx.kwargs["allow_all_env"] == "STAGEWHISPER_ALLOW_ALL_USERS"


def test_env_enablement_none_when_token_missing() -> None:
    assert _env_enablement() is None


def test_env_enablement_present_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "xyz")
    monkeypatch.setenv("STAGEWHISPER_LISTEN_PORT", "9001")
    result = _env_enablement()
    assert result is not None
    assert result["token"] == "xyz"
    assert result["listen_port"] == 9001
    assert result["listen_host"] == "127.0.0.1"
    assert result["max_concurrent"] == 4
    assert result["dedup_cache_size"] == 2048


def test_env_enablement_surfaces_dedup_cache_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "xyz")
    monkeypatch.setenv("STAGEWHISPER_DEDUP_CACHE_SIZE", "512")
    result = _env_enablement()
    assert result is not None
    assert result["dedup_cache_size"] == 512


def test_validate_config_rejects_bad_port() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"token": "xyz", "listen_port": 80, "listen_host": "127.0.0.1"})
    assert validate_config(cfg) is False


def test_validate_config_rejects_non_loopback_host() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"token": "xyz", "listen_port": 8765, "listen_host": "0.0.0.0"})
    assert validate_config(cfg) is False


def test_validate_config_accepts_valid_input() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"token": "xyz", "listen_port": 8765, "listen_host": "127.0.0.1"})
    assert validate_config(cfg) is True


def test_validate_config_accepts_default_port_when_unset() -> None:
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(extra={"token": "xyz"})
    assert validate_config(cfg) is True


def test_platform_hint_asks_for_a_speakable_cue_instead_of_a_direction_to_translate() -> None:
    assert "say or ask right now" in PLATFORM_HINT
    assert "never the exact words to read aloud" not in PLATFORM_HINT
    assert "12 words max" in PLATFORM_HINT
    assert "only respond when you have something genuinely useful to say" in PLATFORM_HINT
    assert "':chat' are direct user messages, answer them conversationally" in PLATFORM_HINT


def test_register_falls_back_when_ctx_rejects_optional_kwargs() -> None:
    class StrictCtx:
        ACCEPTED = {"name", "label", "adapter_factory", "check_fn", "validate_config", "required_env", "platform_hint", "max_message_length"}

        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        def register_platform(self, **kwargs: Any) -> None:
            unknown = set(kwargs) - self.ACCEPTED
            if unknown:
                raise TypeError(f"unexpected kwargs: {unknown}")
            self.kwargs = kwargs

    ctx = StrictCtx()
    register(ctx)
    assert ctx.kwargs is not None
    assert ctx.kwargs["name"] == "stagewhisper"
