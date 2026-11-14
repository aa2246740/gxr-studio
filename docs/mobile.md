# 手机版的地基

手机版是 Safari 里的一个网页，不装应用，也不连电脑。照片留在手机上。这一层先把「读 DNG、对上 Python、在网页里画出中性预览」跑通。后面的移植都接在这里。

## 四块分别放哪

| 部分 | 位置 | 做什么 |
| --- | --- | --- |
| 显卡预览 | `frontend/` | 现成的 TypeScript + WGSL。手机页直接 import 源码，不复制，也不改这个目录 |
| Rust 核心 | `core/` | Python 在 CPU 上算的东西。编成两个产物：原生 `gxr-cli`（只给对拍测试），和给手机用的 WebAssembly |
| 手机网页 | `mobile/` | 触摸界面，没有 UI 框架。WASM 跑在几个 Web Worker 里，画面交给现成的 `Engine` |
| 托管 | 以后的 GitHub Pages | 只放代码。照片、相机数据和校准文件不出手机 |

## 命令

在仓库根目录跑。`cargo` 用 `core/rust-toolchain.toml` 里的 1.93.1，并且把 rustup 那份放在 PATH 最前面。Homebrew 的 rustc 没有 wasm32 标准库。

| 命令 | 做什么 |
| --- | --- |
| `make core` | `cargo build --release -p gxr-cli`，产物 `core/target/release/gxr-cli` |
| `make core-test` | `cargo fmt --check`、`cargo clippy --all-targets -- -D warnings`、`cargo test` |
| `make core-wasm` | 用 `profile.wasm`（和 release 一样，另外 `panic = abort`）编 `gxr-wasm`，再 `wasm-bindgen --target web` 写到 `mobile/src/wasm-pkg/` |
| `make mobile-dev` | 先编 WASM，再在 `http://127.0.0.1:5199/` 开开发服务器 |
| `make mobile-build` | 生产构建，输出 `mobile/dist/` |
| `make mobile-check` | 构建，再 `tsc --noEmit`，并确认产物里没有 `__dev` |
| `make check` | 原来的检查加上上面四项，测试时设 `GXR_REQUIRE_CORE=1` |

没编 `gxr-cli` 时，普通 `make test` 里的对拍会跳过并写明原因。`make check` 不允许跳过。

第一次在一台新机器上：`make setup`（有 rustup 就补 wasm32 目标；没有就只打印怎么装，不会失败），然后

```bash
cargo install --locked wasm-bindgen-cli --version 0.2.130
pnpm -C frontend install --frozen-lockfile
pnpm -C mobile install --frozen-lockfile
```

`wasm-bindgen-cli` 必须和库同一个 0.2.130。

## 对拍测试怎么写

Python 是对照的标准。测试在 `tests/test_core_parity_<范围>.py`，调用 `tests/support/core_cli.py` 里的 `gxr-cli`，读它的 JSON 或 `.npy`，和同一个输入上的 Python 函数比。

- 整数、字符串、形状、dtype、错误种类：必须一样。
- 错误文案也必须一样。Python 的 `RawDecodeError` 对应 Rust 的 `kind == "raw_decode"`。
- `black`、`white`、`wb`、`scene.A`、`scene.b`：`|a−b| ≤ 1e-12·max(1,|a|)`。这是给小矩阵乘积留的，别处不要用。
- 半分辨率码值、以及任何存成 float32 的表：float32 逐位相同（`np.array_equal`）。
- 马赛克和别的整数：每一个都相同。
- 比的是解析后的数，不是 JSON 文本。Python 和 Rust 印浮点的样子不一样。WASM 和原生 CLI 才比字节，因为两边都是 Rust。
- 对不上就停，记下输入、下标和两个值。不要放宽比较。

