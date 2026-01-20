from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(repo_root / "src"))

    from garmin_sync.main import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
