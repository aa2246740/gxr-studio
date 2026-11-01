# GXR 电脑工作室

给理光 GXR 配套的本地网页工具：插卡导入照片、按日期整理、裁成 XPan 等比例、套配方看"电脑预测"、导出 JPEG。
所有东西都在你自己的电脑上，不联网，不往相机的卡上写任何东西。

## 三步开始用（macOS）

1. **双击 `studio/GXR Studio.command`。** 第一次会自动装依赖（需要联网，一两分钟），然后自动打开浏览器。
   - 提示"没有找到 uv"：打开「终端」运行一次 `brew install uv`，再双击。
   - 第一次双击如果 macOS 说"无法验证开发者"：在 Finder 里右键这个文件 → 打开 → 再点一次「打开」。
2. **把相机的存储卡插进读卡器。** 照片会自动读进来，按拍摄日期排好；卡上的文件一个都不会改。
   - **第一次读卡时 macOS 会问**"“终端”想访问可移除的卷上的文件"：点**允许**。如果点了"不允许"，工作室会一直说读不到卡，
     并在页面上提示你：打开「系统设置 → 隐私与安全性 → 文件和文件夹」，给“终端”打开「可移除的卷」（或在「完全磁盘访问权限」里打开“终端”），再把卡拔出来插一次。
   - 导入结束只有在**一张都没漏**时才会说"可以安全弹出卡了"；有读不出来的文件会用红色提示列出文件名，并建议先别格式化卡，换个读卡器或重新插拔再点「继续导入」。
   - 每次插卡都会读一遍卡上照片的内容来核对（160 张约 3 GB，大约 40 秒）：只有每个文件的内容都确实在图库里，才说"这张存储卡上的照片都已导入"。
     只按文件名、大小认出来的不算——两张卡可能有同名同大小的照片。卡的 DCIM 里有工作室不会导入的其他文件时，会说"存储卡上有 N 个文件不会被导入"并列出名字。工作室从不说"可以格式化"。
   - 相机视频（`.AVI`）原样复制进图库的日期文件夹，不解码，不在照片网格里显示。
   - 在访达里看过卡时 macOS 留下的 `._*`（要真是 AppleDouble 格式）、`.DS_Store` 等系统文件不导入，也不影响"都已导入"。
     能认出是照片或视频的文件不算系统文件，不管叫什么名字、放在哪个文件夹里（例如废纸篓里的照片会算作"不会被导入"）。
   - 判断照片"已在图库里"看的是图库文件现在的内容：图库里的副本不在了、被替换或被截断，再导入会把卡上的原件放回原位置，
     被改过的那个文件改名成 `<原名>.modified-<时间>` 留在旁边。
   - 图库里的文件夹（或文件）如果是指到存储卡上的链接，工作室不往那里写，也不把卡上的文件当成电脑上的副本：
     这些照片算"未能导入"，提示请勿格式化存储卡。
   - 启动时，在第一次写图库之前就会核对：图库文件夹本身、或其中的 `.studio`（索引、锁、日志、缩略图等）在存储卡上，
     或经过链接指到了图库以外的位置，工作室不会打开，启动窗口里会说明原因（退出码 78）。请把图库放在电脑上的普通文件夹里。
   - 同时插着几张卡时一次只处理第一张，页面会提示；处理完拔掉它再处理下一张。
3. **点开一张照片**：「裁切」里选 XPan 65:24；「配方」「调整」里调颜色和曝光；「导出」存成 JPEG。
   想看配方的颜色预测，需要先导入相机数据（「配方」标签里点「从卡导入」，卡上要有 `GXRCE.DAT`），
   再导入颜色标定——后者目前只有开发机能做（见下面"第一次使用"），其它功能不受影响。

用完关掉那个黑色的终端窗口（或按 Control-C），工作室就停了。同一个图库只能开一个工作室：再双击一次会提示"工作室已经开着了"，回到第一个窗口继续用就行。

---

## 更详细的说明（开发者）

设计依据：`../research/studio_design/`（ARCHITECTURE / UX / DECISIONS）。集成走查：`../research/studio_integration/`。

## 安装与运行

