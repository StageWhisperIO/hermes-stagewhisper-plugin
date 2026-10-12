from __future__ import annotations

from pathlib import Path

from hermes_stagewhisper_plugin.install import PLUGIN_NAME, main

REQUIRED_MANIFEST_KEYS = ("name: stagewhisper", "kind: platform", "version:")
REQUIRED_ENV_KEYS = ("STAGEWHISPER_RELAY_TOKEN", "STAGEWHISPER_LISTEN_PORT")


def _installed_manifest(plugins_dir: Path) -> Path:
    return plugins_dir / PLUGIN_NAME / "plugin.yaml"


def test_install_writes_non_empty_manifest_with_required_keys(tmp_path: Path) -> None:
    rc = main(["--plugins-dir", str(tmp_path)])
    assert rc == 0

    manifest = _installed_manifest(tmp_path)
    assert manifest.exists()
    text = manifest.read_text(encoding="utf-8")
    assert text.strip(), "installed manifest must not be empty"
    for key in REQUIRED_MANIFEST_KEYS:
        assert key in text, f"manifest missing required key: {key}"
    for env in REQUIRED_ENV_KEYS:
        assert env in text, f"manifest missing required env var: {env}"


def test_install_writes_non_empty_init_shim(tmp_path: Path) -> None:
    rc = main(["--plugins-dir", str(tmp_path)])
    assert rc == 0

    shim = tmp_path / PLUGIN_NAME / "__init__.py"
    assert shim.exists()
    assert shim.read_text(encoding="utf-8").strip(), "installed __init__ shim must not be empty"


def test_uninstall_removes_plugin_dir(tmp_path: Path) -> None:
    assert main(["--plugins-dir", str(tmp_path)]) == 0
    assert (tmp_path / PLUGIN_NAME).exists()

    assert main(["--plugins-dir", str(tmp_path), "--uninstall"]) == 0
    assert not (tmp_path / PLUGIN_NAME).exists()

    assert main(["--plugins-dir", str(tmp_path), "--uninstall"]) == 0