坏文件的文案分两类。下面这些和 numpy / Python 的原文一致：`array is too big; arr.size * arr.dtype.itemsize is larger than the maximum possible size`（numpy 原文在 `arr.size * arr.dtype.itemsize` 两边还有反引号）、`can only specify one unknown dimension`、`negative dimensions are not allowed`、`RAW 数据读不全（raw strips shorter than the image）`、条带偏移越过文件末尾时的 `offset must be non-negative and no greater than buffer length`、以及 `不是可识别的 GXR DNG（ValueError: 不是 TIFF/DNG 文件）`。

打开失败时 kind 是 `raw_decode`，整句消息和 Python 逐字相同，reshape 的形状文本也一样：Rust 照 numpy 的 `convert_shape_to_string` 印，开头的未知维度不印（`[-1, 3]` 印成 `(3)`），后面的未知维度印成 `newaxis`（`[3, -1]` 印成 `(3,newaxis)`），只有一维时带逗号。测试逐字比较。

元素个数超过 2**32 - 1、但字节数仍小于 numpy 的 2**63 - 1 上限时，原生和 WASM 都拒绝，消息用上面的 array is too big。条带也一样：每段都在文件里、但累计超过 2**32 - 1 字节时（例如很多段指向同一块数据），先逐段核对，再按总数拒绝，不去申请缓冲区。64 位 numpy 这时可能去分配并抛 MemoryError。测试里的 2**62 个黑电平两边都是 numpy 自己拒绝的那句。

半分辨率在分配输出之前先核对条带。条带不够时 Rust 返回 `RAW 数据读不全（…）`。Python 的 `np.empty` 有时会先抛 `array is too big`（裁切窗口本身就放不下的时候）。这是有意的顺序：先确认文件里有这些字节，再申请缓冲区。

真照片从 `GXR_STUDIO_TEST_PARENT/captures/` 里找，路径含 `backup` 的不要，最多 60 张，按路径排序后均匀抽取。不要把路径写死，也不要把 DNG 放进仓库。合成文件在测试里用 `tests/support/dng_writer.py` 现造。

## 移植一个 Python 模块

1. 选负责的 crate。已经留好的空 crate 见下面的表。不要改 `core/Cargo.toml`。
2. 一个 Python 函数对一个 Rust 函数，名字用 snake_case。注释写 `/// Port of gxr_studio/<路径>.py:<行号> <函数名>`。运算顺序和每一步的 dtype 都照着 Python 来。
3. 在自己的 `core/crates/gxr-cli/src/cmd_<领域>.rs` 加子命令。数组写成 `.npy`（小端、C 顺序、格式 1.0），其余走 JSON。核心错误打印 `{"error":{"kind","message"}}` 并退出 2，用法错误退出 1。
4. 加 `tests/test_core_parity_<范围>.py`，用 `tests/support/core_cli.py`。
5. 需要网页用到的函数，写在 `core/crates/gxr-wasm/src/<领域>.rs`，比较写在 `mobile/scripts/wasm_cases/<领域>.mjs`。不要改驱动 `wasm_vs_native.mjs`。
6. 先 `make core-test`，再跑对应的 pytest，最后 `make check`。

`#![forbid(unsafe_code)]`。坏输入返回 `Result`，不要 panic。`CoreError.kind` 用稳定的短字符串。

Exception: `gxr-jpeg` may depend on `jpeg-encoder` (owner/manager decision 2026-11-13).

## 为什么原生和 WASM 必须逐位相同

两边要给出同一个字节。所以核心代码不调用标准库的 `exp`、`ln`、`log2`、`log10`、`powf`、`powi`、`sin`、`cos`、`tan`、`atan2`、`cbrt`、`hypot`、`exp2`（`sqrt` 可以）。这些走 `gxr_math`，里面是 `libm`。`core/clippy.toml` 会拦住直接调用。

