#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt
pip install -r scripts/requirements-playwright.txt
pip install pyinstaller

# Install Chromium into a project-local folder so it can be bundled.
PLAYWRIGHT_BROWSERS_PATH="$repo_root/playwright-browsers" python -m playwright install chromium

# Build macOS .app bundle.
pyinstaller --clean --noconfirm --windowed --name GarminSync --paths src run.py

# Copy Playwright browsers into app resources.
app_resources="dist/GarminSync.app/Contents/Resources"
mkdir -p "$app_resources"
rm -rf "$app_resources/playwright-browsers"
cp -R "playwright-browsers" "$app_resources/playwright-browsers"

# Copy config.yaml next to the .app for easy editing.
cp -f "config.yaml" "dist/config.yaml"
