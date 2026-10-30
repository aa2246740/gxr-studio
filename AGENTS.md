# 给改 GXR 电脑工作室的 agent

这是一个本地网页工具：FastAPI 提供接口，`web/` 是不打包的 ES 模块。图库、导出和应用数据都在仓库外面。样本 DNG 在上层 `../captures/a12-50`，测试自己去读，不要把它们复制进来。

不要读 `/Volumes/RICOHDCX`，不要写 `~/Pictures/GXR Studio`、`~/Pictures/GXR 导出` 或 `~/Library/Application Support/GXR Studio`。测试已经把这些路径保护起来了。

## 环境

需要 uv 和 Python 3.12–3.14（`.python-version` 是 3.14）。

```verified-commands
make setup
make dev
make check
make test
make lint
make type
make security
make dast
make profile
make openapi
make notes
```

`make setup` 按 `uv.lock` 安装，并把 git 钩子指到 `.githooks/`。钩子跑 ruff、秘密扫描、大文件和死开关，不跑全量测试。全量检查是 `make check`。

不装钩子时：

```bash
uv sync --frozen
uv run --frozen gxr-studio --no-browser
```

令牌印在启动那一行。接口要带 `X-GXR-Token`，Host 必须是 `127.0.0.1:<端口>`。试健康检查：

```bash
curl -H "Host: 127.0.0.1:18765" -H "X-GXR-Token: $GXR_STUDIO_TOKEN" http://127.0.0.1:18765/api/health
```

不插真卡时，用 README 里的假卡命令，把 `GXR_STUDIO_VOLUMES`、`LIBRARY`、`EXPORT`、`HOME` 指到临时目录。

## 检查

- Python 用 ruff 做 lint 和 format，mypy 做类型检查（还不是 strict，见 pyproject）。
- 复杂度用 ruff 的 mccabe，上限写在 `[tool.ruff.lint.mccabe]`。
- 覆盖率门槛在 `make test` / CI 的 `--cov-fail-under`，也写在 `[tool.coverage.report]`。本地只想看结果可以用 `uv run --frozen pytest -q`。
- CI 失败会重跑一次失败的测试（`--reruns 1`）。
- 依赖要钉死版本并更新 `uv.lock`。新版本至少等 7 天，见 `docs/dependencies.md`。

## 命名

Python 模块、函数、变量用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE。测试文件是 `test_*.py`，测试函数是 `test_*`。

`web/assets/*.js` 里函数和变量用 camelCase，文件名用 snake_case。不要为了风格去改已经在页面里对上的 DOM id。

## 改代码时

界面行为保持不变，除非这次任务就是改界面。`library_rebuild` 开关默认开着，关了才会拒绝重建索引。

日志走 `gxr_studio.observe.log`，不要把令牌打出来。失败的后台任务要走到 `on_job_failed`。

架构、隐私、告警和开关的说明在 `docs/`。