也不要写 `mul_add`，不要去模仿 BLAS。在这台 Mac 上，numpy 的矩阵乘会通过 Accelerate 做融合乘加，而且形状不同融合得不一样，原生和 WASM 没法共用那套规则。点积、矩阵乘和多项式都按 Python 表达式的顺序，从左到右做普通的 IEEE 运算：`s = a0*b0; s = s + a1*b1`。复数乘法写成 `(ar*br - ai*bi, ar*bi + ai*br)`。最终存下来的 float32 和 uint8 才是对拍要的东西。

numpy 的取整照着做：

| Python | Rust 里要做成 |
| --- | --- |
| `np.rint` / `np.round` | 一半向偶数 |
| `astype(int)` | 向零截断 |
| `np.clip` | 先夹再转 |

Python 看起来算错了也先原样移植，把疑问写进报告，不要顺手改公式。

## JSON

`gxr-raw` 里的手写写入器是命令行和 WASM 共用的。键的顺序就是写入的顺序。浮点用 Rust 的最短往返格式（`{}` 打在 f64 上）。不要加 serde。

## 空 crate，各接哪一块 Python

| crate | 以后放什么 |
| --- | --- |
| `gxr-camdata` | `gxr_studio/camdata/`：DAT、机身校准、配方标签 |
| `gxr-color` | `engine/`、`render/` 的链和配方，以及 `gpu/tables.py` 的烘焙表 |
| `gxr-lut` | `gxr_studio/lut/` 的立方体、色彩空间和拟合。布局要用的 `scene` 和 `colorimetry` 已经在 `gxr-raw` |
| `gxr-hdr` | `gxr_studio/hdr/`：HDR、增益图和容器 |
| `gxr-finish` | 几何现在（`crop.py`、`finish/geometry.py`），光栅以后 |
| `gxr-frame` | 边框和戳记的布局：`spec`、`sfnt`、`layout`、`frame_core`、边框样式 |
| `gxr-jpeg` | Pillow 那条 JPEG 编码，以及 `gxr_studio/meta.py` |
| `gxr-export` | `export.py`、`raw/render.py`、`raw/demosaic.py` 的全分辨率流程 |

`gxr-raw` 已经有 TIFF/DNG、马赛克、半分辨率码值和显卡布局。`gxr-math` 是 libm 的薄包装。

## 开发壳

`make mobile-dev` 打开的页面可以选一张 DNG。Worker 里的 WASM 读布局，显卡引擎画预览。拖动曝光、对比度或饱和度只会调用 `setLook`。页面上写着「开发用中性预览，不是配方」：没有相机数据时，`mobile/src/dev/neutral_bake.ts` 造一张中性烘焙表（sRGB 编码乘 255，BT.601，增益网格全是 `[1, 0]`）。真正的配方表以后从 `gxr-color` 来。

没有 WebGPU 时（模拟器就是这样）仍会跑 WASM，并显示布局摘要，同时说明这个浏览器现在用不了显卡预览。

`?autotest=1` 只在开发服务器里生效。它会拉 `GXR_MOBILE_SAMPLES` 里的第一张 DNG，跑描述和 5 秒自动拖动，再把结果写到 `mobile/.reports/`。这些路径不会出现在 `vite build` 的产物里。

## 加一个领域

每个移植领域只改自己的文件。不要改 `core/crates/gxr-cli/src/main.rs`、`core/crates/gxr-wasm/src/lib.rs`、`core/Cargo.toml`，也不要改驱动 `mobile/scripts/wasm_vs_native.mjs`。兄弟 crate 的 path 已经在 `[workspace.dependencies]` 里，自己的 `Cargo.toml` 用 `workspace = true` 引用即可。

