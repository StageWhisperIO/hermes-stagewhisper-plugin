from __future__ import annotations

import os

import pytest

from .environment import resolve_harness_paths
from .scenario import run_cross_member_isolation_scenario

HARNESS = resolve_harness_paths()

RUN_ENV_VAR = "STAGEWHISPER_RUN_GOVERNANCE_DEMO"

pytestmark = pytest.mark.skipif(
    os.environ.get(RUN_ENV_VAR, "") != "1",
    reason=(
        f"real-gateway governance demo skipped by default (it spawns real Hermes "
        f"processes and makes real model calls); set {RUN_ENV_VAR}=1 to run it"
    ),
)


@pytest.mark.asyncio
async def test_a_members_call_content_never_reaches_a_different_members_memory_or_state_db() -> None:
    if HARNESS.missing_reason:
        pytest.skip(HARNESS.missing_reason)

    result = await run_cross_member_isolation_scenario(
        hermes_root=HARNESS.hermes_root,
        harness_home=HARNESS.harness_home,
        hermes_bin=HARNESS.hermes_bin,
        harness_default_env=HARNESS.harness_default_env,
        on_step=print,
    )

    assert result.a_committed_codeword_to_its_own_memory, (
        f"member A ({result.member_a.label}) never wrote its own codeword "
        f"{result.codeword!r} anywhere under {result.member_a.profile_dir / 'memories'}; "
        f"A's replies were: {result.a_call_log.replies}"
    )

    assert not result.b_leaked_codeword_in_its_reply, (
        f"member B ({result.member_b.label}) answered with A's codeword "
        f"{result.codeword!r} it was never told: {result.b_call_log.replies}"
    )

    if result.b_memory_matches:
        locations = "\n".join(str(match) for match in result.b_memory_matches)
        pytest.fail(
            f"LEAK: A's codeword {result.codeword!r} found in B's own memories/ "
            f"directory at:\n{locations}"
        )

    if result.b_state_db_matches:
        locations = "\n".join(str(match) for match in result.b_state_db_matches)
        pytest.fail(
            f"LEAK: A's codeword {result.codeword!r} found in B's own state.db "
            f"at:\n{locations}"
        )