```bash
cd studio
uv sync --frozen            # 按 uv.lock 装依赖（Python 3.12–3.14，默认 .python-version = 3.14）
uv run gxr-studio           # 启动，自动打开浏览器；加 --no-browser 只打印链接
```

开发时也可以 `make setup`（装依赖并挂上检查钩子）然后 `make dev`。命令、命名和不能碰的目录写在 `AGENTS.md`。

- 只监听 `127.0.0.1`，端口随机；启动时打印 `http://127.0.0.1:<端口>/?token=…`，必须用这个链接打开
  （令牌会换成 HttpOnly cookie）。服务还校验 `Host` 和 `Origin`，所有响应带
  `Content-Security-Policy: default-src 'self'; img-src 'self' blob: data:; …`（页面不加载任何外部资源，也没有内联脚本/样式；
  图片放行 `blob:` 是因为预览图是 fetch 之后用 object URL 显示的）。
- 同一个图库同时只允许一个实例：启动时在 `<图库>/.studio/studio.lock` 上拿 `flock`（进程结束系统自动放掉），
  第二个实例打印"工作室已经开着了"并以退出码 75 退出（`GXR Studio.command` 据此不再多说"已停止"）。
- 默认图库 `~/Pictures/GXR Studio`，默认导出目录 `~/Pictures/GXR 导出`，应用数据目录
  macOS `~/Library/Application Support/GXR Studio/`。可以用环境变量改：
  `GXR_STUDIO_LIBRARY`、`GXR_STUDIO_EXPORT`、`GXR_STUDIO_HOME`、`GXR_STUDIO_VOLUMES`（存放"卡"的卷根目录，
  多个用 `:` 分隔，默认 macOS 是 `/Volumes`）、`GXR_STUDIO_TOKEN`。
- 错误追踪默认关闭。只有设置了 `GXR_STUDIO_SENTRY_DSN` 才会把失败任务报到 Sentry，并且会先打码路径和令牌。
  可选 `GXR_STUDIO_SENTRY_ENVIRONMENT`（默认 `local`）和 `GXR_STUDIO_SENTRY_RELEASE`（默认 `gxr-studio@版本`）。
  不设置 DSN 时不会联网。说明在 `docs/observability.md`。

### 不插真卡也能试：造一张假卡

```bash
uv run python -m gxr_studio.devtools.synthetic_card /tmp/fake-volumes      # 在 /tmp/fake-volumes/NO NAME 下造卡
GXR_STUDIO_VOLUMES=/tmp/fake-volumes GXR_STUDIO_LIBRARY=/tmp/fake-lib GXR_STUDIO_EXPORT=/tmp/fake-exp uv run gxr-studio
```

假卡里有 DNG+JPG 一对、只有 JPG、只有 DNG、EXIF 日期乱码、没有 EXIF、竖拍、同名不同内容、不同名同内容、
系统垃圾文件；默认还会把 `captures/a12-50/2026-10-19/stage2c-first-run/` 的 4 对真实样本复制进去
（加 `--no-real` 跳过）。

## 第一次使用：导入相机数据和标定包

套配方、看"电脑预测"的颜色要用两样本地数据。它们**只存在你机器的应用数据目录里**（macOS
`~/Library/Application Support/GXR Studio/`，`GXR_STUDIO_HOME` 可改），不在仓库里，也不随应用分发。
没导入时，预览和导出返回 409（`camera_data_missing` / `camera_data_invalid` / `calibration_missing`），
照片仍然能导入、浏览、裁切；只有 RAW 的照片显示中性转换。

**1. 相机数据（`GXRCE.DAT`）** —— 来自你自己装过配方程序的那张卡，在卡的最外层（和 `DCIM` 并排）。

- 插着卡：`POST /api/camdata/import-card`（可选 `{card_id}`；插着不止一张带 `GXRCE.DAT` 的卡会返回 409 `multiple_cards` 和候选）。
  **只读**：只以 `rb` 打开这一个文件，卡上的东西一个字节都不会动。
