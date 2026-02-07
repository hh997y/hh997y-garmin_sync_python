from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse
from pathlib import Path
from datetime import datetime

from .config import AuthConfig


class PlaywrightLoginError(RuntimeError):
    pass


def _log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def login_with_playwright(base_url: str, auth: AuthConfig, headless: bool = True) -> tuple[str, str | None]:
    if not auth.username or not auth.password:
        raise ValueError("playwright_login auth requires username and password")

    debug = bool(auth.login_debug)
    manual_login = bool(auth.manual_login)
    cookie_cache_path = auth.cookie_cache_path

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightLoginError(
            "Playwright is required for playwright_login. Install it with "
            "`pip install -r scripts/requirements-playwright.txt` and "
            "`python -m playwright install chromium`."
        ) from exc

    sso_base_url = auth.sso_base_url or "https://sso.garmin.cn"
    client_id = auth.client_id or "GarminConnect"
    locale = auth.locale or "zh-CN"
    service_url = auth.service_url or f"{base_url.rstrip('/')}/app"
    signin_url = _build_signin_url(sso_base_url.rstrip("/"), client_id, service_url, locale)
    if debug:
        _log(f"[login] headless={headless} locale={locale} signin_url={signin_url}")

    with sync_playwright() as playwright:
        start = time.monotonic()
        browser = playwright.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            locale=locale,
            user_agent=auth.user_agent,
            extra_http_headers={"Accept-Language": _accept_language(locale)},
        )
        _seed_locale_cookies(context, [sso_base_url, base_url], locale)
        if cookie_cache_path:
            _load_cookie_cache(context, cookie_cache_path, debug=debug)
        page = context.new_page()
        _apply_stealth(page, locale)
        login_capture: dict[str, Any] = {}
        if debug:
            _attach_debug_listeners(page, login_capture, log_all=bool(auth.login_debug))
        if debug:
            _log("[login] goto sign-in page")
        # 打开 SSO 登录页，等待基础 DOM 就绪即可。
        page.goto(signin_url, wait_until="domcontentloaded")
        # Many modern login pages keep background requests open (analytics/long-poll),
        # so waiting for "networkidle" can hang even when the UI is fully rendered.
        # Treat it as best-effort; the real gate is detecting the login form.
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            if debug:
                _log("[login] networkidle timeout; continuing")
        if debug:
            _log(f"[login] sign-in page loaded in {time.monotonic() - start:.1f}s")
        # 只有检测到验证码时才等待 cf_clearance。
        if _has_turnstile(page):
            if debug:
                _log("[login] detected turnstile; waiting for cf_clearance")
            _wait_for_cookie(context, sso_base_url, "cf_clearance", timeout_seconds=5, debug=debug)
        # 将浏览器里的 cookie 注入请求头，提升后续请求一致性。
        _apply_cookie_header(page, context, sso_base_url, debug=debug)
        # 等待登录表单出现后再填充。
        _wait_for_login_form(page, timeout_seconds=60)
        if manual_login:
            if debug:
                _log("[login] manual_login enabled; waiting for user to sign in")
        else:
            if debug:
                _log("[login] login form detected; filling credentials")
            _fill_first(page, _USERNAME_SELECTORS, auth.username)
            _fill_first(page, _PASSWORD_SELECTORS, auth.password)
            if debug:
                _log("[login] submitting form")
            _click_first(page, _SUBMIT_SELECTORS)
        # 捕获 SSO login 接口响应，提取 ticket/重定向信息。
        login_response = _wait_for_login_response(page, login_capture, timeout_ms=6000)
        if not login_response and login_capture:
            login_response = login_capture
        if not login_response:
            try:
                page.wait_for_url(
                    f"{base_url.rstrip('/')}/*", timeout=5000, wait_until="domcontentloaded"
                )
            except PlaywrightTimeoutError:
                pass
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        if already_on_app and not login_response and debug:
            _log(f"[login] on app domain without login response (url={page.url})")
        if debug and login_response:
            _log(
                f"[login] login response status={login_response['status']} url={login_response['url']}"
            )
        redeemed = _redeem_service_ticket(context, login_response, debug=debug)
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        logged_in = already_on_app
        direct_login = None
        if not login_response:
            # Give the response listener a moment to populate after navigation churn.
            login_response = _wait_for_login_response(page, login_capture, timeout_ms=3000)
            if login_response and debug:
                _log(
                    f"[login] late login response status={login_response['status']} url={login_response['url']}"
                )
                redeemed = _redeem_service_ticket(context, login_response, debug=debug)
        if not login_response and login_capture:
            login_response = login_capture
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        logged_in = already_on_app or bool(login_response)
        if already_on_app and debug:
            _log(f"[login] already on app domain; skipping fallback (url={page.url})")
        if not login_response and not already_on_app:
            if debug:
                _dump_login_debug(page)
                _log("[login] attempting direct SSO login fallback")
            direct_login = _direct_sso_login(context, sso_base_url, signin_url, auth, debug)
        if redeemed:
            try:
                if "connect.garmin.com" in base_url:
                    page.goto(f"{base_url.rstrip('/')}/app/home", wait_until="domcontentloaded")
                else:
                    page.goto(f"{base_url.rstrip('/')}/modern/", wait_until="domcontentloaded")
                if debug:
                    _log("[login] loaded /modern/ after redeem")
            except Exception:
                if debug:
                    _log("[login] failed to load /modern/ after redeem")
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
        logged_in = logged_in or already_on_app
        error_banner = _read_error_banner(page)
        if error_banner and not direct_login and not redeemed and not logged_in:
            raise PlaywrightLoginError(f"Login failed: {error_banner}")
        if not login_response and not logged_in:
            try:
                wait_timeout = 60000 if manual_login else 8000
                already_on_app = page.url.startswith(base_url.rstrip("/") + "/")
                if not already_on_app:
                    page.wait_for_url(
                        f"{base_url.rstrip('/')}/*",
                        timeout=wait_timeout,
                        wait_until="domcontentloaded",
                    )
            except PlaywrightTimeoutError:
                if debug:
                    _log(
                        f"[login] login redirect did not reach app domain within timeout (url={page.url})"
                    )

        app_html = None
        try:
            app_html = page.content()
        except Exception:
            app_html = None

        if not app_html and not logged_in:
            app_html = _fetch_app_html(context, base_url, timeout_ms=15000, debug=debug)

        if not app_html and base_url and not logged_in:
            page.goto(f"{base_url.rstrip('/')}/app", wait_until="domcontentloaded")
            if debug:
                _log("[login] fallback: fetched /app page")
            try:
                app_html = page.content()
            except Exception:
                app_html = None

        cookies = context.cookies([base_url] if base_url else [])
        cookie_header = _cookie_header(cookies)
        cookie_header = _ensure_locale_cookie(cookie_header, locale)
        csrf_token = _read_csrf_token(page, cookies, html=app_html)
        if cookie_cache_path:
            _save_cookie_cache(cookie_cache_path, cookies, debug=debug)
        if debug:
            csrf_status = "present" if csrf_token else "missing"
            _log(f"[login] csrf token {csrf_status}; cookies={len(cookies)}")
            if not cookies:
                _log(f"[login] current url={page.url} title={page.title()}")
            _write_login_summary(page, cookies, csrf_token)
            _log(f"[login] total time {time.monotonic() - start:.1f}s")
        context.close()
        browser.close()

    if not cookie_header:
        raise PlaywrightLoginError("Playwright login completed, but no cookies were captured.")
    return cookie_header, csrf_token


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