| 领域 | 命令前缀 | crate | 命令文件 | WASM 模块 | 比较用例 | 对拍测试 |
| --- | --- | --- | --- | --- | --- | --- |
| raw | `dng-` | `gxr-raw` | `cmd_raw.rs` | `gxr-wasm/src/raw.rs` | `wasm_cases/raw.mjs` | `tests/test_core_parity_raw.py` |
| camdata | `camdata-` | `gxr-camdata` | `cmd_camdata.rs` | `gxr-wasm/src/camdata.rs` | `wasm_cases/camdata.mjs` | `tests/test_core_parity_camdata.py` |
| color | `color-` | `gxr-color` | `cmd_color.rs` | `gxr-wasm/src/color.rs` | `wasm_cases/color.mjs` | `tests/test_core_parity_color.py` |
| lut | `lut-`、`scene-` | `gxr-lut` | `cmd_lut.rs` | `gxr-wasm/src/lut.rs` | `wasm_cases/lut.mjs` | `tests/test_core_parity_lut.py` |
| hdr | `hdr-` | `gxr-hdr` | `cmd_hdr.rs` | `gxr-wasm/src/hdr.rs` | `wasm_cases/hdr.mjs` | `tests/test_core_parity_hdr.py` |
| geometry | `geom-` | `gxr-finish` | `cmd_geometry.rs` | `gxr-wasm/src/geometry.rs` | `wasm_cases/geometry.mjs` | `tests/test_core_parity_geometry.py` |
| frame | `frame-`、`stamp-` | `gxr-frame` | `cmd_frame.rs` | `gxr-wasm/src/frame.rs` | `wasm_cases/frame.mjs` | `tests/test_core_parity_frame.py` |
| jpeg | `jpeg-` | `gxr-jpeg` | `cmd_jpeg.rs` | `gxr-wasm/src/jpeg.rs` | `wasm_cases/jpeg.mjs` | `tests/test_core_parity_jpeg.py` |
| export | `export-` | `gxr-export` | `cmd_export.rs` | `gxr-wasm/src/export.rs` | `wasm_cases/export.mjs` | `tests/test_core_parity_export.py` |

命令文件都在 `core/crates/gxr-cli/src/`。每个 `run(command, args)` 收到前缀已经对上的子命令，参数是命令名后面的那些。还不认识的子命令返回原来的用法错误 `unknown command`。`main` 负责 JSON / `.npy` 的打印、错误 JSON 和退出码 0/1/2。

WASM 模块里用 `#[wasm_bindgen]` 导出。`version()` 留在 `lib.rs`。

比较用例导出 `cases(inputs)`，返回 `{label, cliArgs, wasm}` 的列表。`wasm` 成功时返回 string（和 CLI 的 stdout 逐字比较）或 `Uint8Array`（和 `--out` 那个 `.npy` 的前两维加上样本字节比较）。失败时抛出核心错误的 JSON 文本。`label` 写成 `<文件>::<检查名>`，驱动会把它印成 `<文件>: <检查名> bytes differ`。

Python 侧用 `tests/support/wasm_check.py`：

```python
from support.wasm_check import run_area

proc = run_area("raw", paths, tmp_path)
assert proc.returncode == 0, proc.stdout + proc.stderr
```

`run_area` 把路径写成 JSON，执行 `node mobile/scripts/wasm_vs_native.mjs --area <领域> --cli <gxr-cli> --inputs <json>`。驱动失败时，默认用驱动自己的输出让测试失败。WASM 包或 `node` 不在时跳过，除非 `GXR_REQUIRE_CORE=1`。`GXR_REQUIRE_GPU_TEST` 不会把缺包变成失败。

## 边框和日期戳（frame）

`gxr-frame` 移植 `finish/sfnt.py`、`spec.py` 的解析、`layout.py`、`fonts.py` 的度量一半、`frame_core.py` 和三份样式（`frame_styles.py`、`frame_objects.py`、`frame_new.py`），以及 `logo_math.py` 里算尺寸的部分和 `stamp.format_stamp`。Rust 只出版面 JSON：每个框、每行字、每个字的列、颜色。字和形状由浏览器画，墨迹、发光、颗粒、纹理的像素不在对拍里，参数在 JSON 里，要相同。

