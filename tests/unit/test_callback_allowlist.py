from __future__ import annotations

import pytest

from hermes_stagewhisper_plugin.models import is_allowed_callback_url, validate_incoming

ALLOW_ENV = "STAGEWHISPER_ALLOW_CALLBACK_URLS"
INGRESS_ENV = "STAGEWHISPER_ALLOW_INGRESS_HOSTS"
ALLOWED = "https://my-mac.tailnet-name.ts.net"


def _event(callback_url: str) -> dict:
    return {
        "task_id": "11111111-2222-3333-4444-555555555555",
        "session_id": "sess-1",
        "reason": "chat_message",
        "occurred_at": "2026-01-01T00:00:00Z",
        "payload": {"text": "hi", "user_message_id": "umsg-1"},
        "callback": {"url": callback_url, "token": "callback-token-32-chars-aaaaaaaa"},
    }


def test_loopback_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert is_allowed_callback_url("http://127.0.0.1:8788")
    assert is_allowed_callback_url("http://localhost:8788")


def test_remote_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert not is_allowed_callback_url(ALLOWED)
    _, error = validate_incoming(_event(ALLOWED))
    assert error == "invalid_callback_url"


def test_only_exact_origin_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOWED)
    assert is_allowed_callback_url(ALLOWED)
    assert not is_allowed_callback_url("https://evil.example.com")
    event, error = validate_incoming(_event(ALLOWED))
    assert error is None
    assert event is not None


def test_different_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOWED)
    assert not is_allowed_callback_url(f"{ALLOWED}:9999")


def test_plain_http_rejected_unless_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOWED)
    assert not is_allowed_callback_url("http://my-mac.tailnet-name.ts.net")


def test_plain_http_allowed_when_exact_origin_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "http://my-mac.tailnet-name.ts.net:8788")
    assert is_allowed_callback_url("http://my-mac.tailnet-name.ts.net:8788")


def test_path_or_query_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOWED)
    assert not is_allowed_callback_url(f"{ALLOWED}/tasks")
    assert not is_allowed_callback_url(f"{ALLOWED}/?x=1")


def test_loopback_rejected_once_remote_ingress_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.setenv(INGRESS_ENV, "my-vps.tailnet-name.ts.net")
    assert not is_allowed_callback_url("http://127.0.0.1:8788")
    assert not is_allowed_callback_url("http://localhost:8788")
    _, error = validate_incoming(_event("http://127.0.0.1:8788"))
    assert error == "invalid_callback_url"


def test_loopback_allowed_under_remote_ingress_when_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INGRESS_ENV, "my-vps.tailnet-name.ts.net")
    monkeypatch.setenv(ALLOW_ENV, "http://127.0.0.1:8788")
    assert is_allowed_callback_url("http://127.0.0.1:8788")
    assert not is_allowed_callback_url("http://127.0.0.1:9999")


def test_malformed_port_rejected_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, ALLOWED)
    assert not is_allowed_callback_url("https://example.com:bad")
    assert not is_allowed_callback_url("https://example.com:99999")
    _, error = validate_incoming(_event("https://example.com:bad"))
    assert error == "invalid_callback_url"


def test_malformed_port_in_allowlist_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, f"https://example.com:bad,{ALLOWED}")
    assert is_allowed_callback_url(ALLOWED)
    assert not is_allowed_callback_url("https://example.com:bad")