def _ensure_locale_cookie(cookie_header: str, locale: str) -> str:
    if "GarminUserPrefs=" in cookie_header:
        return cookie_header
    prefix = f"GarminUserPrefs={locale}"
    return f"{prefix}; {cookie_header}" if cookie_header else prefix


def _accept_language(locale: str) -> str:
    if locale.lower().startswith("en-"):
        return locale
    return f"{locale},{locale.split('-')[0]};q=0.9"


def _seed_locale_cookies(context, urls: list[str], locale: str) -> None:
    cookies = []
    for url in urls:
        if not url:
            continue
        hostname = urlparse(url).hostname
        if not hostname:
            continue
        cookies.append(
            {
                "name": "GarminUserPrefs",
                "value": locale,
                "domain": hostname,
                "path": "/",
            }
        )
    if cookies:
        context.add_cookies(cookies)


def _read_csrf_token(page, cookies: list[dict[str, Any]], html: str | None = None) -> str | None:
    if html:
        token = _extract_csrf_from_html(html)
        if token:
            return token
    script = """
    () => {
      const keys = [
        "connect-csrf-token",
        "csrf_token",
        "csrfToken",
        "XSRF-TOKEN",
        "xsrf-token",
      ];
      for (const key of keys) {
        try {
          const value = window.localStorage.getItem(key) || window.sessionStorage.getItem(key);
          if (value) return value;
        } catch (err) {}
      }
      return null;
    }
    """
    try:
        value = page.evaluate(script)
    except Exception:
        value = None
    if value:
        return value

    for cookie in cookies:
        name = cookie.get("name", "")
        if "csrf" in name.lower() or "xsrf" in name.lower():
            cookie_value = cookie.get("value")
            if cookie_value:
                return cookie_value

    if not html:
        try:
            html = page.content()
        except Exception:
            return None
    return _extract_csrf_from_html(html)
    return None


