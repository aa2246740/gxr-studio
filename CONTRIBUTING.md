# 参与修改

用户向的用法在 README 前半部分。开发命令在 `AGENTS.md`。

提交前跑 `.githooks/pre-commit` 覆盖的检查。准备给别人看的改动跑 `make check`。

Python 用 snake_case，类用 PascalCase。页面脚本用 camelCase。测试文件保持 `tests/test_*.py`，端到端的放 `tests/integration/`。

依赖更新要等发布满 7 天，并且锁进 `uv.lock`。不要把 `.env`、图库、DNG 或 `GXRCE.DAT` 加进来。

问题用 `.github/ISSUE_TEMPLATE/` 里的缺陷或功能模板，并带上优先级、类型和区域标签。标签表在 `.github/labels.yml`。

## 许可与贡献

本项目使用 PolyForm Noncommercial 1.0.0（见 `LICENSE`）。提交贡献即表示你有权提交这些内容，并同意版权人 aa2246740 按本许可发布它们，也可以把它们纳入另行签订的商业授权。
