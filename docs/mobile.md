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

只保证前缀的：打开失败时 kind 是 `raw_decode`，消息以 `不是可识别的 GXR DNG（ValueError: ` 开头、以 `）` 结尾。reshape 在「一个未知维度紧挨着 0」时，numpy 会把形状印成 `(0)` 或 `(0,newaxis)`，Rust 用同一句 `cannot reshape array of size … into shape …`，形状文本不必逐字相同。

元素个数超过 2**32 - 1、但字节数仍小于 numpy 的 2**63 - 1 上限时，原生和 WASM 都拒绝，消息用上面的 array is too big。64 位 numpy 这时可能去分配并抛 MemoryError。测试里的 2**62 个黑电平两边都是 numpy 自己拒绝的那句。

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

## 色彩（`gxr-color`）

移植 `engine/knobs.py`、`engine/menu.py`、`camdata/calib.py` 的 `_context` 与 `model_for`、冻结引擎的正向模型（`sector_model`、`pretable` 正向、`matrix_model`、`colour.Z_of_words`、`model.pre/predict`、`unit_model`）、`render/chain.py` 的烘焙与套用、`render/tones.py`，以及 `gpu/tables.py` 的 `bake_bytes`。菜单 JSON 由调用方传入，不嵌进这个 crate。不移植 `exact_u8`、`inverse_stock`、`brentq` 和单调扫描（PORT-WAVE-1 A.2、A.7）。

### 命令

```text
gxr-cli color-knobs-apply --entry BASE.bin --menu final.json --params P.json --out OUT.bin [--stock STOCK.bin]
gxr-cli color-calib-check --npz CTX.npz --entry ENTRY.bin
gxr-cli color-calib-check --dir CALDIR --unit U --iso N --stock STOCK04.bin
gxr-cli color-predict --entry E.bin --stock STOCK04.bin --ctx C.npz --ring R.json --codes IN.npy --out OUT.npy [--float] [--half 1] [--base]
gxr-cli color-bake --entry E.bin --stock STOCK04.bin --ctx C.npz --ring R.json [--params P.json] [--menu final.json] --out BAKE.bin --meta META.json
gxr-cli color-apply --bake BAKE.bin --meta META.json --codes IN.npy [--params P.json] --out OUT.npy
gxr-cli color-tones --params P.json --out TABLE.npy
```

- `color-knobs-apply`：`P.json` 有 `levels` 时走菜单（还要 `unit`、`slot`，级别必须是整数）；没有时用 `amount`（默认 100）、`doses`（10 个整数）、`wb_method`、`tone_method`。`--stock` 省略时和底子相同。输出 552 字节，标准输出是 `out`、`length` 和 `stats`。`amount == 0` 仍会重写 CRC。
- `color-calib-check --npz`：`_context` 的检查。`ENTRY.bin` 的字节 115:211 解码成原厂色表行。成功时给出 `rows`（4 或 10）、`d0`、`d1` 的长度。
- `color-calib-check --dir`：`model_for` 对校准包的检查（版本、单元、原厂 04 指纹、文件清单、sha256），再按 ISO 选模型（`iso >= 566` 用 1600），并建好模型。输出 `iso`、`calibrated`、`model_iso`、`ctx`、`ring`、`rows`。
- `color-predict`：默认是 `UnitModel.predict`（先 `input_codes`，色相查找用原厂矩阵行）；`--base` 是 `Model.predict`（不做输入映射，按配方色度 `forward`）。码值 `.npy` 是 float32 或 float64，最后一维是 3，dtype 按 Python 的规则参与运算。默认输出 uint8，`--float` 输出夹紧后未量化的 float64。
- `color-bake`：`P.json` 是 `{"step": 0.375, "knobs": {...}}`，都可省。有 `knobs` 时先 `knobs.apply(stock, entry, …)`，这时要给 `--menu`。`BAKE.bin` 就是 `bake_bytes` 的 float32 表（布局见 `common.wgsl`）。标准输出是 `bytes`、`sha256` 和 `meta`。
- `color-apply`：`chroma_u8`。`P.json` 是 `{"adjust": {...}, "curves": {...}}`，都可省；增益来自 `channel_gains`，调子表来自 `display_table`。
- `color-tones`：输出 `gains`（或 null），调子表写成 float32 `.npy`（或 `table: null`）。`adjust` 和 `curves` 是 `parse_adjust` / `parse_curve` 之后的值；解析本身在第二波（编辑解析）。