def _extract_csrf_from_html(html: str) -> str | None:
    patterns = [
        r"name=[\"']csrf-token[\"']\s+content=[\"']([^\"']+)[\"']",
        r"connect-csrf-token[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"csrfToken[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"csrf_token[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _fetch_app_html(
    context, base_url: str, timeout_ms: int = 60000, debug: bool = False
) -> str | None:
    if not base_url:
        return None
    app_url = f"{base_url.rstrip('/')}/app"
    try:
        if debug:
            _log("[login] fetching /app via context request")
        response = context.request.get(app_url, timeout=timeout_ms)
        if response.ok:
            if debug:
                _log("[login] fetched /app HTML via context request")
            return response.text()
        if debug:
            _log(f"[login] /app request failed status={response.status}")
    except Exception:
        if debug:
            _log("[login] /app request failed")
        return None
    return None


def _wait_for_login_response(
    page,
    login_capture: dict[str, Any],
    timeout_ms: int = 60000,
) -> dict | None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if login_capture:
            return login_capture
        time.sleep(0.2)
    return None


def _dump_login_debug(page) -> None:
    try:
        title = page.title()
    except Exception:
        title = ""
    _log(f"[login] no login response; page title='{title}' url={page.url}")
    try:
        alert_text = page.locator(".g__alert--error").inner_text().strip()
        if alert_text:
            _log(f"[login] error banner: {alert_text}")
    except Exception:
        pass
    try:
        if _has_turnstile(page):
            _log("[login] detected Cloudflare turnstile on page")
    except Exception:
        pass
    try:
        Path("state").mkdir(parents=True, exist_ok=True)
        page.screenshot(path="state/login_debug.png", full_page=True)
        Path("state/login_debug.html").write_text(page.content())
        _log("[login] wrote state/login_debug.png and state/login_debug.html")
    except Exception:
        _log("[login] failed to write debug artifacts")


def _read_error_banner(page) -> str | None:
    try:
        banner = page.locator(".g__alert--error")
        if banner.count():
            return banner.inner_text().strip()
    except Exception:
        return None
    return None


def _has_turnstile(page) -> bool:
    try:
        return bool(
            page.locator('iframe[src*="turnstile"]').count()
            or page.locator('div[class*="turnstile"]').count()
        )
    except Exception:
        return False


def _attach_debug_listeners(page, login_capture: dict[str, Any], log_all: bool) -> None:
    def on_console(msg):
        if msg.type == "error":
            try:
                _log(f"[login] console error: {msg.text}")
            except Exception:
                pass

    log_path = Path("state/login_network.log")
    if log_all:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("")
        except Exception:
            pass

    def log_line(message: str) -> None:
        if not log_all:
            return
        try:
            with log_path.open("a") as handle:
                handle.write(f"{message}\n")
        except Exception:
            pass

    def on_request_failed(request):
        url = request.url
        if "/portal/api/login" in url:
            try:
                _log(f"[login] request failed: {url} {request.failure}")
            except Exception:
                pass
        log_line(f"REQ_FAIL {request.method} {request.url} {request.failure}")

    def on_request(request):
        if "/portal/api/login" in request.url:
            try:
                headers = request.headers
                cookie = headers.get("cookie", "")
                has_cf = "cf_clearance=" in cookie
                _log(f"[login] login request headers keys={sorted(headers.keys())}")
                _log(f"[login] login request cookie has cf_clearance={has_cf}")
            except Exception:
                pass
        log_line(f"REQ {request.method} {request.url}")

    def on_response(response):
        if "/portal/api/login" in response.url:
            try:
                data = None
                try:
                    data = response.json()
                except Exception:
                    data = None
                if data is None:
                    try:
                        data = json.loads(response.text())
                    except Exception:
                        data = None
                login_capture.clear()
                login_capture.update({"status": response.status, "url": response.url, "data": data})
                _log(f"[login] login response status={response.status} url={response.url}")
            except Exception:
                pass
        elif response.status >= 400:
            try:
                url = response.url
                if "sso.garmin.com" in url:
                    _log(f"[login] response status={response.status} url={url}")
            except Exception:
                pass
        try:
            headers = response.headers
            location = headers.get("location")
            if location:
                log_line(f"RESP {response.status} {response.url} -> {location}")
            else:
                log_line(f"RESP {response.status} {response.url}")
        except Exception:
            log_line(f"RESP {response.status} {response.url}")

    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    page.on("request", on_request)
    page.on("response", on_response)


def _apply_stealth(page, locale: str) -> None:
    langs = ["en-US", "en"]
    if locale.lower().startswith("zh"):
        langs = ["zh-CN", "zh"]
    script = f"""
    () => {{
      Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
      window.chrome = window.chrome || {{ runtime: {{}} }};
      Object.defineProperty(navigator, 'languages', {{ get: () => {langs} }});
      Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
    }}
    """
    try:
        page.add_init_script(script)
    except Exception:
        pass


def _direct_sso_login(context, sso_base_url: str, signin_url: str, auth: AuthConfig, debug: bool) -> bool:
    login_url = (
        f"{sso_base_url}/portal/api/login"
        f"?{urlencode({'clientId': auth.client_id or 'GarminConnect', 'locale': auth.locale or 'en-US', 'service': auth.service_url or ''})}"
    )
    cookies = context.cookies([sso_base_url])
    cookie_header = _cookie_header(cookies)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": sso_base_url,
        "Referer": signin_url,
    }
    if auth.user_agent:
        headers["User-Agent"] = auth.user_agent
    if cookie_header:
        headers["Cookie"] = cookie_header
    payload = {
        "username": auth.username,
        "password": auth.password,
        "rememberMe": bool(auth.remember_me),
        "captchaToken": auth.captcha_token or "",
    }
    try:
        response = context.request.post(login_url, headers=headers, data=json.dumps(payload))
    except Exception:
        if debug:
            _log("[login] direct SSO login request failed")
        return False
    if debug:
        _log(f"[login] direct SSO login status={response.status}")
    if not response.ok:
        if debug:
            snippet = response.text().replace("\n", " ")[:200]
            _log(f"[login] direct SSO login response: {snippet}")
        return False
    try:
        data = response.json()
    except Exception:
        return False
    return _redeem_service_ticket(context, {"data": data}, debug=debug)


