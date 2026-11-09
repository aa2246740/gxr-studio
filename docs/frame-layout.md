# 相纸边框

边框按批准的 R3 设计移植。照片不缩放、不裁切；成品比例控制照片外面的整张画布。

`finish/frame_core.py` 的整数求解器与 `web/assets/finish_frame_core.js` 一一对应。样式先生成照片、边沿和文字组成的单元，再找满足最小留白的最小整数比例画布。固定比例至少留 6% 侧边和顶边；白边的细、中、粗分别留 6%、9%、13%。单元按各样式的光学中心定位。

字体从成品宽高计算，按 SFNT 字形框计算大写 H 的高度。Inter SemiBold 的主行、Inter 的次行和实际 RICOH 标志按设计对齐。胶片边印按真实胶片比例取字号，不套正文下限。中文使用系统字体，与同一行的拉丁字共用基线，不加字距。

绘制项按底层、照片、上层排序。Python 和 Canvas2D 使用相同的矩形、圆角、阴影、孔洞、线、文字、旋转文字和多边形数据。胶片孔洞复制灯台背景，竖幅胶片的整个单元转 90°，照片保持原方向。拍立得原比例的画布就是相纸；固定比例在石色背景上绘制带阴影的相纸。

## 设置

边框 JSON 使用 `version: 2`，包含 `style`、`color`、`weight`、`show_logo`、`show_params`、`show_date`、`text`、`output_ratio`、`fmt`、`table`。

- `film.fmt` 是 `135` 或 `120`，`table` 是 `light` 或 `black`。
- `instant.fmt` 是 `polaroid`、`instax_square`、`instax_wide` 或 `instax_mini`。
- 徕卡、哈苏默认显示日期；拍立得默认不显示日期。文字默认留空。
- 旧版或非法的已存边框回到出厂“无边框”，不影响照片的其他编辑。下一次写入 sidecar 时清掉旧边框键。
- 新请求里的未知键、非法画幅、比例或显示开关返回中文错误，不按旧语义猜测。没有边框设置时继续继承默认值。

复制、粘贴和撤销按整块边框设置传递，成品尺寸接口与导出共用布局。

## 对照

`tests/golden/frames_r4.json` 保存批准参考实现的 9,696 个合成布局摘要。覆盖六种照片形状、全部八个成品比例、全部有效选项组合、空白和 24 字文字、完整和缺失拍摄信息。`test_frame_reference.py` 在任何 CI 环境验证摘要；本地有设计参考脚本时还逐字段对照，不存在时只跳过这项。Python/JS 逐项对照另外覆盖所有这些组合。

重新生成参考摘要：

```bash
uv run --frozen python -I scripts/frame_reference.py --reference /path/to/frame-design
```

真实照片和对照图不进仓库。后续样式可复用求解器和绘制顺序，再增加所需的背景处理或绘制项。
