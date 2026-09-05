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
    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "tok-secret"})
    for var in config.PERSISTED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_loaded", False)

    config.load_env_file()

    assert os.environ["STAGEWHISPER_RELAY_TOKEN"] == "tok-secret"


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
    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "tok"})
    for var in config.PERSISTED_ENV_VARS:
        monkeypatch.setenv(var, "stale")

    rc = cli._cmd_unpair()

    assert rc == 0
    assert config.get_relay_token() is None
    contents = _isolate_env.read_text(encoding="utf-8")
    assert "STAGEWHISPER_RELAY_TOKEN" not in contents


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
