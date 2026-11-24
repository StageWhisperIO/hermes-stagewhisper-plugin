from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from hermes_stagewhisper_plugin import secure_fs

INHERITED_CONFIG_KEYS = ("model", "fallback_providers", "fallback_model", "model_catalog")
AUTH_FILENAME = "auth.json"
CONFIG_FILENAME = "config.yaml"


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_yaml_untrusted(path: Path) -> dict:
    dirfd = secure_fs.open_trusted_dir(path.parent)
    try:
        text = secure_fs.read_regular_file(dirfd, path.name)
    finally:
        os.close(dirfd)
    if text is None:
        return {}
    return yaml.safe_load(text) or {}


def write_private(path: Path, text: str, owner_uid: int, owner_gid: int) -> None:
    dirfd = secure_fs.open_trusted_dir(path.parent)
    try:
        secure_fs.write_regular_file(dirfd, path.name, text, owner_uid, owner_gid)
    finally:
        os.close(dirfd)


def credential_providers(auth: dict) -> list[str]:
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict):
        return []
    return sorted(name for name, entries in pool.items() if entries)


def credential_env_vars(auth: dict) -> list[str]:
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict):
        return []
    names: list[str] = []
    for entries in pool.values():
        for entry in entries or []:
            source = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source, str) and source.startswith("env:"):
                variable = source[len("env:") :].strip()
                if variable and variable not in names:
                    names.append(variable)
    return names


KEY_ENV_KEYS = ("key_env", "api_key_env")


def _add_fallback_entry_provider(wanted: set[str], entry) -> None:
    if isinstance(entry, str) and entry:
        wanted.add(entry)
    elif isinstance(entry, dict):
        provider = entry.get("provider")
        if isinstance(provider, str) and provider:
            wanted.add(provider)


def _fallback_entries(inherited_config: dict) -> list:
    entries = list(inherited_config.get("fallback_providers") or [])
    fallback_model = inherited_config.get("fallback_model")
    if isinstance(fallback_model, list):
        entries += fallback_model
    elif fallback_model is not None:
        entries.append(fallback_model)
    return entries


def member_providers(inherited_config: dict) -> set[str]:
    wanted = set()
    provider = (inherited_config.get("model") or {}).get("provider")
    if isinstance(provider, str) and provider:
        wanted.add(provider)
    for entry in _fallback_entries(inherited_config):
        _add_fallback_entry_provider(wanted, entry)
    return wanted


def direct_key_env_vars(inherited_config: dict) -> list[str]:
    names: list[str] = []
    candidates = _fallback_entries(inherited_config)
    model = inherited_config.get("model")
    if isinstance(model, dict):
        candidates.append(model)
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        for key in KEY_ENV_KEYS:
            variable = entry.get(key)
            if isinstance(variable, str) and variable.strip():
                name = variable.strip()
                if name not in names:
                    names.append(name)
    return names


def member_auth_payload(auth: dict, inherited_config: dict) -> dict:
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict):
        return dict(auth)
    wanted = member_providers(inherited_config)
    trimmed_pool = {name: entries for name, entries in pool.items() if name in wanted}
    return {**auth, "credential_pool": trimmed_pool}


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_env_file(path: Path) -> dict[str, str]:
    dirfd = secure_fs.open_trusted_dir(path.parent)
    try:
        content = secure_fs.read_regular_file(dirfd, path.name)
    finally:
        os.close(dirfd)
    return parse_env_text(content) if content is not None else {}


def merge_env_file(
    path: Path, additions: dict[str, str], owner_uid: int, owner_gid: int
) -> None:
    dirfd = secure_fs.open_trusted_dir(path.parent)
    try:
        content = secure_fs.read_regular_file(dirfd, path.name)
        kept = [
            raw_line
            for raw_line in (content.splitlines() if content is not None else [])
            if raw_line.strip() and raw_line.split("=", 1)[0].strip() not in additions
        ]
        lines = kept + [f'{key}="{value}"' for key, value in additions.items()]
        secure_fs.write_regular_file(
            dirfd, path.name, "\n".join(lines) + "\n", owner_uid, owner_gid
        )
    finally:
        os.close(dirfd)


