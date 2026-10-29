from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp


class GatewayStartupError(Exception):
    pass


@dataclass
class MemberGateway:
    profile: str
    profile_dir: Path
    port: int
    hermes_bin: Path
    harness_home: Path
    log_path: Path
    startup_timeout: float = 45.0
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(self.log_path, "wb")
        env = dict(os.environ)
        env["HOME"] = str(self.harness_home)
        env["HERMES_HOME"] = str(self.profile_dir)
        env["PIP_CONFIG_FILE"] = "/dev/null"

        self._process = subprocess.Popen(
            [
                str(self.hermes_bin),
                "-p",
                self.profile,
                "gateway",
                "--accept-hooks",
                "run",
                "--replace",
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()

        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise GatewayStartupError(
                        f"gateway for profile {self.profile!r} exited early with code "
                        f"{self._process.returncode}; log:\n{self._tail_log()}"
                    )
                try:
                    async with session.get(
                        f"http://127.0.0.1:{self.port}/v1/health",
                        headers={"Host": "127.0.0.1"},
                        timeout=aiohttp.ClientTimeout(total=2.0),
                    ) as response:
                        if response.status == 200:
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                await asyncio.sleep(0.5)

        raise GatewayStartupError(
            f"gateway for profile {self.profile!r} did not become healthy on port "
            f"{self.port} within {self.startup_timeout}s (last error: {last_error}); "
            f"log:\n{self._tail_log()}"
        )

    def _tail_log(self, max_chars: int = 4000) -> str:
        if not self.log_path.exists():
            return "(no log file)"
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    async def stop(self, timeout: float = 10.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            await asyncio.sleep(0.25)

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
