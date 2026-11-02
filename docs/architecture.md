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

LUT 库沿用 `lut.cube` 和 `lut.spaces`，用 `PrivateDir` 和 `durable_write` 保存。输入空间先读文件内的明确声明（`LUMIXPHOTOSTYLE`、注释里的 input 字段），再读 TITLE、文件名中的输入域，最后才读目标风格名。TITLE、文件名先一起检查明确的 input 标记，再检查转换源域和其他关键词，同级标记冲突时保留 conflict。文件内声明不受风格名或灰轴形状覆盖。只自动选择现有受支持空间，F-Log2C 的独立色域尚未支持，保留 unknown。无声明、无输入域关键词的创意 LUT 保留 unknown，不凭灰轴选 sRGB。33 点灰轴仅用于核对关键词猜测，known、guessed、unknown、conflict 四态沿用；未知或冲突时需选择输入空间。

输出先读注释中的 output 字段，再读 TITLE、文件名及其他注释的显示传递函数。Rec709 / BT.709 选 `rec709`，sRGB 选 `srgb`，BT1886 / Gamma 2.4 选 `gamma24`（零黑位假设）；原色名不当作传递函数，Rec709 与 BT1886 同时出现时采用 Gamma 2.4。没有明确输出或声明冲突时保持原有 sRGB 默认，用户仍可手选现有输出控件。

自动识别版本记在条目的 `identification_version`（当前为 2），与渲染用的空间 `version` 分开。首次读旧库时重新识别未手选过的条目；`input_space_manual=true` 的整个空间配置保持原样，包括只改过输出、色域或范围的条目。自动条目的输入或输出空间变了，空间版本只递增一次，清空 fit、recipe、camera_lut；仅依据或状态更新不升空间版本。迁移原子写入，重复打开不再改写。原始 cube、名称、分组、收藏和署名保持不动。缩略图、预览的磁盘缓存键和图片 URL 同时带 LUT 身份与空间版本，旧拟合的版本检查会拒绝迁移前的结果。用户改选空间继续沿用相同的失效规则。旧版 SHA 命名的 cube 文件首次打开时登记到库。

署名从整个 cube 的注释读取。内容和署名未改时，导出使用原始字节，TITLE 需要清理时只替换该行；署名改过时保留全部原注释，头部增加当前字段，末尾的 JSON 注释保留多行字段。旧库首次读取时补齐此前未采集的署名，保留用户已改过的字段。

照片编辑只存 LUT 的 SHA，颜色空间从库读取。`lut.photo` 接在 `finish.serve.color_image`，预览和全尺寸导出共用。显示类 LUT 使用现有底图，Log 类使用 DNG 的场景 XYZ，支持 A12 50mm、28mm、MOUNT；仍需可用的白平衡和色彩矩阵。各单元使用自身 DNG 矩阵，曝光采用既有固定中灰锚（50mm 测光标定），不按 LUT 自动曝光；MOUNT 与 28mm 的绝对曝光、测色尚无独立标定，输出均标为电脑预测。全尺寸仍按 RAW 条带计算。照片调整在 LUT 之后执行。没有新增写卡路径；全部调节和机内 LUT 两档预测延期。

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
  - LUT 表：显示类输入（sRGB / Rec.709 / Gamma 2.4）是 8 位精确表——全部 256³ 个输入逐个交给生产函数
    `lut.photo._apply` 算出来（RGBA，64 MB，两个线程约 4 秒，按 LUT 版本缓存两张），着色器只查表，与 Python 逐像素相同，
    一维表和任何输出空间都包括在内；Log 类输入下发 3D 表和空间参数，由着色器计算；
  - 成片裁切框，与 `finish_array` 裁出的位置相同。请求严格校验（有限数、类型、范围、尺寸不超过这张照片），不合格回 422。
- 开关关着时 `/gpu` 只回 `{"enabled": false}`，不解析 DNG、不预热；`/api/settings` 带着开关状态，前端据此不发任何显卡请求。
- LUT 库的旧格式升级在启动时做（`app._migrate_luts`）；只读接口按 `persist=False` 读库，不写盘。

### 照片文件的打开（`library.media`）

`/raw`、`/camera-jpg` 和 RAW 布局解析共用 `open_media`：路径必须是图库根下面的相对路径；图库根按 realpath 确定，并核对不在卡上或受保护目录里。
POSIX 从图库根逐级 `O_DIRECTORY|O_NOFOLLOW` 打开，最后一级 `O_NOFOLLOW`，任何一级是链接都失败，核对和打开是同一次操作；
Windows 逐级 lstat 拒绝链接和重解析点，打开后核对 (st_dev, st_ino) 和 fd 的真实位置（`GetFinalPathNameByHandleW`）。
之后只用这个 fd：`FdResponse` 按块发送，支持单段 Range（206 / 416），RAW 布局从同一个 fd 读字节，不按路径重开。

### 版本和失效（`web/assets/gpu_stage.js`）