- 没有卡：把 `GXRCE.DAT`（或装着它的文件夹）拷到电脑上，`POST /api/camdata/import-file {"path": "/完整/路径"}`。
  界面里的「选择 GXRCE.DAT 文件…」走 `POST /api/camdata/import-upload`：浏览器拿不到本地路径，所以直接把选中文件的
  原始字节（`application/octet-stream`，文件名放在 `X-GXR-Filename` 头里，只用来显示）发过来，校验和 `import-file` 完全相同。
- 提取之前先全部校验：魔数、代数（目前认 127 / 129 / 131 / 135 / 137 / 138 / 150 / 152 / 153，其它代一律拒绝，不猜）、两层 CRC、钉子、
  重新封装后与原文件逐字节相同、三个单元的原厂 04。任何一项不过，返回带中文说明的 422
  （`generation_unsupported` / `dat_invalid`），应用数据目录里原来的数据原封不动。
  文件版本比工作室当前支持的更新时，主提示为「这个相机配方文件需要更新版本的 GXR 工作室。」；
  其它不支持的版本显示「不支持这个版本的相机配方文件。」。主提示不列代数，具体代数及支持列表只放在 API 的 `detail` 中供诊断。
  卡上没有这个文件是 404 `dat_not_found`，没有卡是 404 `no_card`。
- 通过后提取 3 个单元（50mm / 28mm / MOUNT）× 8 个配方 + 原厂 04 到 `camera-data/`，写入后读回比对。
  内容和已导入的完全相同就什么都不做（`unchanged: true`）。
- 152 / 153 的 Looks 从文件里的 GXFX 行池读取，档位和整数剂量从 kv2 R3 点表读取。配方页按顺序显示
  8 个内置配方、文件里的 Looks、文件里的「我的配方」。150 沿用 GXDP v1 / kv2 R2，没有 Looks，内置配方和控制值与 152 相同。
  文件内条目按内容摘要识别，默认值沿用文件；导入失败不会替换已导入的数据。

**2. 标定包** —— 电脑预测用的校准数据（ISO 200 / 1600 两套），**必须在相机数据之后导入**，
导入时用包里的 stock 核对相机数据里 50mm 的原厂 04，对不上就拒绝（422 `calibration_stock_mismatch`）。

- `POST /api/calibration/import {"path": "/完整/路径/文件夹"}`。文件夹里是 `calib-pack.json`（清单，列出每个文件的 SHA-256）
  加上数据文件；先逐个核 SHA-256、复制到临时目录，再从这份副本导入（核对的和导入的是同一份字节）。
  `intake.write_pack(目录, {iso: (npz, ring_json)})` 能把一套数据整理成这种文件夹。
- **目前的限制（开发阶段）：** 标定数据只在开发机的研究目录里有（`research/color_fit_v2/…`、`research/hue_ring/…`，
  理光派生的部分是本地文件，不在 git 里）。所以接口也接受**仓库根目录或 `research/` 目录**作为 `path`（返回里 `kind: "dev"`）。
  **别的用户以后需要另外拿到标定包——如何制作和分发它还没有决定**，现在没有给最终用户的标定办法。
  界面入口：右上角「设置」→「颜色标定（开发用）」，路径框已经预填了工作室所在的项目文件夹（`GET /api/camdata/status` 的 `project_dir`，
  也可以点「用工作室所在的项目文件夹」），点「导入颜色标定（开发用）」；
  界面上写明了"这一步目前只在开发机上可用"。相机数据已导入、标定还缺时，「配方」「导出」里的引导也会指到这里。
- 之后如果相机数据换成了原厂 04 不同的版本，状态里标定会变成 `invalid`（"请重新导入标定包"），不会悄悄继续预测。

`GET /api/camdata/status` 一次给全：是否可以出图（`ready`）、相机数据的来源（卡的卷标或"文件"、文件名、大小、SHA-256、代数、导入时间）、
三个单元的 8 个槽位和原厂 04 是否齐全、标定状态、插着的卡里哪张有 `GXRCE.DAT`、下一步该做什么（`next`）和 `data_version`。

