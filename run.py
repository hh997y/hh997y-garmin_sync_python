from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_config_path(repo_root: Path) -> Path:
    config_path = Path.cwd() / "config.yaml"
    if config_path.exists():
        return config_path

    exe_path = Path(sys.executable).resolve()
    if exe_path.name != "python" and ".app" in exe_path.as_posix():
        app_dir = exe_path.parent.parent.parent
        external_config = app_dir.parent / "config.yaml"
        if external_config.exists():
            return external_config

    return repo_root / "config.yaml"


def _configure_playwright_browsers(repo_root: Path) -> None:
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    bundle_root = Path(getattr(sys, "_MEIPASS", repo_root))
    bundled_browsers = bundle_root / "playwright-browsers"
    if not bundled_browsers.exists():
        exe_parent = Path(sys.executable).resolve().parent
        app_resources = exe_parent.parent / "Resources"
        bundled_browsers = app_resources / "playwright-browsers"
    if bundled_browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled_browsers)


def main() -> None:
    repo_root = Path(__file__).resolve().parent
    _configure_playwright_browsers(repo_root)
    config_path = _resolve_config_path(repo_root)
    sys.path.insert(0, str(repo_root / "src"))

    from garmin_sync.main import main as cli_main

    cli_main(str(config_path))


if __name__ == "__main__":
    main()
