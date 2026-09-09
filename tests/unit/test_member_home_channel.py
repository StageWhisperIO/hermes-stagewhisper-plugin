from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import config

OPS_DIR = Path(config.__file__).resolve().parent / "ops"
COMMON_SH = OPS_DIR / "lib" / "common.sh"
PROVISION_SH = OPS_DIR / "provision-member.sh"

HOME_CHANNEL_VAR = "STAGEWHISPER_HOME_CHANNEL"


def run_common_sh_function(call: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}"; {call}'],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_every_member_gets_a_home_channel_so_hermes_answers_instead_of_asking_for_one() -> (
    None
):
    assert run_common_sh_function("home_channel_for alice") == "sw:home-alice:chat"


def test_two_members_do_not_share_a_home_channel() -> None:
    alice = run_common_sh_function("home_channel_for alice")
    bob = run_common_sh_function("home_channel_for bob")

    assert alice != bob


def test_provisioning_writes_the_home_channel_into_the_members_env() -> None:
    assert HOME_CHANNEL_VAR in PROVISION_SH.read_text(encoding="utf-8")


def test_provisioning_tells_the_operator_how_to_approve_that_members_device() -> None:
    script = PROVISION_SH.read_text(encoding="utf-8")

    assert "pairing approve stagewhisper" in script
    assert "runuser -u ${label}" in script
    assert "HERMES_HOME=${hermes_home}" in script


def test_repairing_a_member_leaves_their_home_channel_intact(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'STAGEWHISPER_RELAY_TOKEN="old-token"\n'
        f'{HOME_CHANNEL_VAR}="sw:home-alice:chat"\n',
        encoding="utf-8",
    )

    config.save_pairing({"STAGEWHISPER_RELAY_TOKEN": "new-token"}, path=env_path)

    written = env_path.read_text(encoding="utf-8")
    assert f'{HOME_CHANNEL_VAR}="sw:home-alice:chat"' in written
    assert "new-token" in written


def test_the_home_channel_is_not_rewritten_as_a_pairing_value(tmp_path: Path) -> None:
    assert HOME_CHANNEL_VAR not in config.PERSISTED_ENV_VARS


@pytest.mark.parametrize("uid,expected", [(100, 8765), (999, 9664)])
def test_a_members_port_follows_from_their_uid(uid: int, expected: int) -> None:
    assert run_common_sh_function(f"port_for_uid {uid}") == str(expected)
