"""Pairing config persistence for the StageWhisper Hermes plugin."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

ENV_PATH = Path.home() / ".hermes" / ".env"

DEFAULT_LISTEN_PORT = 8765

PERSISTED_ENV_VARS = ("STAGEWHISPER_RELAY_TOKEN", "STAGEWHISPER_LISTEN_PORT")

_loaded = False


def load_env_file(path: str | Path | None = None) -> None:
    global _loaded
    if _loaded and path is None:
        return
    if path is None:
        _loaded = True

    env_path = Path(path).expanduser() if path else ENV_PATH
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            parts = [line]
        if not parts or "=" not in parts[0]:
            continue
        key, _, value = parts[0].partition("=")
        key = key.strip()
        if key.startswith("STAGEWHISPER_") and value:
            os.environ[key] = value


def save_pairing(overrides: dict[str, str], path: str | Path | None = None) -> None:
    env_path = Path(path).expanduser() if path else ENV_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)

    current_lines: list[str] = []
    if env_path.exists():
        current_lines = env_path.read_text(encoding="utf-8").splitlines()

    new_lines = [
        line for line in current_lines if not _line_has_key(line, PERSISTED_ENV_VARS)
    ]
    for key in PERSISTED_ENV_VARS:
        value = overrides.get(key, "")
        if value:
            new_lines.append(f'{key}="{_quote_env_value(value)}"')

    tmp_path = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    env_path.chmod(0o600)


def _quote_env_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace('"', '\\"')
    )


def _line_has_key(line: str, keys: tuple[str, ...]) -> bool:
    return any(
        line.startswith(key + "=") or line.startswith(key + '="') for key in keys
    )


def get_relay_token() -> str | None:
    return os.getenv("STAGEWHISPER_RELAY_TOKEN")
