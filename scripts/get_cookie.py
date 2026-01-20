from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def _load_config(path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")
    return raw


def _build_signin_url(base_url: str, client_id: str, service_url: str, locale: str) -> str:
    query = urlencode({"clientId": client_id, "service": service_url})
    return f"{base_url}/portal/sso/{locale}/sign-in?{query}"


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    parts = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


_USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[id*="username" i]',
    'input[id*="email" i]',
]

_PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="current-password"]',
    'input[id*="password" i]',
]

_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Sign In")',
    'button:has-text("Log In")',
    'button:has-text("登录")',
    'button:has-text("登入")',
    'input[type="submit"]',
]


def _fill_first(page, selectors: list[str], value: str) -> None:
    locator = _find_locator(page, selectors)
    if not locator:
        raise RuntimeError(f"Could not find input for selectors: {selectors}")
    locator.first.fill(value)


def _click_first(page, selectors: list[str]) -> None:
    locator = _find_locator(page, selectors)
    if not locator:
        raise RuntimeError(f"Could not find submit for selectors: {selectors}")
    locator.first.click()


def _find_locator(page, selectors: list[str]):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count():
                return locator
            for frame in page.frames:
                frame_locator = frame.locator(selector)
                if frame_locator.count():
                    return frame_locator
        time.sleep(0.5)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Login via Garmin SSO and print cookie header.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--region", default="china", help="Region key in config (china/global)")
    parser.add_argument("--output", default=None, help="Optional output file for cookie header")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="Write cookie into config and set auth.type=session_cookie",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    raw = _load_config(config_path)
    region = raw.get(args.region)
    if not isinstance(region, dict):
        raise ValueError(f"Unknown region '{args.region}' in config")

    auth = region.get("auth", {})
    if not isinstance(auth, dict):
        raise ValueError("auth config missing")

    username = auth.get("username")
    password = auth.get("password")
    if not username or not password:
        raise ValueError("auth.username and auth.password are required")

    sso_base_url = auth.get("sso_base_url") or "https://sso.garmin.cn"
    client_id = auth.get("client_id") or "GarminConnect"
    locale = auth.get("locale") or "zh-CN"
    service_url = auth.get("service_url") or f"{region.get('base_url', '').rstrip('/')}/app"
    signin_url = _build_signin_url(sso_base_url.rstrip("/"), client_id, service_url, locale)
    target_base = region.get("base_url", "")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context = browser.new_context(locale=locale)
        page = context.new_page()
        page.goto(signin_url, wait_until="domcontentloaded")
        _fill_first(page, _USERNAME_SELECTORS, username)
        _fill_first(page, _PASSWORD_SELECTORS, password)
        _click_first(page, _SUBMIT_SELECTORS)
        try:
            page.wait_for_url(f"{target_base}*", timeout=60000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(3000)

        if target_base:
            page.goto(target_base, wait_until="domcontentloaded")

        cookies = context.cookies([target_base] if target_base else [])
        cookie_header = _cookie_header(cookies)
        if args.output:
            output_path = Path(args.output).expanduser()
            output_path.write_text(cookie_header)
            print(f"Wrote cookie header to {output_path}")
        if args.write_config:
            auth["type"] = "session_cookie"
            auth["cookie"] = cookie_header
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
            print(f"Updated {config_path} with session cookie")
        if not args.output and not args.write_config:
            print(cookie_header)

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