def load_source_auth(source_auth_path: Path) -> dict | None:
    if not source_auth_path.is_file():
        return None
    try:
        return json.loads(source_auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: could not read {source_auth_path}: {exc}", file=sys.stderr)
        return None


def copy_provider_secrets(
    source: Path, target: Path, wanted: list[str], owner_uid: int, owner_gid: int
) -> None:
    if not wanted:
        return
    source_env = parse_env_file(source / ".env")
    secrets = {name: source_env[name] for name in wanted if source_env.get(name)}
    if secrets:
        merge_env_file(target / ".env", secrets, owner_uid, owner_gid)
    missing = [name for name in wanted if name not in secrets]
    if missing:
        print(
            "WARNING: the gateway's credentials reference "
            f"{', '.join(missing)}, which are not set in {source / '.env'}. This "
            "member's assistant will fail provider authentication.",
            file=sys.stderr,
        )


def seed_profile(source: Path, target: Path, owner_uid: int, owner_gid: int) -> int:
    source_config = load_yaml(source / CONFIG_FILENAME)
    inherited = {
        key: source_config[key]
        for key in INHERITED_CONFIG_KEYS
        if key in source_config
    }

    target_config_path = target / CONFIG_FILENAME
    target_config = load_yaml_untrusted(target_config_path)
    target_config.update(inherited)
    write_private(
        target_config_path,
        yaml.safe_dump(target_config, sort_keys=False, default_flow_style=False),
        owner_uid,
        owner_gid,
    )

    provider = (inherited.get("model") or {}).get("provider")
    direct_env = direct_key_env_vars(inherited)

    source_auth_path = source / AUTH_FILENAME
    auth = load_source_auth(source_auth_path)
    providers: list[str] = []
    wanted: list[str] = []

    if auth is not None:
        member_auth = member_auth_payload(auth, inherited)
        write_private(
            target / AUTH_FILENAME,
            json.dumps(member_auth, indent=2),
            owner_uid,
            owner_gid,
        )
        providers = credential_providers(member_auth)
        wanted = credential_env_vars(member_auth)

    for name in direct_env:
        if name not in wanted:
            wanted.append(name)

    copy_provider_secrets(source, target, wanted, owner_uid, owner_gid)

    if auth is None and not direct_env:
        print(
            f"WARNING: {source_auth_path} does not exist, so this member has no "
            "provider credentials and their assistant cannot answer yet. "
            "Configure the gateway's own profile first, then re-run.",
            file=sys.stderr,
        )
    elif not providers and not direct_env:
        print(
            "WARNING: the gateway's profile has no stored provider credentials, so "
            "this member's assistant cannot answer yet.",
            file=sys.stderr,
        )
    elif providers and provider and provider not in providers and not direct_env:
        print(
            f"WARNING: this member's model provider is '{provider}' but the only "
            f"stored credentials are for {', '.join(providers)}. Their assistant "
            "will not be able to answer.",
            file=sys.stderr,
        )
    else:
        print(
            f"  Provider:     {provider or 'inherited'} "
            f"({', '.join(providers or direct_env)})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stagewhisper-hermes-seed-profile",
        description="Copy provider settings and credentials from the gateway's own "
        "Hermes profile into a newly provisioned member's profile.",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--owner-uid", type=int, required=True)
    parser.add_argument("--owner-gid", type=int, required=True)
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    target = Path(args.target).expanduser()
    if not source.is_dir():
        print(f"✗ Source profile {source} does not exist.", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"✗ Target profile {target} does not exist.", file=sys.stderr)
        return 1
    if source.resolve() == target.resolve():
        print("✗ Source and target profile are the same.", file=sys.stderr)
        return 1

    return seed_profile(source, target, args.owner_uid, args.owner_gid)


if __name__ == "__main__":
    raise SystemExit(main())
