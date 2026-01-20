from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import requests

from .config import AppConfig, RegionConfig
from .http_client import GarminApiClient


@dataclass
class Activity:
    activity_id: str
    raw: Dict[str, Any]


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def sync_activities(
    config: AppConfig,
    limit: int | None = None,
    dry_run: bool | None = None,
    verbose: bool = False,
) -> None:
    # 以配置为主，必要时提升列表拉取条数以覆盖 sync.limit。
    sync_limit = limit if limit is not None else config.sync.limit
    sync_dry_run = dry_run if dry_run is not None else config.sync.dry_run
    list_params = dict(config.china.list_params)
    if "limit" in list_params:
        try:
            configured_limit = int(list_params["limit"])
        except (TypeError, ValueError):
            configured_limit = None
        if configured_limit is None or sync_limit > configured_limit:
            list_params["limit"] = sync_limit

    state = load_state(config.sync.state_path)
    state.setdefault("uploaded_ids", [])
    state.setdefault("results", {})
    uploaded_ids = set(state.get("uploaded_ids", []))

    # 只上传本地文件时，不登录中国站。
    if config.sync.mode == "upload_only":
        if not config.sync.upload_dir:
            raise ValueError("sync.upload_dir is required for upload_only mode")
        global_client = GarminApiClient(
            config.global_region.base_url,
            config.global_region.auth,
            headers=config.global_region.headers,
        )
        global_client.login()
        if verbose:
            global_client.session.headers.update({"X-Debug-Upload": "1"})
        upload_from_dir(
            global_client,
            config.global_region,
            config.sync.upload_dir,
            config.sync.upload_glob,
            uploaded_ids,
            config.sync.ignore_state,
            sync_dry_run,
            verbose,
            state,
            config.sync.state_path,
        )
        if not sync_dry_run:
            sync_uploaded_ids(state, uploaded_ids)
            save_state(config.sync.state_path, state)
        return

    china_client = None
    global_client = None
    # full/download_only 需要先登录中国站获取列表。
    if config.sync.mode in {"full", "download_only"}:
        china_client = GarminApiClient(
            config.china.base_url,
            config.china.auth,
            headers=config.china.headers,
        )
        china_client.login()
    # full 模式还需要登录国际站上传。
    if config.sync.mode in {"full"}:
        global_client = GarminApiClient(
            config.global_region.base_url,
            config.global_region.auth,
            headers=config.global_region.headers,
        )
        global_client.login()
        if verbose:
            global_client.session.headers.update({"X-Debug-Upload": "1"})

    if verbose:
        log(f"CN list endpoint: {config.china.endpoints.list_activities}")
        log(f"CN list params: {list_params}")
        csrf_token = china_client.session.headers.get("connect-csrf-token")
        if csrf_token:
            log(f"CN csrf token set (len={len(csrf_token)}): {csrf_token}")
        else:
            log("CN csrf token missing")
        cookie_names = sorted({cookie.name for cookie in china_client.session.cookies})
        if cookie_names:
            log(f"CN cookies: {cookie_names}")

    if not china_client:
        raise ValueError("china client not initialized for this mode")
    # 拉取活动列表并按时间倒序选取最近的 N 条。
    activities = fetch_activities(china_client, config.china, list_params=list_params, verbose=verbose)
    activities = sort_activities(activities, config.china.sort_key)
    selected = activities[:sync_limit]

    if verbose:
        log(f"Fetched activities: {len(activities)}")
        log(f"Selected activities (limit={sync_limit}): {len(selected)}")

    consent_done = False
    for activity in selected:
        if not config.sync.ignore_state and activity.activity_id in uploaded_ids:
            log(f"Already uploaded activity {activity.activity_id}")
            record_result(state, activity.activity_id, "already_uploaded")
            sync_uploaded_ids(state, uploaded_ids)
            save_state(config.sync.state_path, state)
            continue

        log(f"Downloading activity {activity.activity_id}")
        activity_bytes = download_activity(china_client, config.china, activity.activity_id)
        maybe_save_download(config.sync.download_dir, activity.activity_id, activity_bytes)

        if sync_dry_run or config.sync.mode == "download_only":
            log(f"Dry run: would upload activity {activity.activity_id}")
            record_result(state, activity.activity_id, "dry_run")
            sync_uploaded_ids(state, uploaded_ids)
            save_state(config.sync.state_path, state)
            continue

        if not global_client:
            raise ValueError("global client not initialized for this mode")
        if not consent_done:
            # 国际站上传前需要 GDPR consent。
            ensure_upload_consent(global_client, config.global_region, verbose)
            consent_done = True
        result = upload_activity(global_client, config.global_region, activity.activity_id, activity_bytes)
        if result == "already_uploaded":
            log(f"Already uploaded activity {activity.activity_id}")
            uploaded_ids.add(activity.activity_id)
            record_result(state, activity.activity_id, "already_uploaded")
        else:
            log(f"Uploaded activity {activity.activity_id}")
            uploaded_ids.add(activity.activity_id)
            record_result(state, activity.activity_id, "uploaded")
        sync_uploaded_ids(state, uploaded_ids)
        save_state(config.sync.state_path, state)