导入成功后：渲染结果和烘焙缓存清空；没有 JPG 的照片的缩略图/预览缓存删除重画，`/api/photos` 里这些照片的
`thumb_url` / `preview_url` 带上 `?v=<data_version>`，浏览器缓存的旧图（`immutable`）因此不会再被用到。
前端在 `data_version` 变化时（`camdata.js`）重新取照片列表，并刷新大图的图片地址和编辑状态。
照片的配方能力按数据版本缓存；数据更新后清空能力和配方菜单的缓存及在途请求记录。
旧请求完成时，原调用者会重新读取当前版本的数据，不会收到旧能力或旧菜单。拍摄信息缓存保留。

## 跑测试

```bash
cd studio
uv run pytest                    # 全部用例（约 5–6 分钟）；只跑导入相机数据的：tests/test_camdata_api.py（40 个，约 5 秒）
uv run pytest tests/test_import.py -k kill     # 只跑"随机杀进程 20 次"
uv run python tests/perf/measure_m1.py         # 1000 张合成照片的后端数字（约 15 秒），写入 tests/perf/baseline.json
```

**测试绝不会碰真实的卡。** `tests/conftest.py` 在整个测试进程里：

- 禁止读/写/列出 `/Volumes` 下除 `Workspace`、`Macintosh HD` 以外的任何卷（包括 `/Volumes/RICOHDCX`），碰一下就抛 `PermissionError`；
- 保护真实的 `~/Pictures/GXR Studio`、`~/Pictures/GXR 导出`、`~/Library/Application Support/GXR Studio`（写入即拒绝），
  并把 `HOME` 换成临时目录；
- 禁止联网（`socket.connect`/`getaddrinfo` 直接拒绝）；
- 测试只用 `tmp_path` 里的合成卡。

## 目录

```
src/gxr_studio/
  app.py  cli.py  config.py  security.py  audit.py  jobs.py   装配、令牌/Host/Origin、审计钩子、后台任务
  meta.py                  EXIF / DNG 文件头（日期、尺寸、方向、ISO、镜头、DNG 内嵌预览）
  crop.py  export.py       裁切几何（纯函数）、导出 JPEG/PNG（EXIF + sRGB ICC；只有 RAW 时出"中性转换"）
  raw/                     DNG 读取、白平衡码值、半分辨率（2×2 合并）、MHC 去马赛克、渲染器接口（S4）
  cards/                   discovery（发现卡、配对）、importer（导入）
  lut/                     LUT 导入（S8a）：cube/spaces/colorimetry（来自研究，PROVENANCE.json）、scene（色样）、fit（路线 1 旋钮拟合）、
                           service（任务与"我的配方"）、worker（拟合子进程）
  camdata/                 datcodec（GXRCE.DAT）、store（相机数据；`swap_in` 原子换目录，启动时 `recover_interrupted_swaps` 恢复被打断的切换）、calib（标定）、intake（导入入口：校验、卡上只读取、标定包）
  library/                 store（目录 + SQLite 索引 + 重建）、sidecar、edits（裁切读写）、thumbs（缩略图与"画面是什么"的标注）、lock（图库启动锁）
  api/                     routes.py、render_routes.py、camdata_routes.py、lut_routes.py、errors.py、temp_home.html（临时首页）
  devtools/synthetic_card.py   测试夹具生成器
web/                       正式前端放这里（有 index.html 时 `/` 就返回它，`web/assets/` 挂在 `/assets`）
tests/                     pytest；tests/perf/ 是性能基线
```

## 接口（都要令牌；JSON；错误统一 `{"error": 代码, "message": 白话}`）

