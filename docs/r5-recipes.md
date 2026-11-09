# 导入 r5 相机配方

将同一套 `GXRCE.DAT` 和 `GXRFX.BIN` 复制到电脑上的同一文件夹。在工作室选择文件时，同时选择这两个文件。开发接口也接受该文件夹路径，读取两份文件后再校验和替换数据。

工作室登记 g176 和 g178 的引擎 SHA、入口、BSS、重定位与链接末端，以及各自的库 SHA。缺库、错配、损坏、尾随字节或改标签的引擎会被拒绝，已导入的数据保留。未知代数的错误详情列出当前支持的代数，g177 尚未登记。

g178 每单元显示相机顺序的 23 份完整配方。原生 01–04 后面接 19 份独立记录，原生 05–08 的旧入口隐藏。Acros 保留 Cont、High、Shad、Fade、Filt 五项。完整记录按单元及内容摘要选择，换库或跨单元不会按旧编号误选另一种风格。

记录按库内十行数据展开为 552 B，旋钮使用同库的整数档位和剂量。MOUNT 的旋钮表按相机规则使用 50mm 的对应表，04 除外；颜色预测仍使用该镜头单元自己的校准模型，见 [单元校准](unit-calibration.md)。电脑预测不是实机照片。

旧代数继续走原来的解析路径。旧代数的 Looks 只用于旧文件，g178 不列 Looks 或旧用户条目。r5 接入只读取源文件，在受保护的应用目录保存副本，不提供写卡操作。

`POST /api/camdata/import-upload` 可上传 DAT 原始字节。双文件上传时，正文为 DAT 后接 BIN，`X-GXR-Dat-Bytes` 声明 DAT 字节数；两段各自受大小上限约束，校验完成前不落盘。

在拥有本地研究副本的开发机重跑全部 69 份记录和各控件边界的真实 C 对照：

```sh
python3 -I /Volumes/Workspace/Projects/T3-Runtime/projects/projectricoh/tools/loadgate.py
lockf -k /tmp/gxr-heavy.lock nice -n 15 env GXR_STUDIO_TEST_PARENT=/Volumes/Workspace/Projects/T3-Runtime/projects/projectricoh uv run --frozen python -I -m pytest -q tests/test_g178.py
```

只在 loadgate 返回 `admit: true` 后运行第二条。测试从本地研究路径读取受审文件，缺少它们时跳过。DAT、库、完整记录、C 源码和校准参数均不随公开仓库分发。
