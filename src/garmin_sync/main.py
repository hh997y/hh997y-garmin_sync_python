from __future__ import annotations

from .config import load_config
from .sync import sync_activities


def main() -> None:
    config = load_config("config.yaml")
    sync_activities(config, limit=None, dry_run=None, verbose=config.sync.verbose)


if __name__ == "__main__":
    main()
