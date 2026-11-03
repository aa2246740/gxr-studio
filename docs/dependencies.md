# 依赖

直接依赖写在 `pyproject.toml`，用 `==` 钉死。`uv.lock` 是安装时的唯一依据，用 `uv sync --frozen`。

`sentry-sdk` 是直接依赖，这样设了 `GXR_STUDIO_SENTRY_DSN` 就不用再装一次。没设这个变量时不会初始化，也不会发请求。它带上的 `urllib3` 是传递依赖；`certifi` 本来就在锁里。

`pydantic-core`、`anyio`、`h11`、`click`、`typing-extensions` 由 FastAPI、uvicorn 和 pydantic 加载。源码里不写它们的 import，但版本仍然钉在直接依赖里，避免锁文件被间接依赖悄悄换掉。deptry 的 DEP002 因此放行这五个名字。

`src/gxr_studio/engine/` 和 `src/gxr_studio/lut/spaces.py` 要和来源逐字节或逐段相同（见各自的 `PROVENANCE.json`）。ruff 不格式化它们。

等一个版本发布满 7 天再把它写进锁定文件。`renovate.json` 的 `minimumReleaseAge` 是同一条规则。安全修复如果已经在用的版本有公开的洞，可以不等满 7 天，但要在提交说明里写是哪一个洞。

加开发依赖也要锁版本，并在提交说明里写为什么需要它。

Windows 的 NumPy 固定为 2.5.4，其他平台保持 2.4.3。Windows 的 2.4.3 轮子自带 OpenBLAS 0.3.31.dev，
两个线程并发执行 65536×3 的矩阵乘法会停住，独立于文件句柄代码和 LUT 表分配。
2.5.4 轮子的 OpenBLAS 0.3.34.106.0 通过同一双线程入口，完整 256³ RGBA 表的 SHA-256 也与 2.4.3 设置
`OPENBLAS_NUM_THREADS=1` 的对照结果相同。[2.5.4](https://github.com/numpy/numpy/releases/tag/v2.5.4)
在 2026-10-10 发布，已超过 7 天。`scripts/windows_probe.py matmul` 和 `lut` 可重跑这两项检查。

## 前端构建（`frontend/`）

显卡预览引擎用 TypeScript + WGSL 写，Vite 打成一个不依赖外部包的 ES 模块 `web/assets/gpu/engine.js`。**产物提交进仓库**，所以双击「GXR Studio.command」、`make dev` 和 `uv run gxr-studio` 都不需要 Node。只有改 `frontend/` 的人需要 Node 22+ 和 pnpm（`packageManager` 钉在 `pnpm@10.33.2`），用 `make web` 重新构建后提交；`make check`（`scripts/check_frontend.py`）会重新构建到临时目录并逐字节核对，CI 里还要求核对必须跑（`GXR_REQUIRE_FRONTEND=1`）。

版本用 `frontend/package.json` 的精确版本和 `frontend/pnpm-lock.yaml` 钉死，`frontend/.npmrc` 的 `minimum-release-age` 同样要求新版本发布满 7 天。都是开发依赖，打进产物的只有我们自己的代码：

| 包 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| `vite` | 8.3.5 | MIT | 构建（库模式），把 `.wgsl` 以文本内联 |
| `typescript` | 7.0.2 | Apache-2.0 | 类型检查（`tsc --noEmit`） |
| `@webgpu/types` | 0.1.74 | BSD-3-Clause | WebGPU 的类型声明 |

传递依赖（`pnpm licenses list`）：rolldown、@rolldown/*、@oxc-project/types、postcss、nanoid、picomatch、tinyglobby、fdir、fsevents 为 MIT；picocolors 为 ISC；source-map-js 为 BSD-3-Clause；detect-libc 为 Apache-2.0；lightningcss 为 MPL-2.0（Vite 自带的 CSS 工具，只在构建机上运行，不进入产物，不改它的源码，所以 MPL 的文件级义务不涉及本仓库）。以上许可都允许在 PolyForm Noncommercial 1.0.0 的项目里使用。

一致性测试（`tests/test_gpu_parity.py`）用本机已装的 Google Chrome 的 SwiftShader 软件适配器，不新增 Python 依赖。