字体都按字节传进来，crate 里不嵌字体文件。命令行写 `--font 名字=路径`，名字是 `atkinson`、`inter`、`inter_sb`、`courier`、`patrick`、`dseg`，中文回退字体叫 `cjk`。没给 `cjk` 而文字要中文字形时，错误是 `missing_cjk`（「无法显示中文。请改用英文或数字。」），和 Python 找不到系统字体时一样。不去看 macOS 的系统字体路径。

中文度量：Python 用 Pillow（FreeType，BASIC）在 1000 像素上量中文字，上沿按字号量。Rust 读回退字体的 CFF 字形程序取控制框，按 FreeType 的定点算法缩放、取整：`y_scale = FT_DivFix(px*64, upm)`，上沿 `FT_PIX_CEIL(FT_MulFix(ascender, y_scale))`，步进取整到像素，框包含原点和步进（Pillow 的 textbbox 从零开始）。测试拿 `tests/fixtures/NotoSansSC-Shanghai.otf` 和 Pillow 比：步进和上沿逐个相同。墨迹框不一样：Pillow 量之前 FreeType 先做 CFF 微调，Rust 用的是不微调的框（和 FreeType 的 `FT_LOAD_NO_HINTING` 相同），1000 像素上框的边差 1 个单位。Python 再把这个取整过的微调框按字号放大，所以日期戳上的差约为 px/1000。

中文字体本来就两边不同：桌面的中文回退是 macOS 系统字体（`stamp.py` 的 `_CJK_FONTS` 第一个是 PingFang），手机读不到也不能带，只能收一份 OFL 中文字体的字节（`--font cjk=…`）。对拍两边都用测试夹具 `NotoSansSC-Shanghai.otf`，验证的是同一字体上的排版算法，不是用户看到的结果。所以没有去做随字体附带的微调框表（经理 2026-11-14 裁定）。

门槛（经理在 Sol r3 之后裁定，2026-11-14）：只用于由中文墨迹框算出的字段——文字的 `x`、`y`、`xs`、`pts` 和按四条边比较的 `rect`：

```
|Δ| ≤ ceil(2 · px · δ / 1000) + 1
```

px 是日期戳字号。δ 由测试实测：同一组字形，Pillow 在 1000 像素下微调后的框和 Rust 在 1000 像素下的框逐条边比，取最大差，不写死。夹具字体测出来 δ = 1（「上」826／825，「海」842／841，「北」「京」也各差 1）。每条边最多差 px·δ/1000，派生字段（原点 `y − box[1]`、底边 = 顶 + 高）最多合两条边再取整一次，所以是上式。它对任何字号都成立，不需要求最大日期戳。其余字段（每个 px、颜色、文字、字体名、发光、类型）全部精确。三个真实几何复现（3148²、3302²、3315² 的正方形裁切，seg7、large、「上海上海上海」）各有一条测试：底边差 2、原点 y 差 1，都来自最高的「海」（顶 842／841，底 −74／−73）。桌面导出照旧用 Pillow 的框。`glyf` 字体做回退时用字形头的框，FreeType 在 1000 像素上的 TrueType 微调没有移植。