def fetch_activities(
    client: GarminApiClient,
    region: RegionConfig,
    list_params: Dict[str, Any] | None = None,
    verbose: bool = False,
) -> List[Activity]:
    if not region.endpoints.list_activities:
        raise ValueError("list_activities endpoint not configured")

    response = client.get_json(region.endpoints.list_activities, params=list_params or region.list_params)
    data = response.data
    if verbose:
        data_type = type(data).__name__
        log(f"List response status: {response.status_code}")
        log(f"List response url: {response.raw.url}")
        log(f"List response type: {data_type}")
        content_type = response.raw.headers.get("Content-Type")
        if content_type:
            log(f"List response content-type: {content_type}")
        if isinstance(data, dict):
            keys_preview = sorted(list(data.keys()))[:10]
            log(f"List response keys (preview): {keys_preview}")
        elif isinstance(data, list):
            log(f"List response length: {len(data)}")
        elif isinstance(data, str):
            snippet = data.replace("\n", " ")[:200]
            log(f"List response snippet: {snippet}")

    content_type = response.raw.headers.get("Content-Type", "")
    if isinstance(data, str) and "text/html" in content_type.lower():
        raise ValueError(
            "List endpoint returned HTML instead of JSON. "
            "This usually means auth is incomplete or required headers are missing."
        )

    if region.list_response_key:
        data = data.get(region.list_response_key, []) if isinstance(data, dict) else []

    if not isinstance(data, list):
        raise ValueError("Expected activities list in response")

    activities: List[Activity] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        activity_id = str(item.get(region.id_field))
        if not activity_id or activity_id == "None":
            continue
        activities.append(Activity(activity_id=activity_id, raw=item))

    return activities


def download_activity(client: GarminApiClient, region: RegionConfig, activity_id: str) -> bytes:
    if not region.endpoints.download_activity:
        raise ValueError("download_activity endpoint not configured")
    path = region.endpoints.download_activity.format(activity_id=activity_id)
    response = client.get_bytes(path)
    content = response.data
    # 下载结果可能是 ZIP，自动解压 FIT。
    if is_zip_bytes(content):
        return extract_fit_from_zip(content, activity_id)
    return content


def upload_activity(client: GarminApiClient, region: RegionConfig, activity_id: str, content: bytes) -> str:
    if not region.endpoints.upload_activity:
        raise ValueError("upload_activity endpoint not configured")
    normalized_id = normalize_activity_id(activity_id)
    filename = f"activity_{normalized_id}.fit"
    try:
        client.post_file(region.endpoints.upload_activity, filename, content)
    except requests.HTTPError as exc:
        response = exc.response
        if response is not None and response.status_code == 409:
            # 409 表示已上传过该活动。
            return "already_uploaded"
        raise
    return "uploaded"


def ensure_upload_consent(client: GarminApiClient, region: RegionConfig, verbose: bool = False) -> None:
    if not region.endpoints.upload_consent:
        return
    params = resolve_consent_params(region.consent_params)
    response = client.get_json(region.endpoints.upload_consent, params=params or None)
    if verbose:
        log(f"Upload consent status: {response.status_code}")


def resolve_consent_params(params: Dict[str, Any]) -> Dict[str, Any]:
    resolved = dict(params)
    for key, value in list(resolved.items()):
        if isinstance(value, str) and value.lower() in {"now", "now_ms"}:
            resolved[key] = int(time.time() * 1000)
    return resolved


def normalize_activity_id(activity_id: str) -> str:
    if activity_id.startswith("activity_"):
        return activity_id[len("activity_") :]
    return activity_id


