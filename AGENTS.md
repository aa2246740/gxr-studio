# 给改 GXR 电脑工作室的 agent

这是一个本地网页工具：FastAPI 提供接口，`web/` 是不打包的 ES 模块。大图的显卡预览引擎在 `frontend/`（TypeScript + WGSL，Vite + pnpm），构建产物 `web/assets/gpu/engine.js` 提交进仓库；改了 `frontend/` 要跑 `make web` 再提交，`make check` 会核对两者一致。图库、导出和应用数据都在仓库外面。样本 DNG 在上层 `../captures/a12-50`，测试自己去读，不要把它们复制进来。

不要读 `/Volumes/RICOHDCX`，不要写 `~/Pictures/GXR Studio`、`~/Pictures/GXR 导出` 或 `~/Library/Application Support/GXR Studio`。测试已经把这些路径保护起来了。

## 环境

需要 uv 和 Python 3.12–3.14（`.python-version` 是 3.14）。

```verified-commands
make setup
make dev
make check
make test
make test-serial
make lint
make type
make security
make dast
make profile
make openapi
make notes
make web
make core
make core-test
make core-wasm
make mobile-dev
make mobile-build
make mobile-check
```

`make setup` 按 `uv.lock` 安装，并把 git 钩子指到 `.githooks/`。钩子跑 ruff、秘密扫描、大文件和死开关，不跑全量测试。全量检查是 `make check`。

手机上要算的东西在 `core/`（Rust），编两个产物：`make core` 是给对拍测试用的原生 `gxr-cli`，`make core-wasm` 是网页里的 WebAssembly。`make core-test` 跑格式、clippy 和 Rust 单测。`mobile/` 是手机开发壳，`make mobile-dev` 在 `127.0.0.1:5199` 打开。`make check` 会先做这些，再跑测试，并且设 `GXR_REQUIRE_CORE=1`，对拍不能再跳过。没装 Rust 时普通的 `make test` 还是会跳过它们。`make setup` 在有 rustup 时补上 wasm32 目标；没有 rustup 只打印一行安装办法，不会失败。

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

- Python 用 ruff 做 lint 和 format，mypy 开了 strict（`[tool.mypy] strict = true`）。`engine/` 和 `lut/spaces.py` 逐字节锁定，注解在旁边的 `.pyi`。
- 复杂度用 ruff 的 mccabe，上限写在 `[tool.ruff.lint.mccabe]`。
- 覆盖率门槛在 `make test` / `make check` 的合并报告（`--cov-fail-under=85`），也写在 `[tool.coverage.report]`。本地只想看结果可以用 `uv run --frozen pytest -q`。
- `make test` 把不依赖墙钟速度的测试拆开并行跑。进程数看环境变量 `GXR_LOADGATE`（指向 `loadgate.py`）的 `workers()`，最多 6 个；没设置或文件不存在时用 `min(cpu_count - 1, 4)`，至少 1 个，并说明公共仓库不依赖那条私有路径。每个分片有自己的临时目录、`TMPDIR` 和覆盖率数据。收集只做一次：分片只跑这次计划里的 node id，不再把位置参数、`-m` 和 `-k` 传给子进程。标了 `browser_serial` 的长时间浏览器流程等计算分片全部结束后再串行跑，不拿锁，免得和计算分片抢 CPU，把页面脚本的 120 秒等待拖过期限。标了 `exclusive` 的测试断言依赖墙钟速度，串行跑，并且只用 `lockf -t 3600 -k /tmp/gxr-heavy.lock` 拿锁。两条标记同时在的用例走 exclusive。exclusive 阶段开始前、每个 exclusive 用例之前，runner 先等机器安静：`GXR_LOADGATE` 可用时问它的 `quiet_for_timing()`（没有就用 `cap(0)` 的 admit），没设置、加载失败或调用出错时用公开回退（1 分钟负载均值低于 CPU 数的一半），每 `GXR_QUIET_POLL_S` 秒（默认 10）问一次；一次 runner 的等待合计最多 `GXR_QUIET_MAX_WAIT_S` 秒（默认 600），用完就照样跑，并打印一行「一直没安静下来」和当时的负载。每次等待都打印等了多久、看到的负载。速度断言写成 `tests/support/speed.py` 的 `assert_fast` / `speed_budgets()`，没达标抛 `SpeedBudgetExceeded`；预算数值不放宽。exclusive 用例最后一次尝试只因它失败（功能断言、setup、teardown 都没出错）时，runner 等安静后在 exclusive 阶段把它重跑一次，以第二次为准，两次的输出都在日志里，JUnit 里只留第二次。别的失败一律不重跑。这是和 `--reruns` 分开的机制，`--force-reruns=0` 时照样生效。新加的速度断言要标 `exclusive`，`tests/test_run_tests.py` 会检查。跑测进程自己提到 nice 15，收到 SIGINT 退出 130、SIGTERM 退出 143，并清掉子进程。`make test-serial` 仍是原来的单进程命令。`--reruns 1` 和覆盖率门槛不变。用户要的 JUnit、覆盖率 XML 等报告由父进程按合并后的数据写一次。
- 测试规矩（2026-11-10 机主定）：日常测试全在本地跑，合并前以本地 `make check` 通过为准，不等 GitHub。GitHub 的全量 CI 只能手动触发（Actions → ci → Run workflow），只在重大版本发布、机主明确要求时跑。需要干净环境或 Linux 检查时，用 Apple Container 开容器跑。Windows 不走 CI。
- 依赖要钉死版本并更新 `uv.lock`。新版本至少等 7 天，见 `docs/dependencies.md`。

## 命名

Python 模块、函数、变量用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE。测试文件是 `test_*.py`，测试函数是 `test_*`。

`web/assets/*.js` 里函数和变量用 camelCase，文件名用 snake_case。不要为了风格去改已经在页面里对上的 DOM id。

## 改代码时

先只做 macOS。Windows 适配暂停（2026-11-06 机主决定）：不为 Windows 写新代码，也不追 Windows 专属的失败；已有的 Windows 代码不删。Windows 先不走任何 CI，以后由机主自己测。

界面行为保持不变，除非这次任务就是改界面。`library_rebuild` 开关默认开着，关了才会拒绝重建索引。

日志走 `gxr_studio.observe.log`，不要把令牌打出来。失败的后台任务要走到 `on_job_failed`。Sentry 只有 `GXR_STUDIO_SENTRY_DSN` 有值才初始化，默认不联网。

架构、隐私、告警和开关的说明在 `docs/`。
