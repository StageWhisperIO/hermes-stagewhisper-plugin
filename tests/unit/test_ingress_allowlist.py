from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_stagewhisper_plugin.listener import _host_header_ok

ALLOW_ENV = "STAGEWHISPER_ALLOW_INGRESS_HOSTS"
TAILNET_HOST = "my-mac.tailnet-name.ts.net"


def _request(host: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Host": host})


def test_loopback_host_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert _host_header_ok(_request("127.0.0.1"))
    assert _host_header_ok(_request("localhost:8765"))


def test_tailnet_host_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert not _host_header_ok(_request(TAILNET_HOST))


def test_tailnet_host_accepted_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, TAILNET_HOST)
    assert _host_header_ok(_request(TAILNET_HOST))
    assert _host_header_ok(_request(f"{TAILNET_HOST}:8765"))


def test_other_host_still_rejected_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, TAILNET_HOST)
    assert not _host_header_ok(_request("evil.example.com"))


def test_host_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, TAILNET_HOST)
    assert _host_header_ok(_request(TAILNET_HOST.upper()))


def test_missing_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, TAILNET_HOST)
    assert not _host_header_ok(_request(""))
