# 依赖

直接依赖写在 `pyproject.toml`，用 `==` 钉死。`uv.lock` 是安装时的唯一依据，用 `uv sync --frozen`。

`sentry-sdk` 是直接依赖，这样设了 `GXR_STUDIO_SENTRY_DSN` 就不用再装一次。没设这个变量时不会初始化，也不会发请求。它带上的 `urllib3` 是传递依赖；`certifi` 本来就在锁里。

`pydantic-core`、`anyio`、`h11`、`click`、`typing-extensions` 由 FastAPI、uvicorn 和 pydantic 加载。源码里不写它们的 import，但版本仍然钉在直接依赖里，避免锁文件被间接依赖悄悄换掉。deptry 的 DEP002 因此放行这五个名字。

`src/gxr_studio/engine/` 和 `src/gxr_studio/lut/spaces.py` 要和来源逐字节或逐段相同（见各自的 `PROVENANCE.json`）。ruff 不格式化它们。

等一个版本发布满 7 天再把它写进锁定文件。`renovate.json` 的 `minimumReleaseAge` 是同一条规则。安全修复如果已经在用的版本有公开的洞，可以不等满 7 天，但要在提交说明里写是哪一个洞。

加开发依赖也要锁版本，并在提交说明里写为什么需要它。
