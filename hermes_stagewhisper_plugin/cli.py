from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .ops_runner import PROVISION_SCRIPT, REVOKE_SCRIPT, run_ops_script

PAIRING_CODE_PREFIX = "stagewhisper-pair:v1:"
PAIRING_LINK_PREFIX = "stagewhisper://pair?v=1&code="


def encode_pairing_code(url: str, token: str, label: str) -> str:
    payload = json.dumps(
        {"url": url, "token": token, "label": label}, separators=(",", ":")
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"{PAIRING_CODE_PREFIX}{encoded}"


def pairing_link(code: str) -> str:
    return f"{PAIRING_LINK_PREFIX}{code.removeprefix(PAIRING_CODE_PREFIX)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stagewhisper-hermes",
        description="Show and manage this Hermes profile's StageWhisper pairing.",
    )
    subs = parser.add_subparsers(dest="command")

    code_parser = subs.add_parser(
        "pair-code",
        help="Render this profile's StageWhisper pairing code and QR",
    )
    code_parser.add_argument(
        "--url",
        help="Relay URL StageWhisper should reach this gateway at "
        "(default: http://127.0.0.1:<listen-port>).",
    )
    code_parser.add_argument(
        "--label", "-l", default="Hermes", help="Label shown in StageWhisper."
    )

    subs.add_parser("unpair", help="Remove the StageWhisper pairing")
    subs.add_parser("status", help="Show pairing status for this profile")

    provision_parser = subs.add_parser(
        "provision",
        help="Set up a new member on this host and print their pairing code (run as root)",
    )
    provision_parser.add_argument("label", help="Member identifier, for example alice")
    provision_parser.add_argument(
        "--relay-host",
        help="Host the member's own tunnel exposes their gateway on "
        "(default: 127.0.0.1, reachable only from this host).",
    )
    provision_parser.add_argument(
        "--url", help="Full relay URL override, takes precedence over --relay-host."
    )

    revoke_parser = subs.add_parser(
        "revoke", help="Shut down a member's gateway and revoke their access (run as root)"
    )
    revoke_parser.add_argument("label", help="Member identifier to revoke")

    args = parser.parse_args(argv)

    if args.command == "provision":
        return _cmd_provision(args.label, args.relay_host, args.url)
    if args.command == "revoke":
        return run_ops_script(REVOKE_SCRIPT, [args.label])

    config.load_env_file()

    if args.command == "pair-code":
        return _cmd_pair_code(args.url, args.label)
    if args.command == "unpair":
        return _cmd_unpair()
    if args.command == "status":
        return _cmd_status()

    parser.print_help()
    return 1


def _cmd_provision(label: str, relay_host: str | None, url: str | None) -> int:
    script_args = [label]
    if relay_host:
        script_args += ["--relay-host", relay_host]
    if url:
        script_args += ["--url", url]
    return run_ops_script(PROVISION_SCRIPT, script_args)


def _print_qr(code: str) -> bool:
    try:
        import segno
    except ImportError:
        return False
    try:
        segno.make(code, error="l").terminal(compact=True, border=2)
    except Exception:
        return False
    print()
    return True


def _profile_home() -> str:
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    return hermes_home or str(Path.home() / ".hermes")


def _ingress_host_for(relay_url: str) -> str:
    host = (urlsplit(relay_url).hostname or "").strip().lower()
    return "" if host in config.LOOPBACK_HOSTS else host


def _cmd_pair_code(url: str | None, label: str) -> int:
    label = (label or "").strip() or "Hermes"
    listen_port = os.getenv("STAGEWHISPER_LISTEN_PORT", str(config.DEFAULT_LISTEN_PORT))
    relay_url = (url or "").strip() or f"http://127.0.0.1:{listen_port}"
    ingress_host = _ingress_host_for(relay_url)

    token = config.get_relay_token()
    minted = not token
    if minted:
        token = config.mint_relay_token()

    allowed_hosts = os.getenv("STAGEWHISPER_ALLOW_INGRESS_HOSTS", "")
    if ingress_host and ingress_host not in allowed_hosts.split(","):
        allowed_hosts = f"{allowed_hosts},{ingress_host}".strip(",")

    persisted = minted or allowed_hosts != os.getenv("STAGEWHISPER_ALLOW_INGRESS_HOSTS", "")
    if persisted:
        config.save_pairing(
            {
                "STAGEWHISPER_RELAY_TOKEN": token,
                "STAGEWHISPER_LISTEN_PORT": listen_port,
                "STAGEWHISPER_ALLOW_INGRESS_HOSTS": allowed_hosts,
            }
        )

    if minted:
        print(f"Created a relay token for this profile ({_profile_home()}).")
        print()

    if persisted:
        print(
            "Restart your Hermes gateway before you pair. Until you do, it "
            "keeps running with the old settings and StageWhisper will not "
            "be able to reach it."
        )
        print()

    code = encode_pairing_code(relay_url, token, label)

    print("On your phone, open StageWhisper and scan this:")
    print()
    if not _print_qr(code):
        print("  (QR rendering is unavailable here, use the code below instead.)")
        print()
    print("On your computer, open this link:")
    print()
    print(f"  {pairing_link(code)}")
    print()
    print("Or paste this code into Settings, then Assistant:")
    print()
    print(f"  {code}")
    print()
    if relay_url.startswith("http://127.0.0.1"):
        print(
            "This address only works on this machine. To reach it from another "
            "device, put the port on your tailnet:\n"
            f"  tailscale serve --bg --https {listen_port} http://127.0.0.1:{listen_port}\n"
            "then run this again with --url https://<name>.your-tailnet.ts.net:"
            f"{listen_port}"
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
    profile_home = _profile_home()

    if not relay_token:
        print("Relay: not configured")
        print(f"Profile home: {profile_home}")
        print("Run 'stagewhisper-hermes pair-code' to set this profile up,")
        print("or ask an operator to provision it for you.")
        return 0

    listen_port = os.getenv("STAGEWHISPER_LISTEN_PORT", str(config.DEFAULT_LISTEN_PORT))
    print("Relay: configured")
    print(f"Profile home: {profile_home}")
    print(f"Listen port: {listen_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