| 方法 路径 | 作用 |
|---|---|
| `GET /api/health` `GET /api/settings` `GET /api/audit` | 基本信息；审计记录（最近的写入） |
| `GET /api/cards` · `GET /api/cards/{id}/summary` | 发现的卡：`{id, label, uuid_state}`。`id` 是这次挂载的会话号（26 个字符，服务器随机生成；卡拔出、换卡、重新挂载、重启后作废），`uuid_state` = ok｜missing｜ambiguous；摘要只列目录不读内容，所以证明不了"已导入"：`new_shots` 是缺少可信证明的张数，`verified` 恒为 false |
| `GET /api/cards/status` | 没找到卡时用：`access_denied` = 系统不让读（macOS 没给「可移除的卷」权限），`message` 是白话说明 |
| `POST /api/import` `{card_id, shift_seconds?, force_verify?}` → 202 任务 | 开始导入（卡只能按会话号选，不接受路径；格式不对 400 `bad_card_id`，会话作废 404 `card_not_found`） |
| `GET /api/jobs` · `GET /api/jobs/{id}` · `POST /api/jobs/{id}/cancel` | 轮询进度（`progress.state`：scanning/running/checking/done/paused_no_space/interrupted/cancelled/failed）。导入进度里 `unreadable` / `verify_failed` / `scan_errors` 是没读进来的文件（或文件夹），`write_refused` 是图库位置按真实路径不在图库里（例如链接到卡上）、所以没写的文件，`failed_shots` 是受影响的照片张数，`completed_shots` 是之前只导进来一半、这次补全的张数，`imported_videos` 是复制进来的视频；**这几个列表任何一个不空，`message` 就不会说"可以安全弹出"**。`coverage` 是结束时整张卡的照片核对：`{complete, dcim, total, covered, uncovered, not_importable, system_files, proof:{content, trusted_seen}, all_imported}`（`system_files` 是已忽略的 macOS 元数据），只有 `all_imported` 为真才说"都已导入" |
| `GET /api/dates` · `GET /api/photos?date=YYYY-MM-DD｜unknown&limit&offset` · `GET /api/photos/{id}` | 日期列表、照片列表 |
| `GET /api/photos/{id}/thumb` · `…/preview` | 256 / 1600 px 的 JPEG。只有 RAW 时画的不是相机 JPG（`preview_kind=dng-preview`）：已导入相机数据就是原厂 04 的**电脑模拟**，响应带 `X-GXR-Prediction: 1`、`X-GXR-Look: stock04-prediction` 和 `X-GXR-Label`（百分号编码的标签）；没有相机数据是中性转换（`X-GXR-Look: neutral`，不算预测）；半分辨率读不出来才退回 DNG 内嵌小预览（`X-GXR-Look: embedded`）。照片列表里的 `preview_look` = `{kind, label, prediction}` 给界面标注（图库角标、大图下面的说明） |
| `GET/PUT/DELETE /api/photos/{id}/crop` | 裁切：`{preset, rect:[x,y,w,h], angle_deg, flip?, expected_rev?}`；不给 `rect` 就取该比例居中的最大框 |
| `GET /api/crop-presets` | 比例预设：original、3:2、4:3、1:1、4:5、16:9、16:10、1.91:1、2.35:1、xpan-65-24、free（社交媒体按钮复用这些比例，横竖由 `flip` 决定） |
| `POST /api/photos/{id}/export` `{long_edge?, quality, format?, size?, quality_tier?, dest_dir?}` | 导出一张，`format` = jpeg｜png，`size` = large/medium/small/original（3000/2048/1600/原始），`quality_tier` = high/medium/low（95/85/70）；文件名 `原名_gxr.jpg`，重名加 `-2`、`-3`。只有 RAW 时导出"中性转换"（白平衡后的线性数据 → sRGB，不加配方），文件名 `原名_gxr_neutral.jpg`，返回里 `render` = neutral-preview、`render_label` = 中性转换；RAW 读不出来才返回 409 `no_jpg_source` |
| `POST /api/export` `{ids｜date, long_edge?, quality, dest_dir?}` → 202 任务 | 批量导出；`ids` 里已经不存在的照片（例如补全另一半后 id 变了）不会让整批失败，而是在结果里记为 `photo_not_found`，其余照常导出 |
| `POST /api/exports/{export_id}/reveal` | 在访达中显示这次导出的第一张。`export_id` 是单张导出返回的 `export_id`，或批量任务的 `id`。不接受路径参数。不是 macOS 时返回 501 |
| `POST /api/library/rebuild?verify=` | 丢掉索引，按目录和 sidecar 重建；读不出来的 sidecar 先改名成 `<名>.gxrs.json.corrupt-<时间>` 留着再建新的（返回里 `corrupt_backups` 列出） |
| `GET /api/camdata/status` | 相机数据/标定是否已导入、来源、三个单元是否齐全、插着的卡里哪张有 GXRCE.DAT、下一步 |
| `POST /api/camdata/import-card` `{card_id?}` | 从插着的卡上**只读**取出 GXRCE.DAT 并导入（见上面"第一次使用"） |
| `POST /api/camdata/import-file` `{path}` | 从本地 GXRCE.DAT（或装着它的文件夹）导入 |
| `POST /api/camdata/import-upload`（请求体 = 文件字节） | 界面选中的 GXRCE.DAT，校验同上 |
| `POST /api/calibration/import` `{path}` | 从本地文件夹导入标定包（要先有相机数据；开发阶段可指向仓库根或 `research/`） |