- 烘焙表的地址和键带相机数据版本（`dv=`）；LUT 表的键带 LUT 的 render token（空间版本）。
- `camdata-changed`、`luts-changed`（LUT 库改了空间、删了 LUT）让前端换代：旧代的请求不管成功还是失败，回来以后都丢掉，
  不装进引擎，也不把新代标成失败；新代重新取表。
- LUT 表还在算（第一次几秒）或取表失败（422 等）时这一帧回到服务器出图，沿用服务器原来的错误提示。
- 引擎里同一个键再上传烘焙表或 LUT 表，先销毁旧的显卡资源；烘焙表最多留 24 张，LUT 表最多 3 张（精确表每张 64 MB）。

### 着色器

着色器按 Python 逐项对应：

| 着色器 | 对应的 Python | 做什么 |
| --- | --- | --- |
| `bin.wgsl` | `half_res_codes` + `orient_array` | RAW 合并、转正 |
| `common.wgsl` | `chain._chroma_chunk`、`tones`、`lut.spaces.Target.render`、`finish.pixels` | 颜色链 |
| `geo.wgsl` | `finish.geometry` 的 `_straighten_coords`、`_sample`、`_rot_flip` | 拉直、旋转、翻转、裁切 |

每个 Python 落成 uint8 的地方，着色器都照样取整。

`tests/test_gpu_parity.py` 用合成数据在无头 Chrome 的 SwiftShader 上逐像素对拍（含复审给的两个全合成 LUT 反例）。门槛在 `tests/gpu_gate.py`：
每个用例 ≤1 码值的像素 ≥ 99.99%、平均 ΔE2000 ≤ 0.01（未舍入计数）；在 CPU 有效区蒙版上取边界内外各一像素的边缘带（随拉直、旋转、翻转、裁切变换），
带外最大差 ≤ 4，>2 码值的像素只能出现在冻结的位置上、并由前向量化复算（第一段 uint8 偏 1 码值后走生产 `finish_array`）逐字节重现；
带内 >2 码值的像素 ≤ ceil(N×10⁻⁶)。显示类 LUT 的用例不允许冻结豁免。

### 回到服务器出图的情况

以下情况仍由服务器出图，前端不画近似结果：

- 原图（按住空格）；
- 透视、畸变；
- 成片里有边框或时间戳（裁剪页的未裁切画面照常走显卡）；
- 只有 JPG 却选了配方（近似）；
- Log 类输入的 LUT 带一维表，或输出空间着色器不会编码（如 V-Log）；Log 类 LUT 没有 DNG（`lut_needs_raw`）或没有场景色彩；
- LUT 表还没到（第一次计算期间）或取表失败；
- RAW 写法特殊：压缩、线性化表、逐行黑电平等；
- 浏览器没有 WebGPU，或显卡设备丢失。

### 预热

第一次请求某张照片的显卡信息时，后台把它所在单元、所在 ISO 模型的 8 个内置配方先烘焙好（`RenderService.warm_preview_bakes`）。浏览器同时预取左右相邻照片的源图和配方表；
预取按照片和它的编辑版本（`sidecar_rev`）去重，拖滑块不会重复预取。

## 平台层（`osplat`）

POSIX（macOS、Linux）和 Windows 的差别都收在 `osplat`：文件锁（`flock` / `msvcrt.locking`，进程退出系统自动放锁）、fd → 真实路径
（`F_GETPATH` / `/proc/self/fd` / `GetFinalPathNameByHandleW`）、完整落盘（`F_FULLFSYNC` / `fsync`）、拟合子进程看父进程还在不在
（POSIX 看 `getppid` 有没有变；Windows 不过继，改为拿住父进程句柄看它结束没有）。

**按目录句柄的文件操作**：`PrivateDir`、读卡的 `CardReader` 和照片文件的 `library.media` 只通过 `osplat` 的原语碰文件系统
（`open_dir`、`open_dir_at`、`open_file_at`、`lstat_at`、`listdir_fd`、`mkdir_at`、`unlink_at`、`rmdir_at`、`rename_at`），
两个平台走同一套上层代码，句柄一律是整数 fd（Windows 上目录句柄也包成 CRT fd）：

- POSIX：`dir_fd` + `O_DIRECTORY|O_NOFOLLOW`，文件 `O_NOFOLLOW`；
- Windows：`NtCreateFile` 以父目录句柄为根相对打开，带 `FILE_OPEN_REPARSE_POINT`，打开后看属性，重解析点一律当链接拒绝
  （云盘占位文件也会被拒绝，宁可退回服务器出图）；删除（`FileDispositionInformation(Ex)`）和改名（`FileRenameInformation(Ex)`，
  目标以父目录句柄为根）都按已经打开的句柄做；列目录按句柄（`GetFileInformationByHandleEx`）。这些调用不经过 `os.*`，
  原语里自己发出同名的审计事件，`audit` 的保护规则照常生效。

没有"按路径核对、再按路径操作"的实现，也就没有那段空隙。`make type` 同时按 win32 平台做类型检查；
`tests/test_platform_io.py` 的竞态测试在 CI 的 windows-latest 上跑的是 Windows 原语。
