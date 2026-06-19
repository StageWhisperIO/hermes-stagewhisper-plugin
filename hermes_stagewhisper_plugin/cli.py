"""`stagewhisper-hermes` CLI: pair, unpair, status."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from typing import Any

import aiohttp

from . import config

_RELAY_API_PREFIX = "/api/v1/assistant-relay"
_PAIR_TIMEOUT_S = 30.0
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

    pair_parser = subs.add_parser("pair", help="Pair this host with StageWhisper")
    pair_parser.add_argument(
        "--code", "-c", required=True, help="Pairing code from StageWhisper"
    )
    pair_parser.add_argument(
        "--label", "-l", default="StageWhisper", help="Label for this pairing"
    )
    pair_parser.add_argument("--api-url", help="StageWhisper backend URL override")
    pair_parser.add_argument(
        "--listen-port",
        type=int,
        default=config.DEFAULT_LISTEN_PORT,
        help=f"Loopback port the adapter listens on (default: {config.DEFAULT_LISTEN_PORT}).",
    )
    pair_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart the Hermes gateway after pairing.",
    )

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

    if args.command == "pair":
        return _cmd_pair(
            args.code,
            args.label,
            args.api_url,
            args.listen_port,
            restart_gateway=not args.no_restart,
        )
    if args.command == "pair-code":
        return _cmd_pair_code(args.url, args.listen_port, args.label)
    if args.command == "unpair":
        return _cmd_unpair()
    if args.command == "status":
        return _cmd_status()

    parser.print_help()
    return 1


def _cmd_pair_code(url: str | None, listen_port: int, label: str) -> int:
    if not (1024 <= listen_port <= 65535):
        print(f"✗ Invalid listen port {listen_port} (must be 1024-65535)")
        return 1
    label = (label or "").strip() or "Hermes"

    token = (os.environ.get("STAGEWHISPER_RELAY_TOKEN") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)

    overrides: dict[str, str] = {"STAGEWHISPER_RELAY_TOKEN": token, "STAGEWHISPER_LISTEN_PORT": str(listen_port)}
    for key in ("STAGEWHISPER_API_URL", "STAGEWHISPER_INTEGRATION_ID"):
        existing = os.environ.get(key)
        if existing:
            overrides[key] = existing
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
    print(f"  {code}")
    print()
    print("Paste it into StageWhisper under Settings → Connection.")
    print("Restart the Hermes gateway so the adapter listens: hermes gateway restart")
    if relay_url.startswith("http://127.0.0.1"):
        print(
            "Running on a remote host? Tunnel the port from the machine running StageWhisper:\n"
            f"  ssh -L {listen_port}:127.0.0.1:{listen_port} <this-host>"
        )
    return 0


def _cmd_pair(
    code: str,
    label: str,
    api_url: str | None,
    listen_port: int,
    *,
    restart_gateway: bool,
) -> int:
    code = (code or "").strip()
    if len(code) < 4:
        print("✗ Invalid pairing code")
        return 1
    if not (1024 <= listen_port <= 65535):
        print(f"✗ Invalid listen port {listen_port} (must be 1024-65535)")
        return 1
    label = (label or "").strip() or "StageWhisper"
    resolved_api_url = (
        api_url.strip()
        if api_url and api_url.strip()
        else os.environ.get("STAGEWHISPER_API_URL") or "http://127.0.0.1:8000"
    ).rstrip("/")

    try:
        result = asyncio.run(_complete_pairing(resolved_api_url, code, label))
    except Exception as exc:
        print(f"✗ Pairing failed: {exc}")
        return 1

    overrides = {
        "STAGEWHISPER_API_URL": resolved_api_url,
        "STAGEWHISPER_INTEGRATION_ID": str(result["integration_id"]),
        "STAGEWHISPER_RELAY_TOKEN": result["relay_token"],
        "STAGEWHISPER_LISTEN_PORT": str(listen_port),
    }
    try:
        config.save_pairing(overrides)
    except Exception as exc:
        print(f"✗ Paired remotely but failed to persist config: {exc}")
        return 1
    for key, value in overrides.items():
        os.environ[key] = value

    print(f"✓ Paired — integration: {result['integration_id']}")
    if restart_gateway:
        _restart_gateway()
    else:
        print("  Restart the Hermes gateway to load the adapter: hermes gateway restart")
    return 0


async def _complete_pairing(api_url: str, code: str, label: str) -> dict[str, Any]:
    url = f"{api_url}{_RELAY_API_PREFIX}/pair/complete"
    body = {"pairing_code": code, "host_label": label}
    timeout = aiohttp.ClientTimeout(total=_PAIR_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"pairing failed ({resp.status}): {text[:200]}")
            return await resp.json()


def _restart_gateway() -> None:
    hermes = shutil.which("hermes")
    if not hermes:
        print("  'hermes' not found — restart the Hermes gateway manually.")
        return
    print("  Restarting Hermes gateway...")
    result = subprocess.run(
        [hermes, "gateway", "restart"], text=True, capture_output=True
    )
    detail = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        print(_indent(detail) if detail else "  Hermes gateway restarted")
        return
    print("  Hermes gateway restart failed — run manually: hermes gateway restart")
    if detail:
        print(_indent(detail))


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


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
    backend_paired = config.is_paired()

    if not relay_token and not backend_paired:
        print("Relay: not configured")
        print("Run `stagewhisper-hermes pair --code <CODE>` or pair from StageWhisper.")
        return 0

    print("Relay: configured")
    listen_port = os.getenv("STAGEWHISPER_LISTEN_PORT", str(config.DEFAULT_LISTEN_PORT))
    print(f"Listen port: {listen_port}")
    if backend_paired:
        print("Mode: backend (Signals)")
        print(f"Integration: {config.get_integration_id()}")
        print(f"Backend: {config.get_api_url()}")
    else:
        print("Mode: relay-only (bring-your-own-AI, no backend)")
    print("Device approvals are managed by Hermes: run `hermes pairing list`.")
    print("Adapter lifecycle: managed by the Hermes gateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
