from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

IMAGE = "alpine:3.20"
RUN_TIMEOUT_SECONDS = 60.0

ALICE_ENV_TOKEN = "swalice-fake-bearer-token-9f3a7c1e2b6d"
ALICE_USER_MD_SECRET = "ALICE-SECRET-USER-MD-VALUE-4d2f9a"

BEGIN_MARKER = "BEGIN_CHECK"
EXIT_MARKER = "EXIT_CODE"
END_MARKER = "END_CHECK"

ENTRY_SCRIPT = f"""
adduser -D -h /home/swalice -s /bin/sh swalice
adduser -D -h /home/swbob -s /bin/sh swbob
chmod 0700 /home/swalice /home/swbob
mkdir -p /home/swalice/memories
printf '{ALICE_ENV_TOKEN}\\n' > /home/swalice/.env
chmod 0600 /home/swalice/.env
printf '{ALICE_USER_MD_SECRET}\\n' > /home/swalice/memories/USER.md
chown -R swalice:swalice /home/swalice
chown -R swbob:swbob /home/swbob

run_check() {{
  name="$1"
  user="$2"
  cmd="$3"
  echo "{BEGIN_MARKER} $name"
  su "$user" -c "$cmd" 2>&1
  echo "{EXIT_MARKER} $?"
  echo "{END_MARKER} $name"
}}

run_check bob_read_env swbob "cat /home/swalice/.env"
run_check bob_read_user_md swbob "cat /home/swalice/memories/USER.md"
run_check bob_list_home swbob "ls -la /home/swalice"
run_check bob_write_new_file swbob "sh -c \\"echo intruder-write > /home/swalice/intruder.txt\\""
run_check bob_overwrite_user_md swbob "sh -c \\"echo bob-was-here > /home/swalice/memories/USER.md\\""

run_check alice_read_env swalice "cat /home/swalice/.env"
run_check alice_read_user_md swalice "cat /home/swalice/memories/USER.md"
run_check alice_list_home swalice "ls -la /home/swalice"
run_check alice_write_new_file swalice "sh -c \\"echo alice-write > /home/swalice/newfile.txt\\""
run_check alice_overwrite_user_md swalice "sh -c \\"echo alice-updated-value > /home/swalice/memories/USER.md\\""
"""


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    exit_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def denied_with_permission_error(self) -> bool:
        return self.exit_code != 0 and "permission denied" in self.output.lower()


class DockerRunError(Exception):
    pass


def docker_unavailable_reason() -> str | None:
    docker_path = shutil.which("docker")
    if docker_path is None:
        return "docker CLI not found on PATH"
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker info failed to run: {exc}"
    if result.returncode != 0:
        return f"docker info exited {result.returncode}: {result.stderr.strip()}"
    return None


def _parse_checks(stdout: str) -> dict[str, CheckOutcome]:
    outcomes: dict[str, CheckOutcome] = {}
    name: str | None = None
    exit_code: int | None = None
    body_lines: list[str] = []

    for line in stdout.splitlines():
        if line.startswith(f"{BEGIN_MARKER} "):
            name = line[len(BEGIN_MARKER) + 1 :].strip()
            exit_code = None
            body_lines = []
            continue
        if line.startswith(f"{EXIT_MARKER} "):
            exit_code = int(line[len(EXIT_MARKER) + 1 :].strip())
            continue
        if line.startswith(f"{END_MARKER} "):
            ended_name = line[len(END_MARKER) + 1 :].strip()
            if name is not None and ended_name == name and exit_code is not None:
                outcomes[name] = CheckOutcome(
                    name=name, exit_code=exit_code, output="\n".join(body_lines)
                )
            name = None
            exit_code = None
            body_lines = []
            continue
        if name is not None:
            body_lines.append(line)

    return outcomes


def run_kernel_denial_checks() -> dict[str, CheckOutcome]:
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", IMAGE, "sh", "-c", ENTRY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerRunError(f"docker run timed out: {exc}") from exc

    if result.returncode != 0:
        raise DockerRunError(
            f"docker run exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    outcomes = _parse_checks(result.stdout)
    expected_names = {
        "bob_read_env",
        "bob_read_user_md",
        "bob_list_home",
        "bob_write_new_file",
        "bob_overwrite_user_md",
        "alice_read_env",
        "alice_read_user_md",
        "alice_list_home",
        "alice_write_new_file",
        "alice_overwrite_user_md",
    }
    missing = expected_names - outcomes.keys()
    if missing:
        raise DockerRunError(
            f"missing check output for {sorted(missing)}; raw stdout:\n{result.stdout}"
        )
    return outcomes