def sort_activities(activities: Iterable[Activity], sort_key: str | None) -> List[Activity]:
    items = list(activities)
    resolved_key = resolve_sort_key(items, sort_key)
    if not resolved_key:
        return items

    def sort_value(activity: Activity) -> Any:
        raw_value = activity.raw.get(resolved_key)
        if isinstance(raw_value, str):
            try:
                return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                return raw_value
        return raw_value

    return sorted(items, key=sort_value, reverse=True)


def resolve_sort_key(activities: Iterable[Activity], sort_key: str | None) -> str | None:
    items = list(activities)
    if sort_key and any(activity.raw.get(sort_key) is not None for activity in items):
        return sort_key

    candidates = [
        "startTimeGmt",
        "startTimeGMT",
        "startTimeLocal",
        "startTimeUtc",
    ]
    for candidate in candidates:
        if any(activity.raw.get(candidate) is not None for activity in items):
            return candidate
    return None


def load_state(path: Path) -> Dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        return {"uploaded_ids": [], "results": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {"uploaded_ids": [], "results": {}}
        data.setdefault("uploaded_ids", [])
        data.setdefault("results", {})
        return data
    except json.JSONDecodeError:
        return {"uploaded_ids": [], "results": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def record_result(state: Dict[str, Any], activity_id: str, status: str, detail: str | None = None) -> None:
    results = state.setdefault("results", {})
    payload: Dict[str, Any] = {
        "status": status,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if detail:
        payload["detail"] = detail
    results[str(activity_id)] = payload


def sync_uploaded_ids(state: Dict[str, Any], uploaded_ids: set[str]) -> None:
    state["uploaded_ids"] = sorted(uploaded_ids)


def maybe_save_download(download_dir: Path | None, activity_id: str, content: bytes) -> None:
    if not download_dir:
        return
    download_dir = download_dir.expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    path = download_dir / f"activity_{activity_id}.fit"
    path.write_bytes(content)


def is_zip_bytes(content: bytes) -> bool:
    return len(content) >= 4 and content[:2] == b"PK"


def extract_fit_from_zip(content: bytes, activity_id: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zip_file:
        names = zip_file.namelist()
        fit_names = [name for name in names if name.lower().endswith(".fit")]
        target = fit_names[0] if fit_names else (names[0] if names else None)
        if not target:
            raise ValueError(f"No files found in activity zip {activity_id}")
        return zip_file.read(target)


def upload_from_dir(
    client: GarminApiClient,
    region: RegionConfig,
    upload_dir: Path,
    upload_glob: str | None,
    uploaded_ids: set[str],
    ignore_state: bool,
    dry_run: bool,
    verbose: bool,
    state: Dict[str, Any],
    state_path: Path,
) -> None:
    files = collect_upload_files(upload_dir, upload_glob)
    if not files:
        log(f"No files found in {upload_dir}")
        return

    if not dry_run:
        ensure_upload_consent(client, region, verbose)

    for path in files:
        activity_id = normalize_activity_id(path.stem)
        if not ignore_state and activity_id in uploaded_ids:
            log(f"Already uploaded activity {activity_id}")
            record_result(state, activity_id, "already_uploaded")
            sync_uploaded_ids(state, uploaded_ids)
            save_state(state_path, state)
            continue

        content = path.read_bytes()
        if dry_run:
            log(f"Dry run: would upload {path.name}")
            record_result(state, activity_id, "dry_run")
            sync_uploaded_ids(state, uploaded_ids)
            save_state(state_path, state)
            continue

        result = upload_activity(client, region, activity_id, content)
        if result == "already_uploaded":
            log(f"Already uploaded activity {activity_id}")
            uploaded_ids.add(activity_id)
            record_result(state, activity_id, "already_uploaded")
        else:
            log(f"Uploaded {path.name}")
            uploaded_ids.add(activity_id)
            record_result(state, activity_id, "uploaded")
        sync_uploaded_ids(state, uploaded_ids)
        save_state(state_path, state)


def collect_upload_files(upload_dir: Path, upload_glob: str | None) -> Sequence[Path]:
    upload_dir = upload_dir.expanduser()
    if not upload_dir.exists():
        return []
    pattern = upload_glob or "*.fit"
    return sorted(upload_dir.glob(pattern))
