# 工作室怎么串起来

一个进程。`gxr_studio.cli` 拿图库锁，听 `127.0.0.1`，`create_app` 装上图库、任务表、渲染和安全中间件。浏览器打开的页面来自 `web/`，脚本是普通 ES 模块，没有打包步骤。

请求先过 `SecurityMiddleware`（令牌、Host、Origin），再过 `ObserveMiddleware`（`X-Request-ID` 和进程内计数）。业务路由在 `api/routes.py`、`render_routes.py`、`camdata_routes.py`、`lut_routes.py`。

数据在本机，不调用外部服务：

| 东西 | 放哪 | 谁用 |
| --- | --- | --- |
| 照片和边车 | 图库目录，默认 `~/Pictures/GXR Studio` | `library.store` |
| SQLite 索引 | `<图库>/.studio/index.sqlite`，可以删掉重建 | `library.store`，表结构在 `src/gxr_studio/library/schema.sql` |
| HDR 版本（相机 JPG + 增益图） | `<图库>/.studio/hdr/<id 前两位>/<id>-v2.jpg`，做不了的记 `.json`；可以删掉，下次导入后重新生成 | `hdr.derived` |
| 相机数据和标定 | 应用数据目录 | `camdata` |
| LUT 库、原文件和 cube 导出 | 应用数据目录：`luts/library.json`、`luts/<SHA>.cube`、`luts/exports/<SHA>.cube` | `lut.library` |
| 界面偏好（引导是否结束、导出 HDR） | 应用数据目录 `prefs.json` | `prefs` |
| 导出的 JPEG/PNG | 导出目录 | `export` |
| 存储卡 | 只读。测试禁止碰 `/Volumes` 里真实的卡 | `cards` |

渲染链是 `raw` 读 DNG，`engine` 拿拟合好的整数表，`render` 把裁切和配方套上去。`lut` 在已经有相机数据时做路线 1 的旋钮拟合。`devtools` 只给测试和假卡用，运行时代码不许 import 它（import-linter 会查）。

LUT 库沿用 `lut.cube` 和 `lut.spaces`，用 `PrivateDir` 和 `durable_write` 保存。关键词与 33 点灰轴检查组合出 known、guessed、unknown、conflict；未知或冲突时需选择输入空间。用户能改选现有全部 12 种空间，改选后版本递增，旧拟合不可再用；缩略图、预览的磁盘缓存键和图片 URL 同时带 LUT 身份与空间版本。旧版 SHA 命名的 cube 文件首次打开时登记到库。

署名从整个 cube 的注释读取。内容和署名未改时，导出使用原始字节，TITLE 需要清理时只替换该行；署名改过时保留全部原注释，头部增加当前字段，末尾的 JSON 注释保留多行字段。旧库首次读取时补齐此前未采集的署名，保留用户已改过的字段。

照片编辑只存 LUT 的 SHA，颜色空间从库读取。`lut.photo` 接在 `finish.serve.color_image`，预览和全尺寸导出共用。显示类 LUT 使用现有底图，Log 类使用 A12 50mm DNG 的场景 XYZ；全尺寸仍按 RAW 条带计算。照片调整在 LUT 之后执行。没有新增写卡路径；全部调节和机内 LUT 两档预测延期。

HDR 在 `hdr`：`gain` 从同一张 DNG 算增益图（对不齐就不做），`container` 按 ISO 21496-1 + Ultra HDR 封装，主图字节不动。导入结束后另起一个 `hdr` 任务生成图库里的 HDR 版本；导出勾选 HDR 时由 `finish.hdr_export` 让 RAW 亮度跟着成片的几何、日期戳和边框走，在导出的 JPEG 上重新算。

生成前，`hdr.capture` 核对原始文件中双方都有的拍摄时间、秒的小数部分、曝光时间、光圈、ISO、相机型号、镜头和图像唯一 ID。已知字段冲突就记为 `capture_mismatch`，原因 JSON 和导出提示均为“RAW 与 JPG 不是同一次拍摄”。缺失、空白或未知数值不表示相同，仍须通过图像检查；不以文件名、文件修改时间或当前日期补全。相机时钟一致地设在 2000 年也可以正常配对。

图像检查包含全局平移、4×4 局部结构相关和多种窗口大小的亮度残差。局部必须有当前位置的相关证据；小范围搜索没有更优结果不能证明已对齐。残差检查先平均取样，再拟合窗口内的亮度关系；超过误差门槛的连续区域会导致不支持。v1 缓存不再显示为已通过，下一次导入后按 v2 重查。

关闭应用时，先在 `hdr_lock` 内停止接受新 HDR 任务并发出取消信号，再等现有 HDR 线程退出，最后关闭图库。整轮枚举出错会把 HDR 进度设为 `failed`，由任务表调用 `on_job_failed`；逐张失败仍独立计数。

后台导入和拟合走 `jobs`。任务抛错时 `register_alert_service` 调用 `on_job_failed`，由 `StudioNotifier` 记一条打过码的告警。

## 显卡预览（功能开关 `gpu_preview`）

大图可以在浏览器的 WebGPU 里实时计算，不再每动一下就请求服务器出图。

### 引擎和数据

- 引擎在 `frontend/`（TypeScript + WGSL），构建产物 `web/assets/gpu/engine.js` 提交进仓库。
- `web/assets/gpu_stage.js` 决定这一张、这组参数走显卡还是服务器，分支与 `finish.serve.color_image` 一一对应。
- 显卡要的数据由 `gpu/` 准备，经 `api/gpu_routes.py` 的只读接口下发，不写文件：
  - DNG 原字节和 RAW 布局；
  - 相机 JPG 原文件，浏览器按 EXIF 方向解码，实测与 PIL 逐字节相同；
  - 预览网格的配方烘焙表，与 `render_preview` 共用缓存；
  - LUT 的 3D 表；
  - 成片裁切框，与 `finish_array` 裁出的位置相同。

### 着色器

着色器按 Python 逐项对应：

| 着色器 | 对应的 Python | 做什么 |
| --- | --- | --- |
| `bin.wgsl` | `half_res_codes` + `orient_array` | RAW 合并、转正 |
| `common.wgsl` | `chain._chroma_chunk`、`tones`、`lut.spaces.Target.render`、`finish.pixels` | 颜色链 |
| `geo.wgsl` | `finish.geometry` 的 `_straighten_coords`、`_sample`、`_rot_flip` | 拉直、旋转、翻转、裁切 |

每个 Python 落成 uint8 的地方，着色器都照样取整。

`tests/test_gpu_parity.py` 用合成数据在无头 Chrome 的 SwiftShader 上逐像素对拍，门槛是 ≤1 码值的像素 ≥ 99.99%、平均 ΔE2000 ≤ 0.01。

### 回到服务器出图的情况

以下情况仍由服务器出图，前端不画近似结果：

- 原图（按住空格）；
- 透视、畸变；
- 成片里有边框或时间戳（裁剪页的未裁切画面照常走显卡）；
- 只有 JPG 却选了配方（近似）；
- 带一维表的 LUT，或输出是 Log 的 LUT；
- RAW 写法特殊：压缩、线性化表、逐行黑电平等；
- 浏览器没有 WebGPU，或显卡设备丢失。

### 预热

第一次请求某张照片的显卡信息时，后台把它所在单元、所在 ISO 模型的 8 个内置配方先烘焙好（`RenderService.warm_preview_bakes`）。浏览器同时预取左右相邻照片的源图和配方表。