def _redeem_service_ticket(context, login_response: dict | None, debug: bool) -> bool:
    if not login_response:
        return False
    data = login_response.get("data")
    if not isinstance(data, dict):
        if debug:
            _log("[login] login response missing JSON payload")
        return False
    service_url = data.get("serviceURL") or data.get("serviceUrl") or data.get("service")
    ticket = data.get("serviceTicketId") or data.get("ticket")
    if not service_url:
        if debug:
            _log("[login] login response missing service URL")
        return False
    if ticket and "ticket=" not in service_url:
        sep = "&" if "?" in service_url else "?"
        service_url = f"{service_url}{sep}ticket={ticket}"
    if debug and ticket:
        _log(f"[login] redeeming service ticket {ticket}")
    if debug and not ticket:
        _log("[login] login response missing service ticket")
    try:
        response = context.request.get(service_url)
        if debug:
            _log(f"[login] redeemed service ticket status={response.status}")
        return response.ok
    except Exception:
        if debug:
            _log("[login] failed to redeem service ticket")
        return False


def _apply_cookie_header(page, context, base_url: str, debug: bool) -> None:
    if not base_url:
        return
    try:
        cookies = context.cookies([base_url])
    except Exception:
        cookies = []
    if not cookies:
        if debug:
            _log("[login] no cookies available to inject into request headers")
        return
    header_value = _cookie_header(cookies)
    if not header_value:
        return
    try:
        page.set_extra_http_headers({"Cookie": header_value})
        if debug:
            has_cf = "cf_clearance=" in header_value
            _log(f"[login] injected Cookie header (cf_clearance={has_cf})")
    except Exception:
        if debug:
            _log("[login] failed to inject Cookie header")


