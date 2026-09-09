from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent / "ops"

PROVISION_SCRIPT = "provision-member.sh"
REVOKE_SCRIPT = "revoke-member.sh"


def run_ops_script(script_name: str, args: list[str]) -> int:
    script = OPS_DIR / script_name
    if not script.is_file():
        print(
            f"✗ The bundled operations script is missing: {script}\n"
            "  Reinstall hermes-platform-stagewhisper into the gateway's venv.",
            file=sys.stderr,
        )
        return 1

    bash = shutil.which("bash")
    if bash is None:
        print(
            "✗ bash was not found on PATH. Member provisioning needs a Linux host "
            "running systemd.",
            file=sys.stderr,
        )
        return 1

    if not Path("/run/systemd/system").exists():
        print(
            "✗ systemd is not running on this host. Member provisioning creates a "
            "systemd service per member and only works on a systemd Linux host.",
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ)
    env.setdefault("HERMES_VENV_PYTHON", sys.executable)
    return subprocess.run([bash, str(script), *args], env=env).returncode
