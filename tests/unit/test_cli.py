from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import cli, config


def _decode_pairing_code(code: str) -> dict[str, str]:
    assert code.startswith(cli.PAIRING_CODE_PREFIX)
    payload = code[len(cli.PAIRING_CODE_PREFIX):]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _pairing_code_line(out: str) -> str:
    return next(
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(cli.PAIRING_CODE_PREFIX)
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_path = tmp_path / ".hermes" / ".env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "_loaded", False)
    saved = {var: os.environ.pop(var, None) for var in config.PERSISTED_ENV_VARS}
    try:
        yield env_path
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_pair_code_renders_the_token_already_configured_for_this_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "already-provisioned-token")
    monkeypatch.setenv("STAGEWHISPER_LISTEN_PORT", "8765")

    rc = cli.main(["pair-code"])

    assert rc == 0
    out = capsys.readouterr().out
    decoded = _decode_pairing_code(_pairing_code_line(out))
    assert decoded == {
        "url": "http://127.0.0.1:8765",
        "token": "already-provisioned-token",
        "label": "Hermes",
    }


def test_pair_code_prints_a_desktop_link_carrying_the_same_payload_as_the_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "already-provisioned-token")
    monkeypatch.setenv("STAGEWHISPER_LISTEN_PORT", "8765")

    assert cli.main(["pair-code"]) == 0

    out = capsys.readouterr().out
    link = next(
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(cli.PAIRING_LINK_PREFIX)
    )
    payload = link[len(cli.PAIRING_LINK_PREFIX):]
    assert _decode_pairing_code(cli.PAIRING_CODE_PREFIX + payload) == _decode_pairing_code(
        _pairing_code_line(out)
    )


def test_the_desktop_link_payload_is_url_safe_so_it_survives_a_query_string(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "token-with+slash/and=pad")
    monkeypatch.setenv("STAGEWHISPER_LISTEN_PORT", "8765")

    assert cli.main(["pair-code"]) == 0

    out = capsys.readouterr().out
    link = next(
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(cli.PAIRING_LINK_PREFIX)
    )
    payload = link[len(cli.PAIRING_LINK_PREFIX):]
    assert set(payload) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_a_host_with_no_token_yet_is_given_one_so_it_can_pair_without_an_operator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["pair-code"])

    assert rc == 0
    assert cli.PAIRING_CODE_PREFIX in capsys.readouterr().out


def test_pair_code_persists_the_token_it_mints_so_the_next_run_matches(
    _isolate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["pair-code"])
    capsys.readouterr()

    assert _isolate_env.exists()
    assert "STAGEWHISPER_RELAY_TOKEN=" in _isolate_env.read_text(encoding="utf-8")


def test_pair_code_honors_an_explicit_url_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "tok-123")

    rc = cli.main(["pair-code", "--url", "https://relay.example.com"])

    assert rc == 0
    out = capsys.readouterr().out
    decoded = _decode_pairing_code(_pairing_code_line(out))
    assert decoded["url"] == "https://relay.example.com"


def test_pair_code_honors_an_explicit_label_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "tok-123")

    rc = cli.main(["pair-code", "--label", "Alice"])

    assert rc == 0
    out = capsys.readouterr().out
    decoded = _decode_pairing_code(_pairing_code_line(out))
    assert decoded["label"] == "Alice"


def test_status_reports_not_configured_when_no_token_is_present(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Relay: not configured" in out
    assert "Profile home" in out


def test_status_reports_configured_with_listen_port_and_profile_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_home = tmp_path / "profiles" / "alice"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "tok-123")
    monkeypatch.setenv("STAGEWHISPER_LISTEN_PORT", "8766")

    rc = cli.main(["status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Relay: configured" in out
    assert str(profile_home) in out
    assert "8766" in out


def test_unpair_clears_pairing_lines(
    monkeypatch: pytest.MonkeyPatch, _isolate_env: Path
) -> None:
    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "tok"})
    for var in config.PERSISTED_ENV_VARS:
        monkeypatch.setenv(var, "stale")

    rc = cli._cmd_unpair()

    assert rc == 0
    assert config.get_relay_token() is None
    contents = _isolate_env.read_text(encoding="utf-8")
    assert "STAGEWHISPER_RELAY_TOKEN" not in contents
