from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_stagewhisper_plugin import profile_seed

OPERATOR_SECRETS = {
    "secrets": {"vault_token": "operator-only"},
    "telegram": {"bot_token": "operator-telegram-token"},
    "slack": {"app_token": "operator-slack-token"},
    "memory": {"enabled": True},
    "sessions": {"history": "operator-history"},
}


def build_profiles(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "operator"
    target = tmp_path / "member"
    (source).mkdir()
    (target).mkdir()

    source_config = {
        "model": {
            "provider": "deepseek",
            "default": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1",
        },
        "fallback_providers": [],
        "model_catalog": {"deepseek-v4-pro": {"context": 128000}},
        **OPERATOR_SECRETS,
    }
    (source / "config.yaml").write_text(yaml.safe_dump(source_config), encoding="utf-8")
    (source / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": None,
                "credential_pool": {
                    "deepseek": [
                        {
                            "source": "env:DEEPSEEK_API_KEY",
                            "auth_type": "api_key",
                            "secret_fingerprint": "abc123",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (source / ".env").write_text(
        'DEEPSEEK_API_KEY="org-secret-key"\n'
        'STAGEWHISPER_RELAY_TOKEN="operator-token"\n'
        'TELEGRAM_BOT_TOKEN="operator-telegram"\n',
        encoding="utf-8",
    )
    (target / ".env").write_text(
        'STAGEWHISPER_RELAY_TOKEN="alice-token"\n'
        'STAGEWHISPER_LISTEN_PORT="9664"\n',
        encoding="utf-8",
    )
    (target / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["stagewhisper"]}}), encoding="utf-8"
    )
    return source, target


def seed(tmp_path: Path) -> Path:
    source, target = build_profiles(tmp_path)
    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())
    return target


def test_a_member_inherits_the_gateways_model_provider(tmp_path: Path) -> None:
    target = seed(tmp_path)

    config = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))

    assert config["model"]["provider"] == "deepseek"
    assert config["model"]["base_url"] == "https://api.deepseek.com/v1"
    assert config["model_catalog"] == {"deepseek-v4-pro": {"context": 128000}}


def test_a_member_inherits_the_provider_credentials_so_their_assistant_can_answer(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))

    entry = auth["credential_pool"]["deepseek"][0]

    assert entry["source"] == "env:DEEPSEEK_API_KEY"
    assert entry["auth_type"] == "api_key"


def test_the_operators_personal_settings_never_reach_the_member(tmp_path: Path) -> None:
    target = seed(tmp_path)

    config = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))

    for leaked_key in OPERATOR_SECRETS:
        assert leaked_key not in config, (
            f"'{leaked_key}' belongs to the operator and must never be copied into a "
            "member's profile"
        )


def test_seeding_keeps_the_stagewhisper_plugin_enabled_for_the_member(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    config = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))

    assert config["plugins"]["enabled"] == ["stagewhisper"]


