from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import config

STAGEWHISPER_PREFIX = "STAGEWHISPER_"


@pytest.fixture(autouse=True)
def _restore_stagewhisper_env():
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(STAGEWHISPER_PREFIX)
    }
    try:
        yield
    finally:
        for key in [
            key for key in os.environ if key.startswith(STAGEWHISPER_PREFIX)
        ]:
            del os.environ[key]
        os.environ.update(saved)


def test_resolve_env_path_uses_the_current_hermes_home_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_home = tmp_path / "profiles" / "alice"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert config.resolve_env_path() == profile_home / ".env"


def test_resolve_env_path_falls_back_to_home_dot_hermes_when_hermes_home_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert config.resolve_env_path() == Path.home() / ".hermes" / ".env"


def test_two_profiles_hermes_homes_resolve_to_two_different_env_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "alice"))
    alice_env = config.resolve_env_path()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "bob"))
    bob_env = config.resolve_env_path()

    assert alice_env != bob_env


def test_a_provisioned_tailnet_host_reaches_the_ingress_allowlist_through_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_stagewhisper_plugin.models import allowed_ingress_hosts

    for var in config.PERSISTED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text(
        'STAGEWHISPER_RELAY_TOKEN="tok"\n'
        'STAGEWHISPER_LISTEN_PORT="9664"\n'
        'STAGEWHISPER_ALLOW_INGRESS_HOSTS="alice.your-tailnet.ts.net"\n',
        encoding="utf-8",
    )

    config.load_env_file(env_path)

    assert allowed_ingress_hosts() == {"alice.your-tailnet.ts.net"}


def test_the_ingress_allowlist_is_persisted_so_rewriting_the_env_file_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "STAGEWHISPER_ALLOW_INGRESS_HOSTS" in config.PERSISTED_ENV_VARS

    env_path = tmp_path / ".env"
    config.save_pairing(
        {
            "STAGEWHISPER_RELAY_TOKEN": "tok",
            "STAGEWHISPER_LISTEN_PORT": "9664",
            "STAGEWHISPER_ALLOW_INGRESS_HOSTS": "alice.your-tailnet.ts.net",
        },
        env_path,
    )

    assert "alice.your-tailnet.ts.net" in env_path.read_text(encoding="utf-8")
