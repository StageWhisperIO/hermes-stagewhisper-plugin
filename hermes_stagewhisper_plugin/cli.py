"""`stagewhisper-hermes` CLI: pair-code, unpair, status."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys

from . import config

PAIRING_CODE_PREFIX = "stagewhisper-pair:v1:"


def encode_pairing_code(url: str, token: str, label: str) -> str:
    payload = json.dumps(
        {"url": url, "token": token, "label": label}, separators=(",", ":")
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"{PAIRING_CODE_PREFIX}{encoded}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stagewhisper-hermes",
        description="Manage the StageWhisper Hermes platform adapter pairing.",
    )
    subs = parser.add_subparsers(dest="command")

    code_parser = subs.add_parser(
        "pair-code",
        help="Generate a StageWhisper pairing code (no backend required)",
    )
    code_parser.add_argument(
        "--url",
        help="Relay URL StageWhisper should reach this gateway at "
        "(default: http://127.0.0.1:<listen-port>).",
    )
    code_parser.add_argument(
        "--listen-port",
        type=int,
        default=config.DEFAULT_LISTEN_PORT,
        help=f"Loopback port the adapter listens on (default: {config.DEFAULT_LISTEN_PORT}).",
    )
    code_parser.add_argument(
        "--label", "-l", default="Hermes", help="Label shown in StageWhisper."
    )

    subs.add_parser("unpair", help="Remove the StageWhisper pairing")
    subs.add_parser("status", help="Show pairing status")

    args = parser.parse_args(argv)
    config.load_env_file()

    if args.command == "pair-code":
        return _cmd_pair_code(args.url, args.listen_port, args.label)
    if args.command == "unpair":
        return _cmd_unpair()
    if args.command == "status":
        return _cmd_status()

    parser.print_help()
    return 1


def _print_qr(code: str) -> None:
    try:
        import segno
    except ImportError:
        return
    try:
        segno.make(code, error="l").terminal(compact=True, border=2)
    except Exception:
        return
    print()


def _cmd_pair_code(url: str | None, listen_port: int, label: str) -> int:
    if not (1024 <= listen_port <= 65535):
        print(f"✗ Invalid listen port {listen_port} (must be 1024-65535)")
        return 1
    label = (label or "").strip() or "Hermes"

    token = (os.environ.get("STAGEWHISPER_RELAY_TOKEN") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)

    overrides = {"STAGEWHISPER_RELAY_TOKEN": token, "STAGEWHISPER_LISTEN_PORT": str(listen_port)}
    try:
        config.save_pairing(overrides)
    except Exception as exc:
        print(f"✗ Failed to persist relay token: {exc}")
        return 1
    os.environ["STAGEWHISPER_RELAY_TOKEN"] = token
    os.environ["STAGEWHISPER_LISTEN_PORT"] = str(listen_port)

    relay_url = (url or "").strip() or f"http://127.0.0.1:{listen_port}"
    code = encode_pairing_code(relay_url, token, label)

    print("StageWhisper pairing code:")
    print()
    _print_qr(code)
    print(f"  {code}")
    print()
    print("On your phone, open Settings, then Relay, then Scan the code.")
    print("On your Mac, paste the code above.")
    print("Restart the Hermes gateway so the adapter listens: hermes gateway restart")
    if relay_url.startswith("http://127.0.0.1"):
        print(
            "Running on a remote host? Tunnel the port from the machine running StageWhisper:\n"
            f"  ssh -L {listen_port}:127.0.0.1:{listen_port} <this-host>"
        )
    return 0


def _cmd_unpair() -> int:
    try:
        config.save_pairing({})
    except Exception as exc:
        print(f"✗ Unpair failed: could not update config: {exc}")
        return 1
    for key in config.PERSISTED_ENV_VARS:
        os.environ.pop(key, None)
    print("✓ Unpaired — config cleared")
    return 0


def _cmd_status() -> int:
    relay_token = config.get_relay_token()

    if not relay_token:
        print("Relay: not configured")
        print("Run `stagewhisper-hermes pair-code` to generate a pairing code.")
        return 0

    print("Relay: configured")
    listen_port = os.getenv("STAGEWHISPER_LISTEN_PORT", str(config.DEFAULT_LISTEN_PORT))
    print(f"Listen port: {listen_port}")
    print("Device approvals are managed by Hermes: run `hermes pairing list`.")
    print("Adapter lifecycle: managed by the Hermes gateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
