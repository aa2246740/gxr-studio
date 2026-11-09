# 导入 r5 相机配方

将同一套 `GXRCE.DAT` 和 `GXRFX.BIN` 复制到电脑上的同一文件夹。在工作室选择文件时，同时选择这两个文件。开发接口也接受该文件夹路径，读取两份文件后再校验和替换数据。

工作室登记 g176 和 g178 的引擎 SHA、入口、BSS、重定位与链接末端，以及各自的库 SHA。缺库、错配、损坏、尾随字节或改标签的引擎会被拒绝，已导入的数据保留。未知代数的错误详情列出当前支持的代数，g177 尚未登记。

g178 每单元显示相机顺序的 23 份完整配方。原生 01–04 后面接 19 份独立记录，原生 05–08 的旧入口隐藏。Acros 保留 Cont、High、Shad、Fade、Filt 五项。完整记录按单元及内容摘要选择，换库或跨单元不会按旧编号误选另一种风格。

记录按库内十行数据展开为 552 B，旋钮使用同库的整数档位和剂量。MOUNT 的旋钮表按相机规则使用 50mm 的对应表，04 除外。每单元颜色标定等待 [PR #106](https://github.com/aa2246740/gxr-studio/pull/106) 合入后接入；本分支沿用 main 的标定范围，28mm 和 MOUNT 仍标为未单独校准。电脑预测不是实机照片。

旧代数继续走原来的解析路径。旧代数的 Looks 只用于旧文件，g178 不列 Looks 或旧用户条目。r5 接入只读取源文件，在受保护的应用目录保存副本，不提供写卡操作。

保留旧代数是因为它们的独立解析和既有验证不妨碍 r5，也让用户手里的合法旧副本仍可读取，不猜旧条目到新配方的对应关系。

卡或目录导入只认最外层的 `GXRCE.DAT`，不按时间或最大代数挑文件。改名备份和子目录文件不参与自动导入。旧 DAT 旁的 BIN 不会被读取；r5 DAT 必须配同代已登记的库，错配或缺库直接拒收，不退回旧文件。多张卡都有 DAT 时要求用户选择。浏览器明确上传旧 DAT 和 BIN 时，会提示只选择 DAT。

`POST /api/camdata/import-upload` 可上传 DAT 原始字节。双文件上传时，正文为 DAT 后接 BIN，`X-GXR-Dat-Bytes` 声明 DAT 字节数；两段各自受大小上限约束，校验完成前不落盘。

在拥有本地研究副本的开发机重跑全部 69 份记录和各控件边界的真实 C 对照：

```sh
python3 -I /Volumes/Workspace/Projects/T3-Runtime/projects/projectricoh/tools/loadgate.py
lockf -k /tmp/gxr-heavy.lock nice -n 15 env GXR_STUDIO_TEST_PARENT=/Volumes/Workspace/Projects/T3-Runtime/projects/projectricoh uv run --frozen python -I -m pytest -q tests/test_g178.py
```

只在 loadgate 返回 `admit: true` 后运行第二条。测试从本地研究路径读取受审文件，缺少它们时跳过。DAT、库、完整记录、C 源码和校准参数均不随公开仓库分发。

`scripts/verify_g178.py` 在 #106 合入后对 69 份默认记录逐单元比较研究颜色模型，覆盖 ISO 200 和 1600、两种 half 模式。用 `python -I -B` 在同一准入和锁下运行，参数为 `--research`、`--out` 和 `--home`。输出必须位于研究任务的 `local/`，临时应用数据目录必须在 Git 工作区之外。`prediction.json` 逐项记录浮点字节是否相同、最大差及量化后变化分量数，任何一项字节不同即失败。

CI 没有本地研究文件时，`tests/test_g178.py` 的真实包解码、引擎绑定、库错配、记录列表、69 条实际 C 对照、登记表和真实文件导入会明确跳过；缺少 C 源码或 clang 时 C 对照也跳过。未知代数与选择形状测试、`tests/test_r5_synthetic.py` 和 `tests/test_r5_coexistence.py` 继续运行，只使用独立合成数据。`tests/test_look_cards.py` 还用合成卡片检查完整配方的名称、选择和摘要，安装了 Node 时运行。