内置标志：crate 里没有理光字标的任何数据，不读也不提 `ricoh.png`，也不带它的尺寸。Leica 样式画内置标志时，标志框的比例和墨迹行由请求里的 `mark` 给出：`{"w", "h", "ink_top", "ink_bottom"}`，单位是标志自己的像素。标志高 `lh = max(14, round(gxr_cap × 1.04 × h / (ink_bottom − ink_top + 1)))`，宽 `round(w × lh / h)`，竖直位置用墨迹行的中点 `lh × (ink_top + ink_bottom) / (2h)`。对拍测试从 Python 读桌面的 `frame_core.LOGO_SIZE`、`LOGO_INK_Y` 填进去；手机填它用 Atkinson 写的「GXR」的度量，再在这个框里写字。只有画内置标志时才需要 `mark`，缺了就是 `bad_request`；不显示标志或用自定义标志时不看它。

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `frame-layout --meta M.json --spec S.json --font …` | `{"w","h","meta":{…},"logos":[LogoInfo…],"mark":{…}}` 和边框规格 | `FrameLayout.to_json()`，无边框时 `null` |
| `stamp-layout --meta M.json --spec S.json --font …` | 同上，另有 `"taken"`、`"file_time"`（`[年,月,日,时,分]` 或 `null`）、`"frame"`（边框规格，排在拍立得底边） | `StampLayout.to_json()`，没有可画的字时 `null` |
| `frame-batch --cases C.json --font …` | `{"frames":[{"request","spec"}…],"stamps":[…]}` | 同样形状的结果，单项出错写成 `{"error":…}` |
| `frame-sfnt --font PATH --chars TEXT` | 一个 TrueType 文件 | `read_font` 的表，加字形号 |
| `frame-cjk --font PATH --chars TEXT --px 1,2,…` | 中文回退字体 | 每个字的 `[步进, 框]` 和每个字号的上沿 |
| `frame-spec` / `frame-stored` / `stamp-spec --spec S.json` | 规格 JSON | 解析后的规格，或 Python 的错误 |

`stamp_layout` 不看 `enabled`，`apply_stamp` 才看；调用方照 `compose.finish_array` 自己决定什么时候用边框版本。`w`、`h` 限 1 到 65535（JPEG 的最大边长，手机导出 JPEG），Python 没有这个检查。超出时原生和 WASM 给同一个 `bad_request`；这个上限也让版面里的整数乘积远在 `i64` 以内，并限制样式能产生的项数（胶片的齿孔数随长边增长），不会在 wasm32 上耗尽内存。字体包的长度先按 u64 和剩余字节比，CFF 的 INDEX 偏移用检查过的加法，两边报同样的错。

JSON 输入（请求、规格、字体索引）最多嵌套 128 层，数组和对象一起算，最外层算第 1 层。每次进入下一层之前检查，超出时原生和 WASM 都报 `json`／「JSON 嵌套太深」，不会把 WASM 的栈用完。

WASM：`frame_layout(meta_json, spec_json, font_index_json, fonts)` 和 `stamp_layout(...)`。`gxr-wasm` 没有 `js-sys`，所以多个字体拼成一个字节数组，`font_index_json` 是 `[[名字, 字节数], …]`。返回值和命令行的 stdout 逐字节相同（含换行），错误抛出核心错误的 JSON。

对拍在 `tests/test_core_parity_frame.py`（中文墨迹框的门槛见上）：`FRAME_STYLES` 的每一种，`test_frame_reference` 的全部认可组合，`test_finish_layout_parity` 的尺寸、选项、拍摄信息和日期戳组合，中文题字，五种自定义标志的每个选项，1×1 这样的极端尺寸，以及缺中文字体。整数、字符串、布尔、键的顺序都要相同，浮点数要相等（不是接近），int 和 float 的类型也和 Python 一样。

## 相机数据（`gxr-camdata`）

`gxr-camdata` 移植了 `datcodec.parse`（含单文件 `pack` 往返校验、`refs` 别名解引用和 `_parse_looks`）、`r5.parse`（`GXRCE.DAT` + `GXRFX.BIN` 双文件）、`entries_from_dat`、`knob_table.read`、`recipe_tags.parse_recipe_tag` 和内嵌 `engine/data/final.json`。不把任何理光字节或表打进包。

命令行子命令（`cmd_camdata.rs`）：

