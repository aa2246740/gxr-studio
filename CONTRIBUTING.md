# 参与修改

用户向的用法在 README 前半部分。开发命令在 `AGENTS.md`。

提交前跑 `.githooks/pre-commit` 覆盖的检查。准备给别人看的改动跑 `make check`。

`make test` 并行跑普通测试，进程数由 `GXR_LOADGATE` 指向的 `loadgate.py` 里 `workers()` 决定（最多 6 个）。没设置这个变量时退回 `min(cpu_count - 1, 4)`，至少 1 个。断言依赖墙钟速度的测试标 `exclusive`，串行跑，并且只在这一阶段用 `lockf` 锁住 `/tmp/gxr-heavy.lock`。要跟以前一样单进程跑，用 `make test-serial`。覆盖率仍是合并后不低于 85%，失败重跑仍是 `--reruns 1`。

Python 用 snake_case，类用 PascalCase。页面脚本用 camelCase。测试文件保持 `tests/test_*.py`，端到端的放 `tests/integration/`。

依赖更新要等发布满 7 天，并且锁进 `uv.lock`。不要把 `.env`、图库、DNG 或 `GXRCE.DAT` 加进来。

## 验证软件显卡

浏览器测试同时设置 `--use-webgpu-adapter=swiftshader` 和 `--use-angle=swiftshader`，让 WebGPU 和画布呈现使用相同的软件后端。
在 Headless Shell 1228 上，只指定前者会出现离屏纹理正确、实际画布全黑的情况；验收仍检查实际画布像素和原直方图更新门槛。

设置 `GXR_PLAYWRIGHT_RUNTIME` 时，也把该运行时的浏览器可执行路径传给 `GXR_CHROME`。旧驱动缺少这个路径时直接失败，不再自动选择系统浏览器。

问题用 `.github/ISSUE_TEMPLATE/` 里的缺陷或功能模板，并带上优先级、类型和区域标签。标签表在 `.github/labels.yml`。

## 许可与贡献

本项目使用 PolyForm Noncommercial 1.0.0（见 `LICENSE`）。提交贡献即表示你有权提交这些内容，并同意版权人 aa2246740 按本许可发布它们，也可以把它们纳入另行签订的商业授权。
