from __future__ import annotations

import pytest

from .docker_denial import (
    ALICE_ENV_TOKEN,
    ALICE_USER_MD_SECRET,
    docker_unavailable_reason,
    run_kernel_denial_checks,
)

DOCKER_UNAVAILABLE_REASON = docker_unavailable_reason()

pytestmark = pytest.mark.skipif(
    DOCKER_UNAVAILABLE_REASON is not None,
    reason=f"Docker is unavailable: {DOCKER_UNAVAILABLE_REASON}",
)


@pytest.fixture(scope="module")
def checks() -> dict:
    return run_kernel_denial_checks()


@pytest.mark.parametrize(
    "check_name",
    [
        "bob_read_env",
        "bob_read_user_md",
        "bob_list_home",
        "bob_write_new_file",
        "bob_overwrite_user_md",
    ],
)
def test_a_member_cannot_traverse_into_another_members_home_via_kernel_dac(
    checks: dict, check_name: str
) -> None:
    outcome = checks[check_name]
    assert outcome.exit_code != 0, (
        f"{check_name} unexpectedly succeeded as bob against alice's home; "
        f"output was:\n{outcome.output}"
    )
    assert outcome.denied_with_permission_error, (
        f"{check_name} failed but not with a permission error, so this proves "
        f"nothing about the DAC boundary; output was:\n{outcome.output}"
    )


@pytest.mark.parametrize(
    "check_name",
    [
        "alice_read_env",
        "alice_read_user_md",
        "alice_list_home",
        "alice_write_new_file",
        "alice_overwrite_user_md",
    ],
)
def test_the_owning_member_can_still_read_and_write_its_own_home(
    checks: dict, check_name: str
) -> None:
    outcome = checks[check_name]
    assert outcome.succeeded, (
        f"positive control {check_name} unexpectedly failed for alice acting on "
        f"her own home; this would make the bob denials meaningless because the "
        f"paths might simply not exist. Output was:\n{outcome.output}"
    )


def test_alices_secrets_are_readable_by_alice_with_their_expected_content(
    checks: dict,
) -> None:
    assert ALICE_ENV_TOKEN in checks["alice_read_env"].output
    assert ALICE_USER_MD_SECRET in checks["alice_read_user_md"].output