- `camdata-parse --dat PATH [--library PATH] [--out-dir DIR]`：输出 `generation`、各单元 `slots` 与条目 `sha256`、`looks`、`recipes`、`users`、`has_kv2`、`kv2_sha256` 的 JSON；带 `--out-dir` 时另写出 24 × 552 字节条目到 `units/<unit>/recipes/<slot:02d>.entry`。
- `camdata-entries --dat PATH [--library PATH] --out FILE.npy`：把 24 × 552 字节写成 `(24, 552)` `uint8` 的 `.npy`。
- `camdata-menu [--dat PATH [--library PATH]]`：不带 `--dat` 返回内嵌 `final.json`；带 `--dat` 时按 `knob_table.read` 用文件内的 kv 块覆盖菜单。
- `camdata-knob-table --kv PATH [--mount-50mm]`：只给原生对拍用，不导出到 WASM。对独立的 kv2/kv3 块执行 `knob_table.read`（覆盖内嵌 `final.json`）；WASM 侧经 `camdata_menu` 走同一段代码。
- `camdata-parse-json --json PATH`：只给原生对拍用，不导出到 WASM。校验 JSON 输入：超过 4 MiB 报 `kind="json"` / `"JSON 文件过大"`，超过 128 层数组/对象嵌套报 `"JSON 嵌套太深"`。
- `camdata-recipe-tag (--tag TEXT | --tag-hex HEX)`：返回 `{"tag": ...}` 解析结果。

WASM 导出（`gxr-wasm/src/camdata.rs`）：

- `camdata_parse(dat_bytes, library_bytes?) -> string`：返回与 `camdata-parse` 逐字节相同的 JSON（末尾带换行），只含摘要与 `sha256`，不含原始条目字节。
- `camdata_entries(dat_bytes, library_bytes?) -> Uint8Array`：单独的字节读取接口，按 `(unit 0..2, slot 1..8)` 顺序返回连续的 `24 × 552 = 13248` 字节条目。
- `camdata_menu(dat_bytes?, library_bytes?) -> string`：返回解析后的菜单 JSON（末尾带换行）。

对拍在 `tests/test_core_parity_camdata.py` 与 `mobile/scripts/wasm_cases/camdata.mjs`；不把 `pack` 导出到 WASM。

## raw-render（全分辨率去马赛克与中性转换）

`gxr-raw` 的 `demosaic.rs` 和 `render.rs` 分别对应 `gxr_studio/raw/demosaic.py` 与 `gxr_studio/raw/render.py` 的中性路径：

- `demosaic_strips` / `demosaic_block`：先做 `reflect` 镜像延拓（不重复边缘样本），再用 5×5 MHC 核做不翻转的 `correlate`，在 float64 中按行主序累加并存为 float32，按 `STRIP_ROWS = 256` 逐条带运行。
- `render_full`：逐条带跑白平衡去马赛克、`srgb_encode` 与 `_neutral_display`（`np.rint` 一半向偶数，不同于 `pixels._linear_to_srgb` 的 `+0.5` 截断），最后按 `Orientation` 转正为 uint8 `(H, W, 3)`。`srgb_encode` 在 Rust 侧用 `gxr_math::powf(f64::from(a), f64::from(f32(1/2.4))) as f32` 计算正确舍入的幂；macOS `libSystem powf` 在个别 `.5` 边界（如 `0x3cc1c2a9`、`0x3da23384`、`0x3eadd76b`）会少 1 ULP，对拍测试通过 `dng-srgb-pow` 核对 Rust 幂的真实 float32 位型，并用 60 位 `Decimal` 独立核对正确舍入结果。
- 分配前边界检查：`demosaic_full`、`demosaic_strips`、`demosaic_strip` 与 `render_full` 在分配任何输出或条带缓冲区前，先以 checked arithmetic 校验负 `DefaultCropOrigin`、超出 `ActiveArea` 与超出裸平面的裁切边界。
- CLI 新增 `dng-demosaic --dng PATH --y0 A --y1 B --out OUT.npy`、`dng-neutral --dng PATH --out OUT.npy` 与 `dng-srgb-pow --bits HEX`；WASM 在 `gxr-wasm/src/raw.rs` 暴露 `dng_neutral` / `dng_neutral_named`。

