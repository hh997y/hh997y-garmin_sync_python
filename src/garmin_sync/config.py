from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass
class SyncConfig:
    limit: int
    state_path: Path
    dry_run: bool
    verbose: bool
    download_dir: Path | None
    ignore_state: bool
    upload_dir: Path | None
    upload_glob: str | None
    mode: str
    direction: str


@dataclass
class EndpointConfig:
    list_activities: str | None
    download_activity: str | None
    upload_activity: str | None
    upload_consent: str | None


@dataclass
class AuthConfig:
    type: str
    cookie: str | None = None
    username: str | None = None
    password: str | None = None
    sso_base_url: str | None = None
    client_id: str | None = None
    locale: str | None = None
    service_url: str | None = None
    captcha_token: str | None = None
    remember_me: bool | None = None
    headless: bool | None = None
    login_debug: bool | None = None
    manual_login: bool | None = None
    cookie_cache_path: Path | None = None
    user_agent: str | None = None


@dataclass
class RegionConfig:
    base_url: str
    auth: AuthConfig
    endpoints: EndpointConfig
    list_params: Dict[str, Any]
    list_response_key: str | None
    id_field: str
    sort_key: str | None
    headers: Dict[str, str]
    consent_params: Dict[str, Any]


@dataclass
class AppConfig:
    china: RegionConfig
    global_region: RegionConfig
    sync: SyncConfig


class ConfigError(RuntimeError):
    pass


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required config key: {key}")
    return data[key]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    raw = yaml.safe_load(config_path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    china = _parse_region(_require(raw, "china"), "china")
    global_region = _parse_region(_require(raw, "global"), "global")
    sync = _parse_sync(_require(raw, "sync"))

    return AppConfig(china=china, global_region=global_region, sync=sync)


def _parse_sync(data: Dict[str, Any]) -> SyncConfig:
    limit = int(data.get("limit", 10))
    state_path = Path(data.get("state_path", "state/uploaded.json")).expanduser()
    dry_run = bool(data.get("dry_run", False))
    verbose = bool(data.get("verbose", False))
    download_dir_raw = data.get("download_dir")
    download_dir = Path(download_dir_raw).expanduser() if download_dir_raw else None
    ignore_state = bool(data.get("ignore_state", False))
    upload_dir_raw = data.get("upload_dir")
    upload_dir = Path(upload_dir_raw).expanduser() if upload_dir_raw else None
    upload_glob = data.get("upload_glob")
    mode = str(data.get("mode", "full"))
    if mode not in {"full", "download_only", "upload_only"}:
        raise ConfigError("sync.mode must be one of: full, download_only, upload_only")
    direction = str(data.get("direction", "cn_to_global"))
    if direction not in {"cn_to_global", "global_to_cn", "bidirectional"}:
        raise ConfigError("sync.direction must be one of: cn_to_global, global_to_cn, bidirectional")
    return SyncConfig(
        limit=limit,
        state_path=state_path,
        dry_run=dry_run,
        verbose=verbose,
        download_dir=download_dir,
        ignore_state=ignore_state,
        upload_dir=upload_dir,
        upload_glob=upload_glob,
        mode=mode,
        direction=direction,
    )


def _parse_region(data: Dict[str, Any], name: str) -> RegionConfig:
    base_url = _require(data, "base_url")
    auth = _parse_auth(_require(data, "auth"), name)
    endpoints = _parse_endpoints(_require(data, "endpoints"))
    list_params = dict(data.get("list_params", {}))
    list_response_key = data.get("list_response_key")
    id_field = data.get("id_field", "activityId")
    sort_key = data.get("sort_key")
    headers = {str(k): str(v) for k, v in data.get("headers", {}).items()}
    consent_params = dict(data.get("consent_params", {}))

    return RegionConfig(
        base_url=base_url,
        auth=auth,
        endpoints=endpoints,
        list_params=list_params,
        list_response_key=list_response_key,
        id_field=id_field,
        sort_key=sort_key,
        headers=headers,
        consent_params=consent_params,
    )


def _parse_auth(data: Dict[str, Any], name: str) -> AuthConfig:
    auth_type = _require(data, "type")
    if auth_type not in {"session_cookie", "playwright_login"}:
        raise ConfigError(f"Unsupported auth type '{auth_type}' for {name}")

    return AuthConfig(
        type=auth_type,
        cookie=data.get("cookie"),
        username=data.get("username"),
        password=data.get("password"),
        sso_base_url=data.get("sso_base_url"),
        client_id=data.get("client_id"),
        locale=data.get("locale"),
        service_url=data.get("service_url"),
        captcha_token=data.get("captcha_token"),
        remember_me=data.get("remember_me"),
        headless=data.get("headless"),
        login_debug=data.get("login_debug"),
        manual_login=data.get("manual_login"),
        cookie_cache_path=Path(data.get("cookie_cache_path")).expanduser()
        if data.get("cookie_cache_path")
        else None,
        user_agent=data.get("user_agent"),
    )


def _parse_endpoints(data: Dict[str, Any]) -> EndpointConfig:
    return EndpointConfig(
        list_activities=data.get("list_activities"),
        download_activity=data.get("download_activity"),
        upload_activity=data.get("upload_activity"),
        upload_consent=data.get("upload_consent"),
    )
