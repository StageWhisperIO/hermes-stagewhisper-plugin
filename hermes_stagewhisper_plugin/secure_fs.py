from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path


WIPE_CHUNK_BYTES = 1 << 16
MAX_WIPE_BYTES = 1 << 20


def open_trusted_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def temp_name_for(name: str) -> str:
    return f"{name}.{secrets.token_hex(8)}.tmp"


def write_regular_file(
    dirfd: int, name: str, text: str, owner_uid: int, owner_gid: int
) -> None:
    tmp_name = temp_name_for(name)
    fd = os.open(
        tmp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dirfd,
    )
    try:
        os.fchown(fd, owner_uid, owner_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except Exception:
        try:
            os.unlink(tmp_name, dir_fd=dirfd)
        except FileNotFoundError:
            pass
        raise


def read_regular_file(dirfd: int, name: str) -> str | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{name} exists but cannot be opened safely: {exc}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{name} exists but is not a regular file")
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def _overwrite_in_chunks(fd: int, size: int) -> None:
    if size <= 0 or size > MAX_WIPE_BYTES:
        return
    zeros = b"\x00" * min(size, WIPE_CHUNK_BYTES)
    offset = 0
    while offset < size:
        offset += os.pwrite(fd, zeros[: min(size - offset, WIPE_CHUNK_BYTES)], offset)
    os.fsync(fd)


def unlink_owned_regular_file(path: Path, owner_uid: int) -> bool:
    dirfd = open_trusted_dir(path.parent)
    try:
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dirfd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError(f"refusing to remove {path}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"{path} exists but is not a regular file; refusing to remove it"
                )
            if info.st_uid != owner_uid:
                raise ValueError(
                    f"{path} is not owned by uid {owner_uid}; refusing to remove it"
                )
            _overwrite_in_chunks(fd, info.st_size)
        finally:
            os.close(fd)
        os.unlink(path.name, dir_fd=dirfd)
        return True
    finally:
        os.close(dirfd)


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_stagewhisper_plugin.secure_fs"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    remove_parser = subparsers.add_parser("remove-owned-file")
    remove_parser.add_argument("path")
    remove_parser.add_argument("--owner-uid", type=int, required=True)

    args = parser.parse_args(argv)

    if args.command == "remove-owned-file":
        try:
            removed = unlink_owned_regular_file(Path(args.path), args.owner_uid)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("removed" if removed else "missing")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
