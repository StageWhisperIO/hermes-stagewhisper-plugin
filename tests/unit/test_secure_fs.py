from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_stagewhisper_plugin import secure_fs


def test_write_regular_file_refuses_to_follow_a_symlink_planted_at_the_temp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("root-owned-secret", encoding="utf-8")
    original_mtime_ns = sentinel.stat().st_mtime_ns

    monkeypatch.setattr(
        secure_fs, "temp_name_for", lambda name: name + ".predictable.tmp"
    )
    (member_dir / "config.yaml.predictable.tmp").symlink_to(sentinel)

    dirfd = secure_fs.open_trusted_dir(member_dir)
    try:
        with pytest.raises(OSError):
            secure_fs.write_regular_file(
                dirfd, "config.yaml", "member data", os.getuid(), os.getgid()
            )
    finally:
        os.close(dirfd)

    assert sentinel.read_text(encoding="utf-8") == "root-owned-secret"
    assert sentinel.stat().st_mtime_ns == original_mtime_ns


def test_write_regular_file_refuses_to_follow_a_symlink_planted_at_the_final_target_path(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("root-owned-secret", encoding="utf-8")
    original_mtime_ns = sentinel.stat().st_mtime_ns

    (member_dir / "config.yaml").symlink_to(sentinel)

    dirfd = secure_fs.open_trusted_dir(member_dir)
    try:
        secure_fs.write_regular_file(
            dirfd, "config.yaml", "member data", os.getuid(), os.getgid()
        )
    finally:
        os.close(dirfd)

    assert sentinel.read_text(encoding="utf-8") == "root-owned-secret"
    assert sentinel.stat().st_mtime_ns == original_mtime_ns
    assert not (member_dir / "config.yaml").is_symlink()
    assert (member_dir / "config.yaml").read_text(encoding="utf-8") == "member data"


def test_unlink_owned_regular_file_refuses_to_remove_a_symlinked_file_and_leaves_the_target_intact(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("root-owned-secret", encoding="utf-8")
    original_mtime_ns = sentinel.stat().st_mtime_ns

    env_path = member_dir / ".env"
    env_path.symlink_to(sentinel)

    with pytest.raises(ValueError):
        secure_fs.unlink_owned_regular_file(env_path, os.getuid())

    assert sentinel.read_text(encoding="utf-8") == "root-owned-secret"
    assert sentinel.stat().st_mtime_ns == original_mtime_ns
    assert env_path.is_symlink()


def test_unlink_owned_regular_file_refuses_to_remove_a_file_owned_by_someone_else(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    env_path = member_dir / ".env"
    env_path.write_text("STAGEWHISPER_RELAY_TOKEN=x\n", encoding="utf-8")

    with pytest.raises(ValueError):
        secure_fs.unlink_owned_regular_file(env_path, os.getuid() + 1)

    assert env_path.is_file()


def test_unlink_owned_regular_file_reports_a_missing_file_without_raising(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()

    removed = secure_fs.unlink_owned_regular_file(member_dir / ".env", os.getuid())

    assert removed is False


def test_unlink_owned_regular_file_removes_a_verified_owned_regular_file(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    env_path = member_dir / ".env"
    env_path.write_text("STAGEWHISPER_RELAY_TOKEN=x\n", encoding="utf-8")

    removed = secure_fs.unlink_owned_regular_file(env_path, os.getuid())

    assert removed is True
    assert not env_path.exists()


def test_unlink_owned_regular_file_removes_a_large_sparse_file_without_allocating_its_size(
    tmp_path: Path,
) -> None:
    member_dir = tmp_path / "member"
    member_dir.mkdir()
    env_path = member_dir / ".env"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.truncate(fd, 4 * 1024 * 1024 * 1024)
    finally:
        os.close(fd)

    removed = secure_fs.unlink_owned_regular_file(env_path, os.getuid())

    assert removed is True
    assert not env_path.exists()


def test_a_real_token_file_is_zeroed_before_it_is_unlinked(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('STAGEWHISPER_RELAY_TOKEN="super-secret"\n', encoding="utf-8")
    size = env_path.stat().st_size
    fd = os.open(env_path, os.O_RDONLY)
    try:
        assert b"super-secret" in os.pread(fd, size, 0)
    finally:
        os.close(fd)

    observed: list[int] = []
    real_pwrite = os.pwrite

    def recording_pwrite(handle, data, offset):
        observed.append(len(data))
        return real_pwrite(handle, data, offset)

    os.pwrite = recording_pwrite
    try:
        assert secure_fs.unlink_owned_regular_file(env_path, os.getuid()) is True
    finally:
        os.pwrite = real_pwrite

    assert sum(observed) == size
    assert not env_path.exists()


def test_an_oversized_token_file_is_removed_without_being_written_to(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    with env_path.open("wb") as handle:
        handle.truncate(secure_fs.MAX_WIPE_BYTES + 1)

    observed: list[int] = []
    real_pwrite = os.pwrite

    def recording_pwrite(handle, data, offset):
        observed.append(len(data))
        return real_pwrite(handle, data, offset)

    os.pwrite = recording_pwrite
    try:
        assert secure_fs.unlink_owned_regular_file(env_path, os.getuid()) is True
    finally:
        os.pwrite = real_pwrite

    assert observed == []
    assert not env_path.exists()
