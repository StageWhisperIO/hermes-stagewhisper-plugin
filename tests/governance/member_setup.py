from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from hermes_stagewhisper_plugin import install

MODEL_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL")

CONFIG_YAML_TEMPLATE = """model:
  default: {model_default}
  provider: {model_provider}
plugins:
  enabled:
    - stagewhisper
"""

PROFILE_SUBDIRS = (
    "memories",
    "sessions",
    "skills",
    "skins",
    "logs",
    "plans",
    "workspace",
    "cron",
    "home",
    "plugins",
)

DEFAULT_BASE_PORT = 19765
DEFAULT_PORT_RANGE = 1000


class MissingModelCredentialsError(Exception):
    pass


@dataclass(frozen=True)
class WorkingMember:
    label: str
    index: int
    profile: str
    profile_dir: Path
    port: int
    token: str
    relay_url: str


def slugify_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.strip().lower()).strip("-")
    return slug or "member"


def profile_dir_for(hermes_root: Path, profile: str) -> Path:
    return hermes_root / "profiles" / profile


def _port_for_profile(profile: str, base_port: int, port_range: int) -> int:
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:8]
    return base_port + (int(digest, 16) % port_range)


def _read_model_env_lines(harness_default_env: Path) -> list[str]:
    if not harness_default_env.exists():
        raise MissingModelCredentialsError(
            f"no .env found at {harness_default_env}; the harness default profile "
            "must already have a working model provider configured"
        )
    text = harness_default_env.read_text(encoding="utf-8")
    carried = [
        line
        for line in text.splitlines()
        if any(line.startswith(f"{key}=") for key in MODEL_ENV_KEYS)
    ]
    found_keys = {line.split("=", 1)[0] for line in carried}
    missing = [key for key in MODEL_ENV_KEYS if key not in found_keys]
    if missing:
        raise MissingModelCredentialsError(
            f"{harness_default_env} is missing {missing}; cannot stand up a real "
            "model-backed member gateway without them"
        )
    return carried


def _bootstrap_profile_dirs(profile_dir: Path) -> None:
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True)
    profile_dir.chmod(0o700)
    for subdir in PROFILE_SUBDIRS:
        (profile_dir / subdir).mkdir()


def _write_config_yaml(profile_dir: Path, model_default: str, model_provider: str) -> None:
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        CONFIG_YAML_TEMPLATE.format(model_default=model_default, model_provider=model_provider),
        encoding="utf-8",
    )
    config_path.chmod(0o600)


def _write_env(profile_dir: Path, model_env_lines: list[str], token: str, port: int) -> None:
    env_path = profile_dir / ".env"
    lines = [
        f'STAGEWHISPER_RELAY_TOKEN="{token}"',
        f'STAGEWHISPER_LISTEN_PORT="{port}"',
        'STAGEWHISPER_ALLOW_ALL_USERS="1"',
        'GATEWAY_ALLOW_ALL_USERS="true"',
    ] + model_env_lines
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


def _install_plugin_shim(profile_dir: Path) -> None:
    install.main(["--plugins-dir", str(profile_dir / "plugins")], print_next_steps=False)


def provision_working_member(
    label: str,
    index: int,
    hermes_root: Path,
    *,
    harness_default_env: Path,
    model_default: str = "deepseek-chat",
    model_provider: str = "openai-api",
    base_port: int = DEFAULT_BASE_PORT,
    port_range: int = DEFAULT_PORT_RANGE,
) -> WorkingMember:
    profile = slugify_label(label)
    profile_dir = profile_dir_for(hermes_root, profile)
    port = _port_for_profile(profile, base_port, port_range)
    token = secrets.token_urlsafe(36)

    model_env_lines = _read_model_env_lines(harness_default_env)
    _bootstrap_profile_dirs(profile_dir)
    _write_config_yaml(profile_dir, model_default, model_provider)
    _write_env(profile_dir, model_env_lines, token, port)
    _install_plugin_shim(profile_dir)

    return WorkingMember(
        label=label,
        index=index,
        profile=profile,
        profile_dir=profile_dir,
        port=port,
        token=token,
        relay_url=f"http://127.0.0.1:{port}",
    )


def remove_provisioned_labels(hermes_root: Path, labels: tuple[str, ...]) -> None:
    for label in labels:
        profile_dir = profile_dir_for(hermes_root, slugify_label(label))
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
