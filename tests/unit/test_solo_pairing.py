from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_stagewhisper_plugin import cli, config


def use_profile(tmp_path: Path, monkeypatch) -> Path:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in config.PERSISTED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return env_path


def payload_from(capsys) -> dict:
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith(cli.PAIRING_CODE_PREFIX):
            raw = line[len(cli.PAIRING_CODE_PREFIX):]
            import base64

            return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    raise AssertionError("no pairing code printed")


def test_a_solo_host_gets_a_token_without_being_provisioned(tmp_path, monkeypatch, capsys):
    env_path = use_profile(tmp_path, monkeypatch)

    assert cli._cmd_pair_code(None, "Mac mini") == 0

    written = env_path.read_text(encoding="utf-8")
    assert "STAGEWHISPER_RELAY_TOKEN=" in written
    assert len(payload_from(capsys)["token"]) == config.RELAY_TOKEN_LENGTH


def test_running_pair_code_twice_keeps_the_same_token(tmp_path, monkeypatch, capsys):
    use_profile(tmp_path, monkeypatch)

    cli._cmd_pair_code(None, "Mac mini")
    first = payload_from(capsys)["token"]
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", first)
    cli._cmd_pair_code(None, "Mac mini")

    assert payload_from(capsys)["token"] == first


def test_pairing_at_a_tailnet_address_allows_that_host_through_the_ingress_guard(
    tmp_path, monkeypatch, capsys
):
    env_path = use_profile(tmp_path, monkeypatch)

    cli._cmd_pair_code("https://mac-mini.tail34b074.ts.net:8765", "Mac mini")

    assert 'STAGEWHISPER_ALLOW_INGRESS_HOSTS="mac-mini.tail34b074.ts.net"' in (
        env_path.read_text(encoding="utf-8")
    )


def test_pairing_on_loopback_does_not_widen_the_ingress_guard(tmp_path, monkeypatch, capsys):
    env_path = use_profile(tmp_path, monkeypatch)

    cli._cmd_pair_code(None, "Mac mini")

    assert "STAGEWHISPER_ALLOW_INGRESS_HOSTS" not in env_path.read_text(encoding="utf-8")


def test_pair_code_tells_the_user_to_restart_the_gateway_when_it_mints_a_new_token(
    tmp_path, monkeypatch, capsys
):
    use_profile(tmp_path, monkeypatch)

    cli._cmd_pair_code(None, "Mac mini")

    assert "restart" in capsys.readouterr().out.lower()


def test_pair_code_tells_the_user_to_restart_the_gateway_when_it_widens_the_ingress_allowlist(
    tmp_path, monkeypatch, capsys
):
    use_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", "tok-123")

    cli._cmd_pair_code("https://mac-mini.tail34b074.ts.net:8765", "Mac mini")

    assert "restart" in capsys.readouterr().out.lower()


def test_pair_code_does_not_nag_about_restarting_when_settings_are_unchanged(
    tmp_path, monkeypatch, capsys
):
    use_profile(tmp_path, monkeypatch)

    cli._cmd_pair_code(None, "Mac mini")
    first_token = payload_from(capsys)["token"]
    monkeypatch.setenv("STAGEWHISPER_RELAY_TOKEN", first_token)

    cli._cmd_pair_code(None, "Mac mini")

    assert "restart" not in capsys.readouterr().out.lower()
