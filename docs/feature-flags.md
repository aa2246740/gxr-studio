# 功能开关

登记表在 `src/gxr_studio/flags.py`。默认值就是现在的行为。要临时关掉，在应用数据目录写 `flags.json`，例如 `{"library_rebuild": false}`。文件不在时全部走默认。

现在的开关：

| 名字 | 默认 | 作用 |
| --- | --- | --- |
| `library_rebuild` | 开 | `POST /api/library/rebuild`。关掉返回 403 `feature_disabled`。 |
| `hdr_on_import` | 开 | 导入结束后在后台给 RAW+JPG 的照片生成 HDR 版本（`.studio/hdr/`）。关掉就不生成；导出时的 HDR 不受影响。 |
| `gpu_preview` | 关（复审时再定） | 大图改用浏览器的 WebGPU 实时预览：拖滑块、换照片不再请求服务器出图（`web/assets/gpu_stage.js`）。浏览器没有 WebGPU、照片或编辑项显卡做不了时，自动回到服务器出图。关着时前端不发任何显卡请求，服务器也不解析 DNG（`/api/settings` 里带着开关状态）。打开：`{"gpu_preview": true}`。 |

## 清理

`scripts/dead_flags.py` 要求每个开关都能在 `src/` 里找到引用，代码里的 `flag_enabled(..., "名字")` 也必须先登记。`added` 超过 180 天的开关会被判失败：要么删掉调用让行为固定下来，要么在仍需要它时更新说明和日期。CI 和 `make check` 会跑这个脚本。
