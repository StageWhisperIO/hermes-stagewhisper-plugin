from __future__ import annotations

import argparse
import asyncio
import sys

from .environment import resolve_harness_paths
from .scenario import run_cross_member_isolation_scenario


def _print_step(message: str) -> None:
    print(f"-> {message}")


async def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        prog="stagewhisper-governance-demo",
        description=(
            "Provision two Hermes org members as real, separate profile-scoped "
            "gateway processes; run a call for member A that commits a secret "
            "codeword to memory; verify member B can neither answer with it nor "
            "find it anywhere in B's own memories/ or state.db."
        ),
    )
    parser.parse_args(argv)

    paths = resolve_harness_paths()
    if paths.missing_reason:
        print(f"Cannot run demo: {paths.missing_reason}", file=sys.stderr)
        return 2

    print("StageWhisper Hermes org-multi-member isolation demo")
    print(f"Harness root: {paths.hermes_root}")
    print()

    result = await run_cross_member_isolation_scenario(
        hermes_root=paths.hermes_root,
        harness_home=paths.harness_home,
        hermes_bin=paths.hermes_bin,
        harness_default_env=paths.harness_default_env,
        on_step=_print_step,
    )

    print()
    print(f"Codeword for this run: {result.codeword}")
    print(f"Member A ({result.member_a.label}) replies:")
    for reply in result.a_call_log.replies:
        print(f"    {reply!r}")
    print(f"Member B ({result.member_b.label}) replies:")
    for reply in result.b_call_log.replies:
        print(f"    {reply!r}")
    print()

    checks = [
        (
            "A committed its own codeword to its own memory",
            result.a_committed_codeword_to_its_own_memory,
            [str(m) for m in result.a_memory_matches],
        ),
        (
            "B did not answer with A's codeword",
            not result.b_leaked_codeword_in_its_reply,
            [],
        ),
        (
            "A's codeword is absent from B's memories/",
            not result.b_memory_matches,
            [str(m) for m in result.b_memory_matches],
        ),
        (
            "A's codeword is absent from B's state.db",
            not result.b_state_db_matches,
            [str(m) for m in result.b_state_db_matches],
        ),
    ]

    all_passed = True
    for description, passed, details in checks:
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {description}")
        for detail in details:
            print(f"        {detail}")
        all_passed = all_passed and passed

    print()
    print("RESULT: ISOLATION HOLDS" if all_passed else "RESULT: ISOLATION VIOLATED")
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