def _write_login_summary(page, cookies: list[dict[str, Any]], csrf_token: str | None) -> None:
    try:
        Path("state").mkdir(parents=True, exist_ok=True)
        summary_path = Path("state/login_summary.txt")
        lines = [
            f"url={page.url}",
            f"title={page.title()}",
            f"cookies={len(cookies)}",
            f"csrf_present={bool(csrf_token)}",
        ]
        summary_path.write_text("\n".join(lines))
    except Exception:
        pass


def _wait_for_cookie(context, base_url: str, name: str, timeout_seconds: int, debug: bool) -> None:
    if not base_url:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            cookies = context.cookies([base_url])
        except Exception:
            cookies = []
        for cookie in cookies:
            if cookie.get("name") == name:
                if debug:
                    _log(f"[login] cookie '{name}' present")
                return
        time.sleep(0.5)
    if debug:
        _log(f"[login] cookie '{name}' not detected within {timeout_seconds}s")


def _load_cookie_cache(context, path: Path, debug: bool) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        if debug:
            _log(f"[login] failed to read cookie cache {path}")
        return
    if isinstance(data, list):
        context.add_cookies(data)
        if debug:
            _log(f"[login] loaded {len(data)} cookies from {path}")


def _save_cookie_cache(path: Path, cookies: list[dict[str, Any]], debug: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cookies, indent=2))
        if debug:
            _log(f"[login] saved {len(cookies)} cookies to {path}")
    except Exception:
        if debug:
            _log(f"[login] failed to write cookie cache {path}")


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
        raise PlaywrightLoginError(f"Could not find input for selectors: {selectors}")
    locator.first.fill(value)


def _click_first(page, selectors: list[str]) -> None:
    locator = _find_locator(page, selectors)
    if not locator:
        raise PlaywrightLoginError(f"Could not find submit for selectors: {selectors}")
    locator.first.click()


def _find_locator(page, selectors: list[str]):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count():
                return locator
            for frame in page.frames:
                try:
                    frame_locator = frame.locator(selector)
                    if frame_locator.count():
                        return frame_locator
                except Exception:
                    continue
        time.sleep(0.5)
    return None


def _wait_for_login_form(page, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _find_locator(page, _USERNAME_SELECTORS) or _find_locator(page, _PASSWORD_SELECTORS):
            return
        title = ""
        try:
            title = page.title()
        except Exception:
            title = ""
        if title.lower().startswith("just a moment"):
            raise PlaywrightLoginError("Blocked by Cloudflare. Try auth.headless: false and retry.")
        time.sleep(0.5)
    raise PlaywrightLoginError(
        f"Login form not found at {page.url}. Try auth.headless: false or update selectors."
    )
