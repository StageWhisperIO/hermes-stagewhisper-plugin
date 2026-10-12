from __future__ import annotations

import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path


PLUGIN_NAME = "stagewhisper"
DEFAULT_PLUGINS_DIR = Path.home() / ".hermes" / "plugins"
CLI_NAME = "stagewhisper-hermes"


def _cli_command() -> str:
    if shutil.which(CLI_NAME):
        return CLI_NAME
    bundled = Path(sys.executable).parent / CLI_NAME
    return str(bundled) if bundled.exists() else CLI_NAME


def main(argv: list[str] | None = None, *, print_next_steps: bool = True) -> int:
    parser = argparse.ArgumentParser(
        prog="stagewhisper-hermes-install",
        description="Install or remove the StageWhisper Hermes platform adapter shim.",
    )
    parser.add_argument(
        "--plugins-dir",
        default=str(DEFAULT_PLUGINS_DIR),
        help=f"Hermes plugins directory (default: {DEFAULT_PLUGINS_DIR}).",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the StageWhisper plugin shim instead of installing it.",
    )
    args = parser.parse_args(argv)

    plugins_dir = Path(args.plugins_dir).expanduser()
    target_dir = plugins_dir / PLUGIN_NAME

    if args.uninstall:
        return _uninstall(target_dir)
    return _install(target_dir, print_next_steps)


def _install(target_dir: Path, print_next_steps: bool = True) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    templates = resources.files("hermes_stagewhisper_plugin.templates")
    plugin_yaml_text = templates.joinpath("plugin.yaml").read_text(encoding="utf-8")
    init_shim_text = templates.joinpath("adapter_shim.py").read_text(encoding="utf-8")

    (target_dir / "plugin.yaml").write_text(plugin_yaml_text, encoding="utf-8")
    (target_dir / "__init__.py").write_text(init_shim_text, encoding="utf-8")

    print(f"Installed StageWhisper plugin shim into {target_dir}")
    if print_next_steps:
        print("Next, set up a member and get their pairing code:")
        print(f"  sudo {_cli_command()} provision <name>")
    return 0


def _uninstall(target_dir: Path) -> int:
    if not target_dir.exists():
        print(f"Nothing to remove at {target_dir}")
        return 0
    if target_dir.is_symlink() or target_dir.is_file():
        target_dir.unlink()
    else:
        shutil.rmtree(target_dir)
    print(f"Removed {target_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
