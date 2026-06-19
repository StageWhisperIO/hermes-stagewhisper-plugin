from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import cli, config


def _decode_pairing_code(code: str) -> dict[str, str]:
    assert code.startswith(cli.PAIRING_CODE_PREFIX)
    payload = code[len(cli.PAIRING_CODE_PREFIX):]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


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


def test_save_pairing_and_load_round_trip(monkeypatch: pytest.MonkeyPatch, _isolate_env: Path) -> None:
    config.save_pairing(
        {
            "STAGEWHISPER_API_URL": "https://sw.test",
            "STAGEWHISPER_INTEGRATION_ID": "int-1",
            "STAGEWHISPER_RELAY_TOKEN": "tok-secret",
        }
    )
    for var in config.PAIRING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_loaded", False)

    config.load_env_file()

    assert os.environ["STAGEWHISPER_API_URL"] == "https://sw.test"
    assert os.environ["STAGEWHISPER_INTEGRATION_ID"] == "int-1"
    assert os.environ["STAGEWHISPER_RELAY_TOKEN"] == "tok-secret"
    assert config.is_paired() is True


def test_save_pairing_writes_owner_only_permissions(_isolate_env: Path) -> None:
    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "tok"})
    mode = stat.S_IMODE(_isolate_env.stat().st_mode)
    assert mode == 0o600


def test_save_pairing_preserves_unrelated_lines(_isolate_env: Path) -> None:
    _isolate_env.parent.mkdir(parents=True, exist_ok=True)
    _isolate_env.write_text('OPENAI_API_KEY="keep-me"\n', encoding="utf-8")

    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "tok"})

    contents = _isolate_env.read_text(encoding="utf-8")
    assert 'OPENAI_API_KEY="keep-me"' in contents
    assert 'STAGEWHISPER_RELAY_TOKEN="tok"' in contents


def test_unpair_clears_pairing_lines(monkeypatch: pytest.MonkeyPatch, _isolate_env: Path) -> None:
    config.save_pairing(
        {
            "STAGEWHISPER_API_URL": "https://sw.test",
            "STAGEWHISPER_INTEGRATION_ID": "int-1",
            "STAGEWHISPER_RELAY_TOKEN": "tok",
        }
    )
    for var in config.PAIRING_ENV_VARS:
        monkeypatch.setenv(var, "stale")

    rc = cli._cmd_unpair()

    assert rc == 0
    assert config.is_paired() is False
    contents = _isolate_env.read_text(encoding="utf-8")
    assert "STAGEWHISPER_RELAY_TOKEN" not in contents


def test_cmd_pair_persists_credentials(monkeypatch: pytest.MonkeyPatch, _isolate_env: Path) -> None:
    async def fake_complete(api_url: str, code: str, label: str) -> dict[str, str]:
        assert api_url == "https://sw.test"
        assert code == "ABC123"
        assert label == "MyHost"
        return {"integration_id": "int-42", "relay_token": "tok-42"}

    monkeypatch.setattr(cli, "_complete_pairing", fake_complete)

    rc = cli._cmd_pair(
        "ABC123", "MyHost", "https://sw.test", 8765, restart_gateway=False
    )

    assert rc == 0
    assert os.environ["STAGEWHISPER_RELAY_TOKEN"] == "tok-42"
    assert os.environ["STAGEWHISPER_INTEGRATION_ID"] == "int-42"
    assert os.environ["STAGEWHISPER_LISTEN_PORT"] == "8765"
    assert config.is_paired() is True
    contents = _isolate_env.read_text(encoding="utf-8")
    assert 'STAGEWHISPER_RELAY_TOKEN="tok-42"' in contents
    assert 'STAGEWHISPER_LISTEN_PORT="8765"' in contents


def test_cmd_pair_persists_custom_listen_port(monkeypatch: pytest.MonkeyPatch, _isolate_env: Path) -> None:
    async def fake_complete(api_url: str, code: str, label: str) -> dict[str, str]:
        return {"integration_id": "int-9", "relay_token": "tok-9"}

    monkeypatch.setattr(cli, "_complete_pairing", fake_complete)

    rc = cli._cmd_pair(
        "ABC123", "MyHost", "https://sw.test", 9100, restart_gateway=False
    )

    assert rc == 0
    assert os.environ["STAGEWHISPER_LISTEN_PORT"] == "9100"


def test_cmd_pair_rejects_short_code(_isolate_env: Path) -> None:
    assert (
        cli._cmd_pair("ab", "MyHost", "https://sw.test", 8765, restart_gateway=False)
        == 1
    )


def test_cmd_pair_rejects_invalid_listen_port(_isolate_env: Path) -> None:
    assert (
        cli._cmd_pair("ABC123", "MyHost", "https://sw.test", 80, restart_gateway=False)
        == 1
    )


def test_pair_code_generates_and_persists_token(capsys: pytest.CaptureFixture[str], _isolate_env: Path) -> None:
    rc = cli._cmd_pair_code(None, 8765, "Hermes")

    assert rc == 0
    token = os.environ["STAGEWHISPER_RELAY_TOKEN"]
    assert len(token) >= 32
    assert os.environ["STAGEWHISPER_LISTEN_PORT"] == "8765"

    out = capsys.readouterr().out
    code_line = next(l.strip() for l in out.splitlines() if l.strip().startswith(cli.PAIRING_CODE_PREFIX))
    decoded = _decode_pairing_code(code_line)
    assert decoded == {"url": "http://127.0.0.1:8765", "token": token, "label": "Hermes"}


def test_pair_code_reuses_existing_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _isolate_env: Path) -> None:
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "already-have-this-token-value-123")

    rc = cli._cmd_pair_code("https://relay.example.com", 8765, "Hermes")

    assert rc == 0
    out = capsys.readouterr().out
    code_line = next(l.strip() for l in out.splitlines() if l.strip().startswith(cli.PAIRING_CODE_PREFIX))
    decoded = _decode_pairing_code(code_line)
    assert decoded["token"] == "already-have-this-token-value-123"
    assert decoded["url"] == "https://relay.example.com"


def test_pair_code_rejects_invalid_port(_isolate_env: Path) -> None:
    assert cli._cmd_pair_code(None, 80, "Hermes") == 1


def test_cmd_pair_does_not_persist_on_remote_failure(monkeypatch: pytest.MonkeyPatch, _isolate_env: Path) -> None:
    async def fake_complete(api_url: str, code: str, label: str) -> dict[str, str]:
        raise RuntimeError("pairing failed (400): nope")

    monkeypatch.setattr(cli, "_complete_pairing", fake_complete)

    rc = cli._cmd_pair(
        "ABC123", "MyHost", "https://sw.test", 8765, restart_gateway=False
    )

    assert rc == 1
    assert not _isolate_env.exists()
    assert config.is_paired() is False