### meta 多了 `input_coefficients`

Python 的 meta 只有 `nx`、`ny`、`origin`、`step`。`.bin` 里的输入映射系数是 float32，而 `chroma_u8` 用的是 float64 原值，只读 `.bin` 套不出逐字节相同的结果。所以 meta 另带 `input_coefficients`（float64，4 或 10 行，原样）。显卡只读 `.bin`，不受影响。

### 精度

- 整数、字符串、错误种类和文案：一样。
- 烘焙表：float32 逐字节相同。套用和 `predict(quantize=True)`：uint8 一样，没有 ±1 余量。
- 未量化的 `predict` 和增益：`|a−b| ≤ 1e-12·max(1,|a|)`。
- 运算都是顺序的乘加，不融合。numpy 在这台机器上会融合，也会用分块的 BLAS；这些差在 float64 的末位，存成 float32 或量化以后不见了（PORT-NOTES §2b）。
- 复数除法照 numpy 的写法：先选分支，再乘倒数 `1/(br + bi·rat)`，不是直接除。
- `np.sum` 沿 24 个扇区是成对求和（8 路累加再成树）。
- `np.interp` 照 numpy 的二分查找（带上一次的猜测）和斜率公式。
- PCHIP 用 scipy 的导数和幂次求值（不是 Horner）。`ndtr` 是 Cephes，`exp` 等走 `gxr_math`。
- 凸包用单调链代替 Qhull。法向量和 Qhull 逐位相同；偏移量 Qhull 用了融合乘加，这里是普通乘加。

### 错误

- `_context`：色表行不一致 kind `stock_mismatch`；尺寸、非有限数、`S_eff` 还原失败 kind `calib`；缺数组 kind `key`，文案和 numpy 一样（`'名字 is not a file in the archive'`）。不是 zip 的文件走 numpy 的 pickle 文案（kind `value`）。压缩的 zip 成员拒绝，kind `zip`（Python 能读，这是有意的差别：不加解压依赖）。
- `model_for`：版本、单元、清单、改动 kind `calib`，原厂 04 不匹配 kind `stock_mismatch`。包里缺文件 kind `file_not_found`，文案不和 Python 的 `FileNotFoundError` 逐字相同。
- 预测和烘焙：`ValueError` 的文案相同（不对称色表行、系数字高位、色相环节点、输入映射尺寸、未知核类型、`需要 v2 pre-table 模型`），色相环缺键 kind `key`。
- 不和 Python 逐字相同的：八个角张不成面积时 Qhull 报 `QhullError`，这里 kind `qhull`，文案只是一行；条目不是 552 字节（Python 的条目都来自 `datcodec`，永远是 552）；色相环里节点不是 24 个、`gain_per_sector` 不是 1 或 24 个（Python 到预测时才报 numpy 的广播错误，这里构造时就拒绝，文案用节点检查那句）；数字字段不是数字（kind `type`）；网格超过 2^24 格（kind `memory`）。
- 尺寸：`.npy` 的形状、头部长度和烘焙网格（包括 meta 里的 `nx`、`ny`）都用带检查的 u64 相乘，元素数不超过 2^32−1，分配失败也不 panic。超出时原生和 WASM 给同一句 numpy 文案 `array is too big; …`（kind `value`）。

### WASM

`color_bake(entry, stock, menu_json, ctx_npz, ring_json, params_json)` 返回 `ColorBake`（`bytes()`、`meta`、`summary`），`color_apply(bake_bin, meta_json, codes_f32, params_json)` 返回 uint8。两者和 CLI 走同一段 `gxr-color` 代码。比较用例在 `mobile/scripts/wasm_cases/color.mjs`，每个输入是一份列出文件的 JSON。