def test_the_members_credential_file_is_not_readable_by_other_accounts(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    for name in ("config.yaml", "auth.json"):
        mode = (target / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} should be 0600 but is {oct(mode)}"


def test_an_operator_profile_without_credentials_is_reported_not_silently_ignored(
    tmp_path: Path, capsys
) -> None:
    source, target = build_profiles(tmp_path)
    (source / "auth.json").unlink()

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert "cannot answer yet" in capsys.readouterr().err


def test_the_member_inherits_the_provider_secret_the_credentials_point_at(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    env = profile_seed.parse_env_file(target / ".env")

    assert env["DEEPSEEK_API_KEY"] == "org-secret-key"


def test_inheriting_the_secret_does_not_clobber_the_members_own_relay_token(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    env = profile_seed.parse_env_file(target / ".env")

    assert env["STAGEWHISPER_RELAY_TOKEN"] == "alice-token"
    assert env["STAGEWHISPER_LISTEN_PORT"] == "9664"


def test_unrelated_operator_secrets_are_not_copied_into_the_member_env(
    tmp_path: Path,
) -> None:
    target = seed(tmp_path)

    assert "TELEGRAM_BOT_TOKEN" not in profile_seed.parse_env_file(target / ".env")


def test_a_missing_provider_secret_is_reported_rather_than_failing_silently(
    tmp_path: Path, capsys
) -> None:
    source, target = build_profiles(tmp_path)
    (source / ".env").write_text('STAGEWHISPER_RELAY_TOKEN="x"\n', encoding="utf-8")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_seeding_a_profile_whose_env_is_a_symlink_does_not_write_through_it(
    tmp_path: Path,
) -> None:
    source, target = build_profiles(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("root-owned-secret", encoding="utf-8")
    original_mtime_ns = sentinel.stat().st_mtime_ns
    (target / ".env").unlink()
    (target / ".env").symlink_to(sentinel)

    with pytest.raises(ValueError):
        profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert sentinel.read_text(encoding="utf-8") == "root-owned-secret"
    assert sentinel.stat().st_mtime_ns == original_mtime_ns
    assert (target / ".env").is_symlink()


def build_multi_provider_profiles(
    tmp_path: Path, fallback_providers: list[str] | None = None
) -> tuple[Path, Path]:
    source = tmp_path / "operator"
    target = tmp_path / "member"
    source.mkdir()
    target.mkdir()

    source_config = {
        "model": {"provider": "deepseek", "default": "deepseek-v4-pro"},
        "fallback_providers": fallback_providers or [],
        "model_catalog": {},
    }
    (source / "config.yaml").write_text(yaml.safe_dump(source_config), encoding="utf-8")
    (source / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "deepseek",
                "credential_pool": {
                    "deepseek": [
                        {"source": "env:DEEPSEEK_API_KEY", "auth_type": "api_key"}
                    ],
                    "openai": [
                        {"source": "env:OPENAI_API_KEY", "auth_type": "api_key"}
                    ],
                    "anthropic": [
                        {"source": "env:ANTHROPIC_API_KEY", "auth_type": "api_key"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (source / ".env").write_text(
        'DEEPSEEK_API_KEY="deepseek-secret"\n'
        'OPENAI_API_KEY="openai-secret"\n'
        'ANTHROPIC_API_KEY="anthropic-secret"\n',
        encoding="utf-8",
    )
    (target / ".env").write_text(
        'STAGEWHISPER_RELAY_TOKEN="alice-token"\n', encoding="utf-8"
    )
    (target / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    return source, target


def test_a_member_gets_only_the_configured_providers_credentials(
    tmp_path: Path,
) -> None:
    source, target = build_multi_provider_profiles(tmp_path)

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert set(auth["credential_pool"]) == {"deepseek"}

    env = profile_seed.parse_env_file(target / ".env")
    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_fallback_providers_are_also_retained_for_the_member(
    tmp_path: Path,
) -> None:
    source, target = build_multi_provider_profiles(
        tmp_path, fallback_providers=["anthropic"]
    )

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert set(auth["credential_pool"]) == {"deepseek", "anthropic"}

    env = profile_seed.parse_env_file(target / ".env")
    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert "OPENAI_API_KEY" not in env


def build_fallback_profiles(
    tmp_path: Path,
    fallback_providers: object = None,
    fallback_model: object = None,
) -> tuple[Path, Path]:
    source = tmp_path / "operator"
    target = tmp_path / "member"
    source.mkdir()
    target.mkdir()

    source_config = {"model": {"provider": "deepseek", "default": "deepseek-v4-pro"}}
    if fallback_providers is not None:
        source_config["fallback_providers"] = fallback_providers
    if fallback_model is not None:
        source_config["fallback_model"] = fallback_model
    (source / "config.yaml").write_text(yaml.safe_dump(source_config), encoding="utf-8")
    (source / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "deepseek",
                "credential_pool": {
                    "deepseek": [
                        {"source": "env:DEEPSEEK_API_KEY", "auth_type": "api_key"}
                    ],
                    "openai": [
                        {"source": "env:OPENAI_API_KEY", "auth_type": "api_key"}
                    ],
                    "anthropic": [
                        {"source": "env:ANTHROPIC_API_KEY", "auth_type": "api_key"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (source / ".env").write_text(
        'DEEPSEEK_API_KEY="deepseek-secret"\n'
        'OPENAI_API_KEY="openai-secret"\n'
        'ANTHROPIC_API_KEY="anthropic-secret"\n',
        encoding="utf-8",
    )
    (target / ".env").write_text(
        'STAGEWHISPER_RELAY_TOKEN="alice-token"\n', encoding="utf-8"
    )
    (target / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    return source, target


def test_a_member_inherits_the_credentials_for_a_dict_shaped_fallback_provider(
    tmp_path: Path,
) -> None:
    source, target = build_fallback_profiles(
        tmp_path,
        fallback_providers=[{"provider": "anthropic", "model": "claude-sonnet"}],
    )

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert set(auth["credential_pool"]) == {"deepseek", "anthropic"}

    env = profile_seed.parse_env_file(target / ".env")
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"


def test_a_member_inherits_the_credentials_for_a_legacy_fallback_model_chain(
    tmp_path: Path,
) -> None:
    source, target = build_fallback_profiles(
        tmp_path,
        fallback_model=[
            {"provider": "anthropic", "model": "claude-sonnet"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
    )

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    config_out = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert config_out["fallback_model"] == [
        {"provider": "anthropic", "model": "claude-sonnet"},
        {"provider": "openai", "model": "gpt-4o"},
    ]

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert set(auth["credential_pool"]) == {"deepseek", "anthropic", "openai"}

    env = profile_seed.parse_env_file(target / ".env")
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert env["OPENAI_API_KEY"] == "openai-secret"


def test_a_member_inherits_the_credentials_for_a_single_dict_legacy_fallback_model(
    tmp_path: Path,
) -> None:
    source, target = build_fallback_profiles(
        tmp_path,
        fallback_model={"provider": "anthropic", "model": "claude-sonnet"},
    )

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert set(auth["credential_pool"]) == {"deepseek", "anthropic"}

    env = profile_seed.parse_env_file(target / ".env")
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"


def test_a_provider_that_is_neither_configured_nor_a_fallback_is_still_excluded(
    tmp_path: Path,
) -> None:
    source, target = build_fallback_profiles(
        tmp_path,
        fallback_providers=[{"provider": "anthropic", "model": "claude-sonnet"}],
    )

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert "openai" not in auth["credential_pool"]

    env = profile_seed.parse_env_file(target / ".env")
    assert "OPENAI_API_KEY" not in env


def test_seeding_a_profile_whose_config_is_a_symlink_does_not_read_through_it(
    tmp_path: Path,
) -> None:
    source, target = build_profiles(tmp_path)
    operator_only = tmp_path / "operator-only.yaml"
    operator_only.write_text("operator_secret: do-not-leak\n", encoding="utf-8")

    config_path = target / "config.yaml"
    config_path.unlink()
    config_path.symlink_to(operator_only)

    with pytest.raises(ValueError):
        profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert operator_only.read_text(encoding="utf-8") == "operator_secret: do-not-leak\n"


def build_key_env_profiles(tmp_path: Path, fallback_key: str) -> tuple[Path, Path]:
    source, target = build_profiles(tmp_path)
    config = yaml.safe_load((source / "config.yaml").read_text(encoding="utf-8"))
    config["fallback_providers"] = [
        {"provider": "groq", "model": "llama-3.3", fallback_key: "GROQ_FALLBACK_KEY"}
    ]
    (source / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (source / ".env").write_text(
        'DEEPSEEK_API_KEY="org-secret-key"\n'
        'GROQ_FALLBACK_KEY="groq-secret"\n'
        'UNRELATED_KEY="do-not-copy"\n',
        encoding="utf-8",
    )
    return source, target


def test_a_fallback_declaring_its_key_env_gets_that_secret_copied(
    tmp_path: Path,
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert profile_seed.parse_env_file(target / ".env")["GROQ_FALLBACK_KEY"] == (
        "groq-secret"
    )


def test_the_api_key_env_alias_is_honoured_the_same_way(tmp_path: Path) -> None:
    source, target = build_key_env_profiles(tmp_path, "api_key_env")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert profile_seed.parse_env_file(target / ".env")["GROQ_FALLBACK_KEY"] == (
        "groq-secret"
    )


def test_a_legacy_fallback_model_declaring_key_env_also_gets_its_secret(
    tmp_path: Path,
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")
    config = yaml.safe_load((source / "config.yaml").read_text(encoding="utf-8"))
    config.pop("fallback_providers")
    config["fallback_model"] = {
        "provider": "groq",
        "model": "llama-3.3",
        "key_env": "GROQ_FALLBACK_KEY",
    }
    (source / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert profile_seed.parse_env_file(target / ".env")["GROQ_FALLBACK_KEY"] == (
        "groq-secret"
    )


def test_a_key_env_secret_is_copied_even_when_the_gateway_has_no_auth_file(
    tmp_path: Path,
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")
    (source / "auth.json").unlink()

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert profile_seed.parse_env_file(target / ".env")["GROQ_FALLBACK_KEY"] == (
        "groq-secret"
    )


def test_a_key_env_secret_is_copied_even_when_the_gateway_auth_file_is_corrupt(
    tmp_path: Path,
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")
    (source / "auth.json").write_text("{ not json", encoding="utf-8")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert profile_seed.parse_env_file(target / ".env")["GROQ_FALLBACK_KEY"] == (
        "groq-secret"
    )


def test_a_member_with_only_key_env_credentials_is_not_warned_they_cannot_answer(
    tmp_path: Path, capsys
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")
    (source / "auth.json").unlink()

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert "cannot answer yet" not in capsys.readouterr().err


def test_copying_a_fallback_secret_does_not_drag_in_unrelated_operator_secrets(
    tmp_path: Path,
) -> None:
    source, target = build_key_env_profiles(tmp_path, "key_env")

    profile_seed.seed_profile(source, target, os.getuid(), os.getgid())

    assert "UNRELATED_KEY" not in profile_seed.parse_env_file(target / ".env")
