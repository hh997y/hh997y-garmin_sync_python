# Garmin 中国站 -> 国际站同步

本项目用于从 Garmin 中国站下载最近的活动记录，并上传到 Garmin 国际站，自动跳过已上传的记录。

## 快速开始

1) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) 编辑 `config.yaml`

- 中国站/国际站认证方式只支持 `session_cookie` 或 `playwright_login`
- 确认 endpoints 和 params 与你自己的站点一致
- 通过 `sync.mode / sync.limit / sync.dry_run / sync.verbose` 控制行为

3) 运行

```bash
python run.py
```

`run.py` 固定读取当前目录下的 `config.yaml`。

常见切换方式（修改 `config.yaml`）：

- `sync.mode: download_only` 只下载
- `sync.mode: upload_only` 只上传（从 `sync.upload_dir` 读取本地文件）
- `sync.dry_run: true` 只下载不上传
- `sync.verbose: true` 输出更详细日志

## 调试建议

- `sync.mode: download_only` + `sync.verbose: true` 方便验证下载是否正常
- `sync.download_dir` 控制保存目录
- `sync.ignore_state: true` 忽略上传缓存，强制重试
- `sync.upload_dir` 用于只上传本地文件
- 从目录上传时，文件名（不含后缀）会作为 activity_id 去重
- 国际站上传通常需要 GDPR consent，请确保 `global.endpoints.upload_consent` 和 `global.consent_params` 配置正确

## Playwright 获取 Cookie（可选）

如果你希望手动拿 Cookie：

```bash
pip install -r scripts/requirements-playwright.txt
python -m playwright install chromium
python scripts/get_cookie.py --config config.yaml --region china --output state/cn_cookie.txt
```

将输出粘贴到 `china.auth.cookie`，并设置 `china.auth.type: session_cookie`。
也可以直接使用 `auth.type: playwright_login`，运行时自动拿 Cookie。

如需自动写回 `config.yaml`：

```bash
python scripts/get_cookie.py --config config.yaml --region china --write-config
```

## macOS 打包（.app）

生成可双击运行的 `.app`（包含 Playwright Chromium）：

```bash
bash scripts/build_mac_app.sh
```

产物在 `dist/GarminSync.app`，同时会生成 `dist/config.yaml`。分发时把 `GarminSync.app` 和 `config.yaml` 放在同一目录，先编辑 `config.yaml`，再双击运行应用。

如果需要在终端查看日志：

```bash
open dist/GarminSync.app --args
```

## 备注

- 已上传的活动会记录在 `state/uploaded.json`，确保重复运行不会重复上传
- 如果设置了 `sync.download_dir`，即使 `dry_run` 也会保存文件
- 下载可能返回 ZIP，本程序会自动解压并取出 `.fit`
- `list_params.limit` 会自动提高到至少 `sync.limit`
- 国际站上传地址应为 `/gc-api/upload-service/upload/.fit`

## 目录结构

- `src/garmin_sync/`: 核心逻辑
- `config.yaml`: 配置文件
- `state/`: 已上传记录缓存
