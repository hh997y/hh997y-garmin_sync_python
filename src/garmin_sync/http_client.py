from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from .config import AuthConfig
from .playwright_login import login_with_playwright


@dataclass
class ApiResponse:
    status_code: int
    data: Any
    raw: requests.Response


class GarminApiClient:
    def __init__(self, base_url: str, auth: AuthConfig, headers: Optional[Dict[str, str]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)

    def login(self) -> None:
        if self.auth.type == "session_cookie":
            if not self.auth.cookie:
                raise ValueError("session_cookie auth requires cookie value")
            self._apply_cookie_header(self.auth.cookie)
            return

        if self.auth.type == "playwright_login":
            headless = True if self.auth.headless is None else bool(self.auth.headless)
            cookie_header, csrf_token = login_with_playwright(
                self.base_url, self.auth, headless=headless
            )
            # Playwright 返回 cookie/CSRF，直接注入到 requests 会话。
            self._apply_cookie_header(cookie_header)
            if csrf_token:
                self.session.headers.update({"connect-csrf-token": csrf_token})
            return

        raise ValueError(f"Unsupported auth type {self.auth.type}")

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> ApiResponse:
        response = self.session.get(self._url(path), params=params)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = response.text
        return ApiResponse(response.status_code, data, response)

    def get_bytes(self, path: str) -> ApiResponse:
        response = self.session.get(self._url(path))
        response.raise_for_status()
        return ApiResponse(response.status_code, response.content, response)

    def post_file(self, path: str, filename: str, content: bytes) -> ApiResponse:
        files = {"file": (filename, content)}
        response = self.session.post(self._url(path), files=files, timeout=60)
        if self.session.headers.get("X-Debug-Upload") == "1" and response.status_code != 409:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"[{timestamp}] Upload POST {self._url(path)} filename={filename} size={len(content)}"
            )
            print(f"[{timestamp}] Upload response {response.status_code} {response.reason}")
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = response.text
        return ApiResponse(response.status_code, data, response)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _apply_cookie_header(self, cookie_header: str) -> None:
        parsed = urlparse(self.base_url)
        domain = parsed.hostname or ""
        jar = SimpleCookie()
        jar.load(cookie_header)
        for key, morsel in jar.items():
            self.session.cookies.set(key, morsel.value, domain=domain, path="/")