LUT 导入（路线 1 旋钮拟合，数字一律是"电脑预测"；28mm / MOUNT 没有标定，拒绝）：

| 方法 路径 | 作用 |
|---|---|
| `GET /api/lut/spaces` | 可选的输入/输出色彩空间（sRGB / Rec.709 / Gamma 2.4 / F-Log / F-Log2 / L-Log / V-Log / S-Log3 / Apple Log / 线性） |
| `POST /api/lut/upload`（请求体 = .cube 字节，`X-GXR-Filename` 放 URL 编码的文件名） | 校验并存进应用数据目录 `luts/`，返回 `cube_id`（SHA-256）、尺寸、按文件名猜的输入空间 |
| `GET /api/lut/{cube_id}/preview?which=original｜lut&photo=&space=&output=` | 原图 / 加 LUT 后（有相机数据 + 照片：04 电脑模拟上套 LUT；否则中性转换/示例照片） |
| `POST /api/lut/fit` `{cube_id, space, output, profile?}` | 开始拟合（独立子进程，同时只跑一个），返回 `job_id` |
| `GET /api/lut/fit/{job_id}` · `POST /api/lut/fit/{job_id}/cancel` | 进度 / 结果（`state` = running｜done｜cancelled｜error）；取消会结束子进程 |
| `GET/POST /api/my-recipes` · `GET/PUT/DELETE /api/my-recipes/{id}` | 电脑端的"我的配方"（`my_recipes/<id>.json`）；`POST {job_id, candidate, name}` 从服务器上的结果里取，不信客户端给的档位 |

**编辑记录（sidecar）坏了或被删了**：读的接口（裁切、编辑、预览、导出）当作"没有编辑"继续，响应里带 `warning`（白话，不含路径和解析错误）；
写的接口（保存裁切/编辑）先把坏的改名留着（`.corrupt-<时间>`），重建一份"没有编辑"的再写，同样带 `warning`；
真读写不了（权限、磁盘）才返回 422 `sidecar_unreadable`/`sidecar_corrupt`。批量导出里单张失败的提示是"这张没导出成功…（错误代码：XXX）"，不带异常原文。编辑文件里 `rev`/`frame`/`crop` 的类型不对（手改过）也按“编辑文件损坏”处理：备份成 `.corrupt-<时间>`，当作没有编辑。

裁切约定：`rect` 归一化到按方向转正后的画面；`angle_deg` 正值 = 顺时针，范围 ±10°；`rect` 定义在旋转后的画布上，
四个角必须都在原画面之内。XPan 65:24 在 4288×2848 上是 4288×1583，默认居中，`rect` 的 `y` 可以拖。

## 目录与数据

```
<图库>/YYYY/YYYY-MM-DD/Rxxxxxxx.DNG|JPG + Rxxxxxxx.gxrs.json（sidecar）
<图库>/未知日期/…                 （undated_policy=unknown_folder 时）
<图库>/.studio/                   index.sqlite（缓存，可删）、thumbs/（可删）、staging/、tmp/、import-log.jsonl（追加日志，勿删）
```

- 没有 EXIF 拍摄时间（或日期不合理）时：默认退回**文件修改时间**归档，并在索引里标 `date_source=mtime`；
  `Settings.undated_policy="unknown_folder"` 改成放进"未知日期"。
- 同名但内容不同：整对文件一起加 `_<sha8>` 后缀，不覆盖。
- 内容哈希相同的文件只留一份（即使文件名不同、来自不同的卡）。
- 对卡只以 `rb` 打开；被发现的卡根目录会登记进审计钩子，任何写入都会被记录并拒绝。
