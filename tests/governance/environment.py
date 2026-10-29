from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HARNESS_ROOT = Path("/Users/piotrmrzyglowski/.stagewhisper-localtest")


@dataclass(frozen=True)
class HarnessPaths:
    harness_home: Path
    hermes_root: Path
    hermes_bin: Path
    harness_default_env: Path

    @property
    def missing_reason(self) -> str | None:
        if not self.hermes_bin.exists():
            return f"hermes binary not found at {self.hermes_bin}"
        if not self.harness_default_env.exists():
            return f"harness default profile .env not found at {self.harness_default_env}"
        return None


def resolve_harness_paths() -> HarnessPaths:
    root = Path(os.environ.get("STAGEWHISPER_GOVERNANCE_HARNESS_ROOT", str(DEFAULT_HARNESS_ROOT)))
    harness_home = root / "hermes" / "home"
    hermes_root = harness_home / ".hermes"
    hermes_bin = root / "hermes" / "venv" / "bin" / "hermes"
    return HarnessPaths(
        harness_home=harness_home,
        hermes_root=hermes_root,
        hermes_bin=hermes_bin,
        harness_default_env=hermes_root / ".env",
    )
