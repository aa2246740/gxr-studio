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

`?autotest=1` 只在开发服务器里生效。它会拉 `GXR_MOBILE_SAMPLES` 里的第一张 DNG，跑描述和 5 秒自动拖动，再把结果写到 `mobile/.reports/`。这些路径不会出现在 `vite build` 的产物里。开发壳的页面是 `dev.html`，`/?dev=1` 和 `/?autotest=1` 也会打开它，所以原来的自动测试地址不用改。

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
gxr-cli color-parse-edit --json EDIT.json
gxr-cli color-listing [--dat GXRCE.DAT [--library GXRFX.BIN]]
gxr-cli color-controls [--dat GXRCE.DAT [--library GXRFX.BIN]] --unit U --slot S
gxr-cli color-resolve --dat GXRCE.DAT [--library GXRFX.BIN] (--recipe R.json | --default --unit U)
```

- `color-knobs-apply`：`P.json` 有 `levels` 时走菜单（还要 `unit`、`slot`，级别必须是整数）；没有时用 `amount`（默认 100）、`doses`（10 个整数）、`wb_method`、`tone_method`。`--stock` 省略时和底子相同。输出 552 字节，标准输出是 `out`、`length` 和 `stats`。`amount == 0` 仍会重写 CRC。
- `color-calib-check --npz`：`_context` 的检查。`ENTRY.bin` 的字节 115:211 解码成原厂色表行。成功时给出 `rows`（4 或 10）、`d0`、`d1` 的长度。
- `color-calib-check --dir`：`model_for` 对校准包的检查（版本、单元、原厂 04 指纹、文件清单、sha256），再按 ISO 选模型（`iso >= 566` 用 1600），并建好模型。输出 `iso`、`calibrated`、`model_iso`、`ctx`、`ring`、`rows`。
- `color-predict`：默认是 `UnitModel.predict`（先 `input_codes`，色相查找用原厂矩阵行）；`--base` 是 `Model.predict`（不做输入映射，按配方色度 `forward`）。码值 `.npy` 是 float32 或 float64，最后一维是 3，dtype 按 Python 的规则参与运算。默认输出 uint8，`--float` 输出夹紧后未量化的 float64。
- `color-bake`：`P.json` 是 `{"step": 0.375, "knobs": {...}}`，都可省。有 `knobs` 时先 `knobs.apply(stock, entry, …)`，这时要给 `--menu`。`BAKE.bin` 就是 `bake_bytes` 的 float32 表（布局见 `common.wgsl`）。标准输出是 `bytes`、`sha256` 和 `meta`。
- `color-apply`：`chroma_u8`。`P.json` 是 `{"adjust": {...}, "curves": {...}}`，都可省；增益来自 `channel_gains`，调子表来自 `display_table`。
- `color-tones`：输出 `gains`（或 null），调子表写成 float32 `.npy`（或 `table: null`）。`adjust` 和 `curves` 是 `parse_adjust` / `parse_curve` 之后的值。
- `color-parse-edit`：`resolve::parse_edit_state` 只解析 `recipe`、`adjust`、`curve` 和可选 `lut`（不包含桌面 `render/edits.py:parse_edit` 里的 `geometry`、`border`/`frame`、`stamp`），输出标准化后的对象以及 `canonical` 和 `digest`。
- `color-listing` / `color-controls` / `color-resolve`：无 `--dat` 时按内嵌菜单输出（`color-listing`、`color-controls`）；提供 `GXRCE.DAT`（和可选 `GXRFX.BIN`）时按机内配方表解析列表、控件或目标 `552` 字节条目、`entry_sha256` 与 `knobs` 请求。`EditError` 按 `{"error":{"kind","message","detail"}}` 返回。

### meta 多了 `input_coefficients`

Python 的 meta 只有 `nx`、`ny`、`origin`、`step`。`.bin` 里的输入映射系数是 float32，而 `chroma_u8` 用的是 float64 原值，只读 `.bin` 套不出逐字节相同的结果。所以 meta 另带 `input_coefficients`（float64，4 或 10 行，原样）。显卡只读 `.bin`，不受影响。

### 精度

- 整数、字符串、错误种类和文案：一样。
- 烘焙表（Gate A / Gate B，PORT-WAVE-1 Amendments 1–3）：Python 参考侧通过唯一入口 `color_parity.capture_bake_chroma`（`tests/support/color_oracle_gates.py`）只运行一次生产 `chain.bake_chroma`，在最终 `.astype(np.float32)` cast 前捕获完整 `(ny, nx, 2)` float64 增益网格并返回冻结对象 `CapturedBake`（含 `bk`、`data`、`meta` 与只读 `witness`，涵盖彩色凸包内、凸包外与 `entry[0]==3` 黑白路径；仅对属主线程拦截 `stack`，其他线程直通原生 numpy，同线程嵌套捕获在修改 `chain.np` 前即拒绝，钩子未恰好触发 1 次则失败）；`color_parity.bake_gate(py, rust, model, entry, *, grid_cache=..., host_cache=...)` 只接收 `CapturedBake` 作为 `py`（普通元组/ndarray/异常或错配均直接报错，绝不跳过；无独立 `witness=` 参数，杜绝旧见证配新烘焙），不再重跑 `chain.bake_chroma`，保留字节、`meta`、`ChromaBake` 与整张 float64 见证对顺序 oracle 的 `<= 1e-12·max(1,|a|)` 校验（含 NaN/inf 规则，并覆盖参考运行后恢复 `table.forward` 的 `1e-10` 注入、持续 `table.forward` `1e-10` 注入、黑白/凸包外 `np.ones` `1e-10` 注入、旧捕获配新 Rust 结果、普通元组拒绝与并发 `bake_chroma`/`bake_model` 隔离用例）；**Gate A** 要求 Rust 与顺序 oracle 的 float32 字节**逐位相等**，无掩码；**Gate B** 只允许真实 Python 与 Rust 在 Gate A 通过的格点上差值，并机械归因（oracle 与 Python 批量/单格 float64 值、各自落在 float32 舍入中点的哪一侧、以及恢复宿主 BLAS/FMA/libm 路径后精确复现 Python float32 的负对照；虚部极小近零分量 `|py| < 1e-4` 须满足 float64 `1e-12` 门限且负对照复现 Python）。套用和 `predict(quantize=True)`：uint8 一样，没有 ±1 余量。
- 覆盖范围（`tests/test_core_parity_color_looks.py`）：日常对拍（`make check` / 默认 pytest）运行合成烘焙对拍、负对照（恢复宿主核与 `1e-10` 注入）、`lut-libm` 边界检查（`atan2`/`hypot` 仅限 `--stream` tag 7/8，文件模式 `--op` 拒绝）以及 3 个回归 Looks（`g158` `M9 STD`、`instax mini`、`QianXi A`，`step=0.375` 默认与各旋钮 ±max）。全量 26 份真实 DAT 扫描（`test_all_real_dats_base_recipes_step_0375` 与 `test_all_real_dats_knobs_minmax_step_2`，在 `jlaunch` ISO 200/1600、`qlaunch` ISO 200、`l08firm` ISO 200 上测全部 builtin、Look、record 与 user 默认与 ±max，约 1 小时）仅在 `GXR_COLOR_DAT_SWEEP=1` 时执行；未设该变量时即使 `GXR_REQUIRE_SAMPLES=1` 也直接跳过。触及 `gxr-color` 或色彩测试的合并门槛显式运行：

```sh
GXR_COLOR_DAT_SWEEP=1 GXR_REQUIRE_CORE=1 GXR_REQUIRE_SAMPLES=1 uv run --frozen python -I -m pytest -q tests/test_core_parity_color.py tests/test_core_parity_color_looks.py
```
- 未量化的 `predict` 和增益：`|a−b| ≤ 1e-12·max(1,|a|)`。
- 运算都是顺序的乘加，不融合。numpy 在这台机器上会融合，也会用分块的 BLAS；这些差在 float64 的末位，跨过 float32 舍入中点时由上面的 Gate B 机械归因（PORT-NOTES §2b）。
- 复数除法照 numpy 的写法：先选分支，再乘倒数 `1/(br + bi·rat)`，不是直接除。
- `np.sum` 沿 24 个扇区是成对求和（8 路累加再成树）。
- `np.interp` 照 numpy 的二分查找（带上一次的猜测）和斜率公式。
- PCHIP 用 scipy 的导数和幂次求值（不是 Horner）。`ndtr` 是 Cephes，`exp`、`sin`、`cos`、`atan2`、`hypot` 等走 `gxr_math`。
- 凸包用单调链代替 Qhull。法向量和 Qhull 逐位相同；偏移量 Qhull 用了融合乘加，这里是普通乘加。

### 错误

- `_context`：色表行不一致 kind `stock_mismatch`；尺寸、非有限数、`S_eff` 还原失败 kind `calib`；缺数组 kind `key`，文案和 numpy 一样（`'名字 is not a file in the archive'`）。不是 zip 的文件走 numpy 的 pickle 文案（kind `value`）。压缩的 zip 成员拒绝，kind `zip`（Python 能读，这是有意的差别：不加解压依赖）。
- npz 照 CPython 3.14 `zipfile` 和 numpy `NpzFile` 读：打开时找结尾记录（含 ZIP64）并逐条读中央目录；取某个数组时才看它的本地头（签名、名字、和下一个成员重叠），payload 的大小和 CRC-32 以中央目录为准，所以 `np.savez` 写不可 seek 的流时产生的 data descriptor 也能读。同名成员取中央目录里最后一条，取键先按完整名、再按 `名字.npy`。文案用 `zipfile` 的原文，kind 按 Python 的异常：`BadZipFile` 是 `zip`，`NotImplementedError` 是 `not_implemented`，`RuntimeError` 是 `runtime`，`EOFError` 是 `eof`，负偏移的 `ValueError` 是 `value`，名字不是合法 UTF-8 时 `UnicodeDecodeError` 是 `unicode`，文案和 Python 一样带出错的字节、位置和原因。
- 有意的偏离：手机读完整个被选中的成员再核对 CRC-32。numpy 按需读取，成员里 NPY 数组后面如果还有很长的尾部，它读不到成员结尾就不核对 CRC，所以只坏在尾部的包 Python 会接受，手机用同一句 `Bad CRC-32 for file '…'` 拒绝。`np.savez` 不会写出这种尾部；拒绝损坏的包对手机更安全。坏在数组字节里时两边都拒绝。
- `model_for`：版本、单元、清单、改动 kind `calib`，原厂 04 不匹配 kind `stock_mismatch`。包里缺文件 kind `file_not_found`，文案不和 Python 的 `FileNotFoundError` 逐字相同。
- 预测和烘焙：`ValueError` 的文案相同（不对称色表行、系数字高位、色相环节点、输入映射尺寸、未知核类型、`需要 v2 pre-table 模型`），色相环缺键 kind `key`。
- 不和 Python 逐字相同的：八个角张不成面积时 Qhull 报 `QhullError`，这里 kind `qhull`，文案只是一行；条目不是 552 字节（Python 的条目都来自 `datcodec`，永远是 552）；色相环里节点不是 24 个、`gain_per_sector` 不是 1 或 24 个（Python 到预测时才报 numpy 的广播错误，这里构造时就拒绝，文案用节点检查那句）；数字字段不是数字（kind `type`）；网格超过 2^24 格（kind `memory`）。
- JSON：数组和对象最多嵌套 128 层，进入下一层之前就检查，超出时 kind `json`，文案 `JSON 嵌套太深`；编辑与配方解析输入上限为 4 MiB（`MAX_JSON_BYTES`），超出时 kind `json`，文案 `JSON 文件过大`，原生和 WASM 一样（Python 的 `json` 会接受更大、更深的输入，这是有意的差别）。解析器同时保留独立 UTF-16 代理转义（`\uD800`..`\uDFFF`），并在输出 JSON 时按 `\uXXXX` 原样写回。
- 尺寸：`.npy` 的形状、头部长度和烘焙网格（包括 meta 里的 `nx`、`ny`）都用带检查的 u64 相乘，元素数不超过 2^32−1，分配失败也不 panic。超出时原生和 WASM 给同一句 numpy 文案 `array is too big; …`（kind `value`）。

### WASM

`color_bake(entry, stock, menu_json, ctx_npz, ring_json, params_json)` 返回 `ColorBake`（`bytes()`、`meta`、`summary`），`color_apply(bake_bin, meta_json, codes_f32, params_json)` 返回 uint8；`color_listing(dat_bytes?, library_bytes?)`、`color_controls(dat_bytes?, unit: f64, slot: f64, library_bytes?)`、`color_resolve(dat_bytes, recipe_json, library_bytes?)` 返回带换行的 JSON 字符串（失败抛出含 `kind`、`message` 和可选 `detail` 的核心错误 JSON）。`color_controls` 会先校验有限整数与 `unit`（0..2）/ `slot`（1..8）范围，避免 JS `Number` 转 `i32` 回绕。比较用例在 `mobile/scripts/wasm_cases/color.mjs`，对拍在 `tests/test_core_parity_resolve.py`（另含完整错误 JSON 与非有限/小数边界对比）。

## 边框和日期戳（frame）

`gxr-frame` 移植 `finish/sfnt.py`、`spec.py` 的解析、`layout.py`、`fonts.py` 的度量一半、`frame_core.py` 和三份样式（`frame_styles.py`、`frame_objects.py`、`frame_new.py`），以及 `logo_math.py` 里算尺寸的部分和 `stamp.format_stamp`。Rust 只出版面 JSON：每个框、每行字、每个字的列、颜色。字和形状由浏览器画，墨迹、发光、颗粒、纹理的像素不在对拍里，参数在 JSON 里，要相同。

字体都按字节传进来，crate 里不嵌字体文件。命令行写 `--font 名字=路径`，名字是 `atkinson`、`inter`、`inter_sb`、`courier`、`patrick`、`dseg`，中文回退字体叫 `cjk`。没给 `cjk` 而文字要中文字形时，错误是 `missing_cjk`（「无法显示中文。请改用英文或数字。」），和 Python 找不到系统字体时一样。不去看 macOS 的系统字体路径。

中文度量：Python 用 Pillow（FreeType，BASIC）在 1000 像素上量中文字，上沿按字号量。Rust 读回退字体的 CFF 字形程序取控制框，按 FreeType 的定点算法缩放、取整：`y_scale = FT_DivFix(px*64, upm)`，上沿 `FT_PIX_CEIL(FT_MulFix(ascender, y_scale))`，步进取整到像素，框包含原点和步进（Pillow 的 textbbox 从零开始）。测试拿 `tests/fixtures/NotoSansSC-Shanghai.otf` 和 Pillow 比：步进和上沿逐个相同。墨迹框不一样：Pillow 量之前 FreeType 先做 CFF 微调，Rust 用的是不微调的框（和 FreeType 的 `FT_LOAD_NO_HINTING` 相同），1000 像素上框的边差 1 个单位。Python 再把这个取整过的微调框按字号放大，所以日期戳上的差约为 px/1000。

中文字体本来就两边不同：桌面的中文回退是 macOS 系统字体（`stamp.py` 的 `_CJK_FONTS` 第一个是 PingFang），手机读不到也不能带，只能收一份 OFL 中文字体的字节（`--font cjk=…`）。对拍两边都用测试夹具 `NotoSansSC-Shanghai.otf`，验证的是同一字体上的排版算法，不是用户看到的结果。所以没有去做随字体附带的微调框表（经理 2026-11-14 裁定）。

门槛（经理在 Sol r4 之后裁定，2026-11-14）：中文日期戳和 Rust 比的不是真实的 Python，而是「度量替换 oracle」——Python 真实的布局代码（`stamp_layout`、边框布局、缩字循环）原样运行，只把中文字形的度量（`FontMetrics.glyph` 里 CJK 的步进和框）换成 Rust 在 1000 像素下量出的值。替换在测试辅助 `tests/support/frame_oracle.py` 里，`src/` 不动。Rust 必须和这个 oracle 每个字段都精确相等：px、glow、rect、ops、颜色。原生和 WASM 也逐字节相同。

为什么用 oracle：真实的中文印章两边本来就用不同字体（桌面是 PingFang，手机是 OFL 字体）；在同一个夹具字体上，两边唯一的差别是微调和不微调的度量。Rust 照 Python 的流程走：每个中文字在 `CJK_REFERENCE_PX = 1000` 下从 CFF 轮廓量框，按 FreeType 不微调的办法取整，然后走和 Python 一样的缩放取整。对真实的 Python，测试只断言 δ ≤ 1 并打印：δ 是同一组字形在 1000 像素下微调框和不微调框逐条边的最大差，夹具字体实测是 1（「上」826／825，「海」842／841）。

已知、已归因的差异：除了坐标差约 px·δ/1000（原来的公式 `ceil(2·px·δ/1000) + 1` 现在只打印统计），1 像素的框差还会翻转离散分支。例如拍立得 `instax_wide`、题字「海」、照片 282×212、印章「北」seg7 large 开 glow 放在边框上：14 px 时 Python 的框高 14、Rust 的框高 13，可用高度正好 13，Python 缩到 11 px，Rust 保持 14 px，于是 px、glow、rect 都不同。这类例子（Sol r4 的 30 例和每种拍立得格式的缩字临界点）都在 oracle 门槛里。桌面导出照旧用 Pillow 的框。`glyf` 字体做回退时用字形头的框，FreeType 在 1000 像素上的 TrueType 微调没有移植。

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

## LUT 移植（`gxr-lut`）

`gxr-lut` 负责 `.cube` 解析与四面体插值（`cube.rs`）、色彩空间与 `Target.render`（`spaces.rs`）、8 位量化套用（`photo.rs`）、GPU LUT 表（`tables.rs`：`display_lut_u8` 与 `lut_bytes`）、空间识别与未声明默认选项（`identify.rs` / `identify_scan.rs`），以及复用 `gxr-raw::colorimetry` / `gxr-raw::scene` 的场景 XYZ 胶水层（`scene.rs`）。

- CLI 子命令在 `core/crates/gxr-cli/src/cmd_lut.rs`：`lut-parse`、`lut-apply`、`lut-encode`、`lut-table`、`lut-identify`、`scene-xyz`。
- WASM 导出在 `core/crates/gxr-wasm/src/lut.rs`：`lut_parse`、`lut_identify`、`lut_table`、`lut_apply_u8`。
- `identify` 对未声明且无法唯一确认输入空间的 `.cube` 保留 `input_space: null`，绝不把未知 `.cube` 默认当成 sRGB；`undeclared_choice` 只返回界面提示的默认选项与说明文本。
- `scene-xyz` 复用 `gxr-raw` 已有的 `scene_colour` 与 `affine_of`，并把 `SceneError.code`（`no_neutral`、`no_matrix`、`bad_matrix`、`bad_colour`）原样作为 `CoreError.kind` 返回。
- 接受与拒绝边界和 Python 一致，不在解析端拦截极端有限值 LUT；表值都在 `[-1, 2]` 内的良态用例保持 Gate B `<= 1` 码上限，而表值极端的 LUT，在两边结果都是浮点噪声的像素上，手机和桌面可能差几个码值（仍须通过 Gate A `1e-12` 无掩码与机械归因）。

## HDR（`gxr-hdr`，命令前缀 `hdr-`）

移植的是 `hdr/gain.py`（对齐、曲线、增益平面）、`hdr/container.py`（ISO 21496-1 + Ultra HDR + MPF 封装）和 `hdr/capture.py` 的 `_capture`、`differs`。增益图 JPEG 的编码不在这里：这里给出 `encode_gainmap` 交给 Pillow 之前的 uint8 平面，封装时把编好的增益图 JPEG 当输入。`derived.py` 和 `finish/hdr_export.py` 是第二波。

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `hdr-filter --op gaussian\|uniform\|minimum\|label\|dilate` | `--in` 平面，`--sigma` / `--size` / `--iterations` | `--out` 同 dtype（label 是 int32，另印 `count`） |
| `hdr-map` | `--in` 平面，`--sy`、`--sx` float32 坐标 | `map_coordinates(order=1, mode="nearest")` |
| `hdr-block-mean` | `--in`、`--factor` | 边缘补齐的块平均 |
| `hdr-luma` | `--rgb` uint8 `(h, w, 3)`、`--factor` | `sdr_luma_grid` float32 |
| `hdr-raw-luma DNG` | DNG 路径 | `--out` 亮度 float32，`--clip-out` 过曝掩码（都已转正） |
| `hdr-phase` | `--a`、`--b` float32 | `_phase_shift` 的 `dy`、`dx` |
| `hdr-fit` | `--x`、`--y` float32，`--valid` 掩码 | `fit_curve` 的 `cx`、`cy` |
| `hdr-align` | `--sdr`、`--raw` float32 | `Alignment` 全部字段，`check` 是 `null` 或 `{kind, message}` |
| `hdr-register` | `--raw`、`--clip`、`--align` JSON、`--shape H W`、`--grid H W` | 重采样后的亮度和掩码 |
| `hdr-gain` | `--sdr`、`--raw`（NaN = 不是照片）、`--clip`、可选 `--params` JSON | `--out` uint8 码值；stdout 是 `meta`、`fractions`、`stats`；可选 `--delta-out` |
| `hdr-segments` | `--in` JPEG | `jpeg_segments` |
| `hdr-assemble` | `--primary`、`--gain` JPEG，`--meta` JSON | 文件字节，存成 uint8 `(1, N)` 的 `.npy` |
| `hdr-capture` | `--raw`、`--jpeg` 两份 `{标签号: 值}` JSON | 两边的 `_capture` 和 `differs` |

WASM：`hdr_align`、`hdr_gain`（`HdrGain` 有 `codes()` 和 `report`）、`hdr_assemble`。JSON 文本和 CLI 打印的逐字节相同。

### 数值上照着 numpy 2.4.3 / scipy 1.17.1 做的事

这些都在合成数组上实测过，`crates/gxr-hdr/src/np.rs` 和 `filter.rs` 的开头写着规则。

- NEP 50：float32 数组旁边的 Python float 先变成 float32 再算，比较也一样。
- 连续数组求和：从 0 开始加一次 numpy 的 pairwise 和。跨步视图：一次缓冲 `max(1, 8192 // 宽)` 整行，每块 pairwise，再依次相加。`mean`、`std` 除以 `np.intp(n)`，所以 float32 均值是 `f32(f64(和) / n)`。
- `block_mean` 只有一列块时，numpy 把整块 f×f 合成一次 pairwise。
- `percentile` 的插值在 float32 里做；`arange` 的长度在 float32 里算，第二个元素是 float32 的 `lo + step`。
- scipy 的 `reflect` 重复边缘样本。偶数窗口从 `i - size/2` 开始。`uniform_filter` 是 float64 滑窗和除以 size。`label` 按每块第一个像素的光栅顺序编号。
- FFT 按 numpy 2.4.3 的 pocketfft 路径逐位移植，见本文件末尾「hdr-fft」一节。

### 不逐位相同的地方，以及为什么不追

- scipy 在 arm64 上把短高斯核（sigma 0.5、0.6）的累加编成了 FMA；`np.convolve`（BLAS）和 `np.interp` 也会融合。CONVENTIONS 不许 FMA，所以这些 float64 中间量可能差最后一位，float64 滤波输出按 `1e-12` 相对比较。
- numpy 的 float32 `log2`、`pow` 调的是 Apple 的 libm，它在少数输入上不是正确舍入。按 PORT-WAVE-1「平台 libm」那条规则，对照值是正确舍入的结果，不抄 Apple 的值。这里用「libm 的 float64 结果舍入一次」：sRGB 的 256 个码值全部等于 `decimal` 50 位算出、再舍入一次的 float32（`test_srgb_to_linear_is_correctly_rounded`）。本机 numpy 只在码值 71、180 上不同，所以 `sdr_luma_grid` 会差最后一位；测试同时断言 Python 的 `align` 在 Rust 的网格上给出和 Rust 一样的结果，uint8 增益码值不变。
- 量化前的 float32 增益平面不设门槛：scipy 的 sigma 0.6 高斯在 arm64 上是 FMA，个别格子差 1 ulp；uint8 码值和元数据是精确的。

### hdr-fft：和 numpy 逐位相同的相位相关（`crates/gxr-hdr/src/fft/`）

`_phase_shift` 的 FFT 照 numpy 2.4.3 的 pocketfft 逐行移植（代码和 BSD-3 许可见 `fft/NOTICE`）。`fft2` 的 `fct=1` 是 Python int，numpy 因此走 complex128 循环：正变换在 double 里算，再舍入成 complex64；`ifft2` 走 complex64，在 float 里算并乘 float32 的 `1/n`。FMA 照 arm64 轮子的 `-ffp-contract=on` 来；`abs` 是 `simd_cabsolute_f32`，不是 `hypotf`；除法是 Smith 形式。twiddle 的 `cos`/`sin` 用正确舍入的值（PORT-WAVE-1 libm 规则），所以在 Apple `cos`/`sin` 不正确舍入的角上和本机 numpy 差最后一位，每个这样的角在 `tests/test_core_parity_hdr_fft.py` 里单独断言。

| 命令 | 输入 | 输出 |
|---|---|---|
| `hdr-fft` | `--in` float32 `(n, 2)` 或 `(h, n, 2)`（complex64 的实部、虚部），`--inverse 0\|1`，可选 `--two-d`、`--in-shape H N` | `--out` 同形状：numpy `fft`/`ifft`（或 `fft2`/`ifft2`） |
| `hdr-phase-plane` | `--a`、`--b` float32，可选 `--a-shape`/`--b-shape` | `--out` 整张 float32 相位相关平面；stdout 是 `dy`、`dx` |
| `hdr-fft-twiddle` | `--n N`，`--kind float\|double\|tables` | `sincos_2pibyn` 的 twiddle；`tables` 是原始 `cos`/`sin` 表和角度 |

三个命令都接受 `--trig host`（只在原生 CLI，诊断用）：用本机 `cos`/`sin` 建表，这时结果和本机 numpy 逐位相同。WASM：`hdr_fft`、`hdr_phase_plane`（`HdrPhasePlane` 有 `report` 和 `plane()`）。

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

## `gxr-jpeg`（JPEG 编码与容器段）

`gxr-jpeg` 负责成片导出时的单次 JPEG 编码，以及给 wave-2 HDR 封装使用的段操作：

- 编码参数对齐 `src/gxr_studio/export.py` 的 `_finish`：品质 `1..=100`（高 95 / 中 85 / 低 70），`quality >= 90` 用 4:4:4，否则用 4:2:0；一次交错扫描；写入 APP1 EXIF 和 588 字节 sRGB ICC（`ICC_PROFILE`）。
- `encode_jpeg_with(.., optimize)`（CLI `jpeg-encode --optimize`，WASM `jpeg_encode` 的最后一个参数）：
  - `optimize = true` 就是 `_finish` 写出的文件（Pillow `optimize=True`），整个文件逐字节相同：按这张图自己的符号统计建霍夫曼表（`jchuff.c` 的 `jpeg_gen_optimal_table`，平局取较大的符号，保留 256 号），JFIF 1.01，libjpeg 的段顺序（DQT 在 SOF0 前），EXIF 有理数按 Pillow 写（见下）。
  - `optimize = false`（`encode_jpeg`）和以前逐字节相同：Annex K 标准表，SOF0、DQT、DHT 和扫描数据与 Pillow `optimize=False` 相同；头部是 JFIF 1.02、SOF0 在 DQT 前、有理数原样写。
  - 优化表分两遍走同一批行条带：第一遍做颜色转换、下采样、DCT 和量化，只计数；建表；第二遍重新算同样的块并写出。libjpeg-turbo 是把整张系数数组存下来（4:4:4 时是 RGB 输入的两倍），这里不存，峰值内存和一次编码相同，代价是 DCT 算两遍。
  - 内存：Node 里的 WASM，4288×2848，每次一个新实例（线性内存只增不减，结束时的大小就是峰值）。噪声图 q95 开不开优化峰值都是 82.3 MiB（耗时 1298 ms 对 722 ms），平滑图 q95 都是 39.4 MiB；q85、q70 也都不变（round-6 `mobile-jpeg-optimize/report/wasm-peak.jsonl`）。
  - 用不到的表、全零的计数、越界的系数都返回 `encode_failed`，不 panic（`jpeg_enc/optimize.rs` 的单测）。
- EXIF 有理数（`optimize = true`，`rational.rs`）：照 Pillow 12.2.0 的 `_setitem` 推断类型，值里有 `< 0` 的才是 SRATIONAL，分母为 0 的值是 NaN，不算负数（所以 `5/0`、`-5/0`、`0/-3` 都是 RATIONAL）。RATIONAL 每个值过 `_limit_rational(v, 2**32-1)`（约分，大于 1 时经倒数限制；整数和浮点先变成 `float`）；SRATIONAL 过 `_limit_signed_rational`（保留原分子分母，超出 int32 时才限制，第二步用浮点除法）。Pillow 写不了的值（比如 RATIONAL 里的 `-5/0`）两边都让完整 EXIF 失败，退到精简 EXIF。
- Pillow 的输出缓冲区（`pillow_buffer.rs`）：`optimize=True` 时 libjpeg 在 `jpeg_finish_compress` 里一次写出全部表和扫描，不能中途换缓冲区，Pillow 只给 `max(65536, k·w·h + len(exif)+5 + len(icc 段)+1, 4w)` 字节（品质 ≥ 95 时 k = 2，否则 1）。装不下就是 libjpeg 的 “Suspension not allowed here”，`_finish` 先退到精简 EXIF 再试，再失败就是 `encode_failed`。桌面在高熵画面上真会这样失败（例如 320×480 的噪声在 q100）。gxr-jpeg 按 Pillow 的状态机算同样的条件，结果和桌面一致：同样退到精简 EXIF，同样报 `encode_failed`。边界是在 Pillow 12.2.0 上量出来的：写完 SOI、APP0、APP1、APP2 后剩余空间不足 1024 字节时，Pillow 换一个新缓冲区再写扫描；最后一段输出必须严格小于剩余空间。
- 底层编码器是内置在 `core/crates/gxr-jpeg/src/jpeg_enc/` 的 `jpeg-encoder 0.6.0` 源码（MIT/Apache-2.0，含 IJG 代码，许可原文和致谢见 `core/crates/gxr-jpeg/NOTICE` 与 `docs/dependencies.md`），已改成 libjpeg-turbo 的算术：整数除法量化 `(|x| + (q>>1)) / q`、`jccolor.c` 的定点颜色转换、`h2v2_downsample` 的交替 bias 与边缘扩展。不再依赖外部 crate。门槛（Gate B）：SOF0、DQT、DHT 和 SOS 起的扫描数据与 Pillow 逐字节相同，解码像素零差。
- EXIF 映射（`build_exif`）：`270`（`ImageDescription`）按 UTF-8 写入、`33432`（`Copyright`）按 latin-1 写入，强制 `274`（`Orientation`）= 1、`305`（`Software`）= `"GXR Studio"`、输出尺寸 `40962`/`40963`；去掉 GPS 与 MakerNote（`37500`）；超过 65533 字节自动退到精简子集。
- sRGB ICC 固定为 LittleCMS 生成的 588 字节标准配置（头里 `24..36` 字节的创建时间戳固定，以保证原生与 WASM 逐字节一致）。
- 容器辅助函数（`jpeg_segments`、`app_segment`、`set_meta`）按 `gxr_studio/hdr/container.py` 的段遍历逻辑实现，在不重编码熵数据的前提下替换/插入 APP1 EXIF 与 APP2 ICC。
- WASM 导出 `jpeg_encode(rgb_u8, w, h, quality, exif_json, optimize) -> bytes`，由 `mobile/scripts/wasm_cases/jpeg.mjs` 验证与 `gxr-cli jpeg-encode` 逐字节一致（规格里 `"optimize": true` 时 CLI 加 `--optimize`）。
- 整个文件的门槛在 `tests/test_core_parity_jpeg_optimize.py`：13 种尺寸（从 1×1 起）× 12 个品质（1–100），有理数的各种写法和退回精简 EXIF，缓冲区上限（失败、跨过上限的扫描、退回精简 EXIF），20 张真实照片的 95/85/70 以及不带 ICC、不带源 EXIF 的文件，WASM 对原生（含错误）。真实照片的比较走导出管线的脱敏函数，这个文件也在 `test_core_parity_export_pipeline_guard.py` 的 lint 范围里。

## 几何（`gxr-finish`）

`gxr-finish` 现阶段包含裁剪预设与校验（`crop.py`）、纠正与有效区收框（`finish/geometry.py`）、以及显卡预览裁切框（`gpu/box.py`）。

- 两种取整：`crop::crop_box` 用 `floor(v + 0.5)`（与 `crop.py` 一致），而 `geometry::crop_box` 与 `final_box` 用一半向偶数（与 Python `round` 一致）。
- 拉直中心：`warp` 围绕 `(w - 1) / 2` 旋转并双线性采样，`crop::box_inside` 围绕 `w / 2`；水平翻转在 `rotate90` 之后执行，`validate_crop(..., mirrored=true)` 按 `-angle` 检查。
- 大图有效区：长边超过 240 时 `normalized_valid_rect` 先抽样求最大内接矩形并向内缩 1 像素；`fit_ratio_in_valid` 的最近位置在全像素格点上逐个查找，不抽样。
- CLI 子命令：`geom-crop-box`、`geom-final-box`、`geom-warp`、`geom-header`（另附 `geom-warp-valid`、`geom-fitted-crop`、`geom-clamp-rect`、`geom-fit-ratio` 供对拍）。
- WASM 导出：`geom_final_box(w, h, geometry_json, crop_json)` 与 `geom_warp(rgb_u8, w, h, geometry_json)`（返回 `WarpedImage`，提供 `width`、`height`、`rgb()`、`mask()`）。

## 导出元数据（`gxr-export::meta`）

`gxr-export` 的 `meta` 模块移植 `export.py` 的源 EXIF 提取、`finish/meta_text.py` 的边框/日期文字生成、`meta.py` 的 `read_jpeg_meta` / `read_dng_meta` 以及 `dng_preview_image(embedded_only=True)` 的内嵌预览定位。

- **预览定位与解码边界**：`dng_preview_location`、`export-meta-preview` 与 `export_meta_preview` 只做结构挑选，返回 `dng_preview_image` 在不解码之前选中的唯一最大候选 `{offset, length}`（SubIFD `330`、`Compression == 7`、`0 < 条带字节数 <= 32 MiB`、可读且 `width * height` 严格最大），或 `null`。不在核心里复制 libjpeg 解码或表/扫描验证。JPEG 解码与坏图拒收由调用方负责（手机端由浏览器解码，对拍测试由 Pillow 解码）；若这个唯一候选解码失败，调用方视为无预览（`None`），不再尝试次大候选，与桌面 Python 一致。
- **WASM 参数与时间**：`export_meta_photo` 从浏览器显式接收 `mtime_ms` 与 `tz_offset_min`（`Option<f64>`），不读系统时钟或时区；`tz_offset_min` 在窄化之前先校验有限、整数且位于 `-1439..=1439`，与原生 CLI 给出相同的 `CoreError`。

## 导出管线（`gxr-export` 的 `pipeline/`，命令前缀 `export-run-`）

手机的整张导出拆成两次 Rust 调用，中间由浏览器画墨迹（文字、线条、标志、日期戳和光晕；PORT-WAVE-1「成片与导出 2–4」）。结果对齐桌面 1.0.1 的 `finish/serve.export_finish`。手机只导出 JPEG，不做 PNG。台账、目标文件夹、`claim_path` 的文件系统部分、本机配方和精确导出都不在手机上。

- **`export_plan(shot, side, defaults, base)`**：`serve._plan`。返回 `original`（没有可见编辑、有相机 JPG、底图是自动或相机 JPG 时为真）和解析后的边框、日期戳。为真时调用方把解码后的相机 JPG 交给下一步，否则交 `color_image` 的结果。
- **`export_prepare(...)`**：先校验品质和长边（`ExportOptions.validate`），再跑 `render.params.parse_edit` 里成片要用的部分。底图有两种：原样相机 JPG（`_plan` 为真时），否则调用 export-source 的 `color_image`（RAW 配方、中性 RAW、`standard` 的 04 模拟、内嵌预览回退、LUT、编辑过的相机 JPG）。之后是调整、几何、裁剪（raster `finish_array`，不画日期戳）、日期戳版面、边框版面，以及只有背景和照片的画布（raster `draw_frame_canvas`）。返回画布（uint8）和 JSON：`frame`、`stamp`、`stamp_on`、`photo`、`labels`、`state`。
  - 源文件输入照 export-source 的约定，空字节等于没给：DAT、resolve 给出的配方条目、校准包（`calib_index` + `calib_blob`）、写进 EXIF 描述的 `binding`、DNG 字节、相机 JPG 解码后的像素和**原始字节**（hdr-track 的拍摄核对要用）、内嵌预览像素、LUT（`.cube` 和空间 JSON）。sidecar 的颜色部分按 resolve 解析后的值交给 export-source；export-source 的错误（`raw_unreadable`、`no_jpg_source`、带 `state` 的 `RenderUnavailable`）原样返回。
  - 边框和日期戳的文字（`serve._meta`）：有源文件字节时，Rust 用 export-meta 的 `photo_meta` 算出 `meta`、`taken` 和 `file_time`，有相机 JPG 时读 JPG，否则读 DNG，和桌面一样。`file_time` 来自调用方传的 `mtime_ms`（`File.lastModified`）和当时的时区偏移 `tz_offset_min`，Rust 不读时钟和时区。这时 `layout` 只提供 `logos` 和 `mark`。
  - 日期戳画在哪：`stamp_on == "photo"` 时画在照片上，坐标相对照片。浏览器必须按 `photo` 矩形裁剪着画，光晕不能越出照片，因为桌面是在加边框之前把日期戳画进照片的。即时成像边框加 `placement == "border"` 时是 `"canvas"`，在边框画好之后画在整张画布上。
  - `canvas`：`"full"` 时返回的就是整张画布（有边框时是背景加照片，没边框时就是照片）。`"photo"` 表示这是模糊背景或纸张颗粒（PORT-WAVE-1 决定 3，raster 不画）：返回的只是成片照片，浏览器画好背景后把它贴在 `photo` 的位置，再画墨迹。
  - `labels` 和桌面 `_stage_rgb` / `_original_labels` 写出的字典逐项相同，顺序也相同。
  - HDR（`hdr` 为真时）：在底图上跑 hdr-track 的 `begin`（DNG 加相机 JPG 的原始字节；没有 DNG 是 `no_raw`），再按照片实际走的分支做 `warp` / `box` / `crop`（和 `finish_unframed` 一样用 `warp_valid`、`fit_preset_in_valid`、`normalized_valid_rect`、`clamp_norm_rect` 判断）。数据层以 float32 `(h, w, 2)` 单独返回（WASM 的 `hdr_layer()`，CLI 的 `--hdr-out`），`state.hdr` 里是它的 `active` / `shape` / `problem` 和边框版面。
  - `state` 是交给下一步的小 JSON（名字、品质、长边、标签、HDR），调用方原样传回。
- **`export_complete(canvas, w, h, state, source, hdr_layer, stamp_mask, mask_w, mask_h)`**：HDR 时先用浏览器给的日期戳掩码跑 `stamp`（只有画在照片上的日期戳才有掩码：照片大小，日期戳在黑底和白底上各画一次，变了的像素非 0；边框上的日期戳不碰数据层，和桌面一样），再用版面跑 `frame`，最后 `finish`：payload 是本单元那一次编码的结果，rgb 是缩放后的数组。`hdr`、`hdr_reason` 和文件名里的 `_hdr` 跟着它，和桌面一致。手机只导出 JPEG，所以 `not_jpeg` 走不到；`container` 只在 payload 不是合格 JPEG 时出现，本单元自己的编码结果不会触发（hdr-track 自己的测试覆盖这两个理由）。像素超过 Pillow 上限的相机 JPG 是已接受的偏差：桌面直接抛 `DecompressionBombError`，手机在原样导出时 `begin` 报 `metadata_unreadable`、照常导出不带 HDR；编辑过的照片在 `photo_meta` 那一步两边都失败。
  - `source` 等参数：`source` 是 `state.source` 指的那个文件（有相机 JPG 时是 JPG，否则是 DNG，即 `serve._source_path`）的字节。export-meta 按 `export.source_exif` 读出 `base` / `sub`，`pipeline` 写上 `270` 描述并按 Pillow 约分有理数，再交给 gxr-jpeg 的 `build_exif`。画好墨迹的画布先 `_fit` 到长边（gxr-finish resample 的 LANCZOS），再按 `_finish` 的参数编码一次。返回文件字节，以及 `ExportResult` 的字段（`width`、`height`、`bytes`、`render`、`render_label`、`format`、`render_kind`、`calibrated`、`detail`、`hdr`、`hdr_reason`、`recipe_binding`）和文件名 `file_name` / `tag`（`原名_gxr{tag}.jpg`，做成 HDR 时 tag 加 `_hdr`）。
- **`export_output_sizes(w, h, side, defaults, layout, rect, preset, fonts)`**：`finish/sizes.output_sizes`。`w × h` 是源尺寸（相机 JPG 转正后的尺寸、DNG 裁切窗口或内嵌预览尺寸），由调用方从文件头得到。
- 版面输入 `layout` 用 gxr-frame 的请求格式，去掉 `w`、`h`：`meta`、`logos`、`mark`、`taken`、`file_time`。字体和 `frame_layout` 一样，用字体包（`font_index` + `font_pack`）。
- CLI：`export-run-plan`、`export-run-prepare`、`export-run-complete`、`export-run-sizes`。CLI 和 WASM 都调用 `pipeline::api`，同样的输入给出同样的字节。尺寸、品质和长边都按 f64 传入，先检查再收窄。

### 编码就是桌面的文件

桌面 `_finish` 用 Pillow `optimize=True`。`export_complete` 直接调用 `gxr_jpeg::encode_jpeg_with(.., optimize = true)`，出来的就是这个文件：优化霍夫曼表、JFIF 1.01、DQT 在 SOF0 前、有理数按 Pillow 写，都在 gxr-jpeg 里（见上面 `gxr-jpeg` 一节）。以前这里有一步 `pipeline/huffopt.rs`：把 gxr-jpeg 的 `optimize=False` 输出无损重编码，再改段顺序、JFIF 版本，并在交给 gxr-jpeg 之前先约分有理数。这些都已经移进编码器，重编码和 `export-run-huffopt` 命令也去掉了。

### 和桌面字节相同的前提

- **ICC 时间戳。** Python 的 `srgb_icc()` 是 LittleCMS 运行时生成的，头里第 24–36 字节是生成时间，所以桌面每次运行写出的文件都不一样。gxr-jpeg 把这 12 字节固定了。测试先断言两份 588 字节的配置只有这 12 字节不同，再让 Python 用固定的那份。
- **有理数。** Pillow 写 RATIONAL 时都经过 `_limit_rational(值, 2**32-1)`（例如 28/10 写成 14/5），SRATIONAL 经过 `_limit_signed_rational`。gxr-jpeg 在 `optimize = true` 时照样写，分母为 0 的值也按 Pillow 推断类型（NaN 不 `< 0`，所以是 RATIONAL）。`pipeline` 把源照片的有理数原样交给 gxr-jpeg。
- **缓冲区。** 桌面写不出的高熵文件（Pillow 的输出缓冲区装不下），手机也报 `encode_failed`，并且和桌面一样先退到精简 EXIF 再试。
- **EXIF 描述里的 `binding`。** 桌面写 `engine_pin()`，手机无法得到同样的值。描述由 export-source 的 `Request.binding` 决定，界面传 `gxr-core <构建号>`；测试传桌面的值，描述就逐字节相同。

### LUT 底图不逐字节

export-source 的显示域 LUT 和桌面在这台 Mac 上有差：numpy 的矩阵乘经过 Accelerate，Rust 用顺序的 IEEE 运算。它的 Gate A（对顺序 oracle）为 0 点，Gate B 的差点逐点归因到主机 BLAS（PORT-WAVE-1 的平台规则）。所以带显示域 LUT 的导出在这些像素上和桌面不同。测试分三步证明其余部分一致：Rust 的底图和 Python 的底图过 export-source 自己的 Gate A/B；Rust 的画布等于 Python 用 **Rust 的底图**做成片的结果；把 Python 自己的成片数组交给 `export_complete`，文件和桌面逐字节相同。Log LUT 走 float64 路径，真照片上 0 差。

### 内存

两次测量都在 Node 里用同一个 WASM 包，线性内存只增不减，所以结束时的大小就是峰值：
- 4288×2848 真 DNG，配方渲染（合成校准包）+ 拉直 + 画廊边框 + 日期戳：prepare 2678 ms，complete 519 ms，峰值 191.8 MiB（`mobile-export-pipeline/report/wasm-measure-raw.json`）；
- 合成的 4288×2848 底图走同样的成片，再在同一个实例里做一张长边 2048 的无边框导出：峰值 257.6 MiB（`report/wasm-measure.json`）。

- 4288×2848 真 DNG，中性渲染 + 拉直 + 画廊边框 + 日期戳 + HDR（数据层有效，带日期戳掩码）：prepare 6464 ms，complete 1222 ms，峰值 471.5 MiB（`report/wasm-measure-hdr.json`）。

都远低于页面约 2 GiB 的上限。

### 测试输出里不出现照片数据

对拍用实拍照片（EXIF、像素、元数据），测试失败时不能把这些内容打出来。威胁模型是：按本仓库平常写法写的测试，用 `make test`、`make check` 或 pytest 的 `-q`、`-v`、`-vv` 跑，不会无意中把照片派生的数据打进输出或 JUnit。故意绕过的测试代码不在范围内，那归代码评审管。

- 比较走 `tests/support/export_pipeline_cases.py` 的脱敏函数：`same_redacted`（字段路径只用固定词表里的键，别的键打哈希）、`same_file_redacted`（长度、第一个不同的字节、SHA-256 前缀）、`same_pixels`（形状、固定集合里的 dtype、不同元素的个数和第一个位置，不打数值）、`cli_ok`、`check`（标签是字面量）。错误只报已知的种类，别的打哈希。所有 helper 的 label 只打 SHA-256 前缀（label 可能含照片标签），后面附调用方的文件名和行号（只从栈帧取这两项，不带回溯和局部变量），所以失败仍然找得到。失败用 `pytest.fail(..., pytrace=False)`。
- `tests/test_core_parity_export_pipeline_guard.py` 是一个 lint，只抓常见的失手：裸 `assert` 比较非标量（含对非字面量取 `len`、动态的 f-string 格式说明）、非字面量的 `raise AssertionError` / `pytest.fail`（含 `reason=`）、`check` 的非字面量 label（含 `label=`）、任何 `np.testing` / `numpy.testing` 以及 `assert_array_*` / `assert_allclose`（会打出数组值，改用 `same_pixels`）、`print`、`logging`、`sys.stdout` / `sys.stderr`、`warnings.warn`、不是字面量或 `anon_ids(<个数>)` 的参数化 id、WASM 测试之外的 `synthetic_failure`。参数按同一套办法取：先看位置参数，没有再看关键字，`*args` / `**kwargs` 当作非字面量。每条报规则代码加文件和行号（如 `E_ASSERT test_core_parity_export_pipeline_x.py:12`），并附该规则的固定提示。它不解析别名，也不是边界。
- 金丝雀测试在 `-q`、`-v`、`-vv` 下让真实的比较代码在合成照片上失败（文件字节、元数据、HDR 状态和 Gate A/B、像素、RAW/LUT 共用路径、未知键、未知错误种类、结构 dtype，以及用照片 LensModel 拼 label 的各个 helper 和 `check(label=...)`），检查每条失败都带 label 哈希、调用位置和 helper 的细节，金丝雀和像素值不出现在 stdout、stderr 和 JUnit 里。另有测试确认 yield fixture 的 teardown 出错照常报错，`-W error` 照常把警告变成失败：测试不吞 pytest 自己的结果。
- **`-l` / `--showlocals` 不支持用在实拍测试上**：它会把回溯里的局部变量打出来，局部变量里有照片数据。

## 第一版界面

`mobile/index.html` 是给 iPhone Safari 的界面，不是开发壳。三屏都在内存里切换，不用浏览器的历史记录：起始、按拍摄时间分组的网格、编辑一张。

选文件是一个没有 `accept` 的多选框，否则存储卡上的 `.DAT` 会变灰。`R*.DNG` 和 `R*.JPG` 按主文件名配成一张，`GXRCE.DAT` 先留着，这一版不解析。别的文件不计进网格，导航上会写有几个没放进来。文件字节在 Worker 里读，同时最多两个。缩略图好一张挂一张。拍摄时间优先用 JPG 前 128 KiB 里的 `DateTimeOriginal`（带亚秒）；只有 DNG 时用 TIFF IFD 里的同一组标签，没有就用文件修改时间。描述接口现在不带日期。只有 DNG 时，缩略图来自内嵌预览：GXR 把 640×480 的 JPEG 放在第二个 SubIFD 里，是一条缩小尺寸（NewSubFileType 1）、JPEG 压缩的条带，不在 `JPEGInterchangeFormat` 标签下；两处都找，取最大的那份。

默认从早到晚，可以改成从晚到早，分组间隔是 1、3、5 或 10 分钟。这两项记在 `localStorage`（`gxr.mobile.order`、`gxr.mobile.gap`）。网格只挂看得见的行。点进一张后，DNG 原字节交给显卡引擎，配方键用开发壳那张中性烘焙表，画面上写着「开发用中性预览，不是配方」。拖滑块只调用 `setLook`。没有显卡时（模拟器）改显示相机 JPG 或内嵌预览，并写「这个浏览器现在用不了显卡预览」，滑块停用。`?perf=1` 在滚动和拖动时记下帧时间。

坏掉的 DNG 用设计稿里「读不了」那一屏。Worker 先查完整的 TIFF 头和 IFD 结构（`checkDng`），点开时再用 WASM 描述；这两步都和有没有显卡无关。读到一半文件句柄失效，用「存储卡被拔出了」那一屏。

整份 DNG 和下一张的预读也在 Worker 里读（`role: 'full'`，单独一个 Worker），主线程不调 `File.arrayBuffer`。每张照片有一个版本号，重新选同名照片就加一；缩略图、整份读取、清楚的大图回来时都先核对照片、版本和文件句柄，过期的结果丢掉，位图关掉。

一张照片分三件事记：缩略图好没好（`status`）、这个版本的文件读不读得了（`gone`）、显卡里有没有这个版本的完整源（`cached`；没有显卡时是描述过的版本 `checked`）。卡拔了以后，只有完整源还在显卡里（同一版本）的照片不读卡也能打开、调整；只读过缩略图、还要原文件的照片都计入「没读完」，格子上标卡的图标，点开会提示重新选。同一张照片的显卡上传由 App 排成一队（`uploadRaw`），每次开始前再核对照片、File 和版本，旧版本的上传不会在新版本之后把源换回去。拔卡状态一直留着、写在导航副标题上，直到重新选照片后没有没读完的。每张照片在显卡里只有一个固定的键，再加一次会替换原来那份。

有显卡时，配方卡是这张 RAW 的中性小图（`engine.thumb`，每个版本做一次）；没有显卡时卡片照实写「相机 JPG」或「内嵌预览」。直方图照设计稿的 `hist_svg`：红绿蓝和亮度、平方根纵轴、上限只看内部档位，两端三角在压死或溢出到 0.5% 时点亮；RAW 第一帧、换片和每次调整后数一次，按内容版本合并，静止时不读显卡。显卡正忙（比如换片时上一张的直方图还在读回）时 `histogram()` 给 null，这一帧还没画完（`pending`）也算一次没拿到（每次只看一次，不另外等帧），重试排到下一帧，不在微任务里接着转；连着 300 帧拿不到就放慢到每 250 ms 试一次，一直试到画上、换片或关掉（显卡丢了时每秒问 4 次）。裁剪、边框、LUT、曲线四页按 03e–03h 摆好静态控件，全部停用，写「这项下一步做」。

模拟器截图走开发服务器：`?shot=start|grid|editor|drag` 从样本端点读 DNG，摆好那一屏后 POST `/__dev/report/shot-<名字>`（写进不进仓库的 `mobile/.reports/`），截图脚本等这个文件出现再截。生产包里没有这段。

## 换配方（M2）

### 校准数据：Mac 导出，iPhone 导入

桌面「设置 → 校准数据」多了「导出给 iPhone」（`POST /api/calibration/export-iphone`，`camdata/calib_export.py`）。它把 `calib_dir(home)` 里的 `manifest.json` 和清单列出的每个文件打成一个**不压缩**（stored）的 `GXR 校准数据 YYYY-MM-DD.zip`，放进导出文件夹，重名时加 `-2`、`-3`。写之前逐个核对大小和 SHA-256，再对包里每个单元、两个 ISO 分支跑一次 `model_for`（原厂 04 指纹要对上当前的相机数据）；缺包、改过、清单不对、模型用不了就拒绝，文案和 `model_for` / `unit_status` 一样。目标文件夹走导出的检查（卡卷、图库），也不能在 Git 工作区里；检查之后马上钉成目录句柄（`privacy.PrivateDir`；目标还不存在时钉最近的已存在上级，再通过句柄逐级建出来），写入、占名字和失败清理都只通过句柄，之后路径被换成指向别处（哪怕是另一个允许的文件夹）的链接或目录被移走都不会写到别处。包只收集一次：校验的模型（和 `model_for` 共用 `calib.build_model`）和写进 zip 的是同一份内存快照。做好后可以和导出一样「在访达中显示」。

手机上在起始页的校准卡或「更多 → 校准数据」里选这个 zip。字节在 Worker 里读：`lib/zip.ts` 只认 stored 条目，压缩、加密、ZIP64、带路径的名字、重名、CRC 不对都拒绝，所有偏移先查边界，坏数据只给原因不抛。`lib/calib.ts` 照 `model_for` 核对版本、单元、文件清单和每个 SHA-256。zip 条目必须正好是 `manifest.json` 加清单列的文件，每个本地头都要和中央目录的名字、标志、长度、CRC 一致。File 直接交给 Worker：先看大小，超过 64 MiB 一个字节都不读；核对通过后由 Worker 原样存进 IndexedDB（`gxr-mobile` / `calib`），主线程不读也不复制整包。删除留一个时间戳，晚到的旧导入不会把删掉的包写回去；IndexedDB 另存见过的最高版本（墓碑也算），新会话从它之上编号，时钟回拨也不会把新导入当成旧的。删除要等 IndexedDB 确认才清空，失败就保留原来的包并写一行；导入没真正存进去时也照实说。每次打开页面也在 Worker 里重新核对一遍。原厂 04 的指纹要等选了 `GXRCE.DAT` 才查：拿 DAT 里这个单元的 04 条目，`sha256(entry[115:211])` 对上 `camera_stock_sha256`，再用两个 ISO 分支各跑一次 `color_bake`（就是建模型那一步）。哪一步不过，这个单元就按没校准处理，校准卡上写桌面的原话。

### 相机配方文件和配方卡

`GXRCE.DAT` 在 Worker 里用 `camdata_parse`、`camdata_entries`、`color_listing` 读。选了几个时用修改时间最新的，网格上写一行。只选 DAT（06d 的按钮）时留在当前这一屏。超过 16 MiB 的不读。每次选 DAT、清空照片都让 DAT 的版本加一，导入、删除、恢复校准数据让校准的版本加一；异步结果提交前核对版本，晚到的只丢掉。

配方卡照桌面 `CameraData.listing` 和 `recipes.js`：这张照片所在单元的内置 8 个（`BASE_NAMES`，说明用 `look_model.js` 的表），文件里对这个单元开放的 Looks，相机里的用户配方。镜头单元按 `unit_of_lens`（镜头名先取相机 JPG 的 `LensModel`，没有再取 DNG 的，再没有用 `UniqueCameraModel` 去掉「RICOH 」），认不出用 50mm。模型按 `model_for`：ISO ≥ 566 用 1600，否则用 200。「未校准」照 `RenderService.plan`：认不出单元、ISO 不在实拍校准范围、配方单元不是照片的单元，原因用 `calibration_note.js` 的句子。

每张卡的烘焙表见下面「旋钮、Looks 和校准核对」：`color_resolve` 出条目，再 `color_bake`。一张表约 3.8 MB，JS 里最多留 16 张；显卡里用 24 个固定的键轮流装（同一个键再装一次引擎会销毁旧的那份），舞台正在用的那张不让出去。这两个数等真机量过再调。配方卡小图（`app/looks.ts`）全局最多 32 MiB（一张约 96 KiB，约 340 张），每张卡只留现在用的那一张（动旋钮换上新的，旧的立刻关掉），按最近最少使用放掉，先放别的照片，上限任何时候都成立；换相机配方文件或校准数据时全部放掉。卡片按顺序一张张做烘焙表、请显卡出 192 像素的小图，拖滑块时先停。浏览器测试核对过：同一张照片、同一个条目和模型，UI 交给显卡的表和桌面 `preview_bake → bake_bytes` 逐字节相同（默认的原厂 04、内置配方、相机里的用户配方）；Look 和动过旋钮的 Look 走 color-oracle 的门禁：Python 参考在测试进程里用 `capture_bake_chroma` 跑一次生产 bake（抓下转 float32 之前的 float64 见证），手机交给引擎的表交给 `bake_gate`，Gate A（手机 == 顺序 oracle）逐位相同，Gate B（手机和 numpy 不同的点）由门禁逐点归因。

点卡就把选择记进这张照片的编辑状态（`photo.choice`），和调整放在一起，换片、重新选同一张都留着。表已经在缓存里就当帧换上；还在做时照片留着上一张，做好了而且还是要它才换。长按看原图是中性转换、调整归零。

没有校准数据（或这个单元没校准）时，卡片和照片都是中性转换（`lib/neutral.ts`），配方照样能选（06c）。没有 DAT 时只列八个位置的名字，卡片置灰，点卡或打开「配方」页提示补选（06d）。只有 JPG 时卡片是相机 JPG、全部停用，面板是桌面的两句（06e）。

### 旋钮、Looks 和校准核对（第 6 项）

配方列表来自核心的 `color_listing`（桌面 `CameraData.listing`）：内置 8 个（r5 时含文件记录）、对这个单元开放的 Looks、相机里的用户配方，每张卡带自己的旋钮。选中的配方照桌面 `RecipeSel` 的 JSON 交给 `color_resolve`，拿到旋钮已经套好的 552 字节条目，再 `color_bake`（不带 knobs）。Looks 和 r5 记录也这样画、这样选。缓存键是（单元、模型、条目 sha256）。

「配方」页在已校准时列出这个配方的全部旋钮（`KNOB_NAMES`，强度在最前），滑块的位置是档位的序号，旧版相机（第 135 代以前）对比度、高光、阴影、饱和度只到 ±5。拖动时只改右边的数字；松手（`change`）才记下档位、重新 resolve 和烘焙。新表做好之前照片留着上一张。每个配方各记各的档位（`photo.levels`），没选配方时动旋钮就选上现在显示的这个。

导入和打开页面恢复时，存之前先跑核心的包级检查 `color_calib_pack`（`gxr-cli color-calib-check --dir` 不带 `--unit`：版本、单元集合、每个单元的文件清单和 SHA-256），旧版本、单元为空或不认得的包不存。`iso` 两边都用 `gxr_color::checked_iso`（f64，有限整数，±(2**53−1) 以内）。单元能不能用交给核心的 `color_calib_check`（`gxr-cli color-calib-check --dir` 背后的 `model_for` 检查：版本、单元、原厂 04 指纹、文件清单、SHA-256、按 ISO 选模型并建模型），两个 ISO 分支各调一次，选出来的 ctx、ring 文件名就是烘焙用的。TS 里只剩 zip 这一层的完整性检查。`color_calib_check(manifest, index_json, blob, stock04, unit, iso)`：manifest 之外的文件拼成一块 `blob`，`index_json` 是 `{"名字": [偏移, 长度]}`，每段先查边界；`iso` 是 f64，必须是有限的整数。和 CLI 的比较用例在 `wasm_cases/color.mjs` 的 `calib`。

### 还没做的

- 卡片画的是调整归零的样子，不跟着调整变。
- r5（`GXRFX.BIN`）还不能选。

## 复制和套用到这一组（M3）

编辑器的「复制调整」（04a）照桌面 `paste_core.js`：六组（配方 / 风格、调整、几何、裁剪、边框、时间戳）、默认勾前两组，勾选记在 `localStorage` 的 `gxr-studio.paste-groups`（和桌面同一个键）。「设置过」照 `groupSource`：选了配方算配方组，有一项调整不是 0 算调整组；几何、裁剪、边框、时间戳在 M4 之前设不了，总是写「（这张没有设置）」。剪贴板只在这一页的内存里，是复制那一刻的深拷贝：`{ groups, from: {id, stem}, recipe: {choice, levels} | null, knobs | null }`。

复制完是 04b：这张所在的一组（当前间隔下的组）、「套用到这一组（N 张）」（不含来源这张）、「选照片…」和「先不套用」。网格组标题上的「套用到这一组」复制过就能点，目标是这一组里除来源以外的每一张；没复制时点它写一行「先打开一张照片，点「复制调整」。」。「选照片…」把网格开成挑选模式（04d），底栏是剪贴板和「套用到 N 张」；选择栏的「套用调整」一样。每条路都先确认（04c）。「这一组」永远是现在显示的那一组：复制后改了间隔，组标题给的就是新的张数，确认里写准确的张数。

旋钮档位和 `memory` 照桌面记：`photo.levels` 是选中那个配方的档位，只记用户动过的（`ensureRecipe(view).levels[name] = …`）；`photo.memory` 是桌面的 `memory`（`look_cards.js` 的 recipeTarget）：换配方时把切走的那个配方的档位（`cleanLevels`，不补默认；没选过配方时是档位为空的原厂 04）记进去，新配方取回 memory 里上次的档位，点已经选中的那张什么都不改，当前配方的档位不在 memory 里更新。键是卡的 id，导出时按镜头单元换成桌面的 recipeKey（`lib/export_request.ts` 的 `buildSide`）。换配方的那一步在 `lib/recipe_memory.ts`，`cleanLevels` 只有 `lib/export_request.ts` 那一份。

套用是同步的、在内存里，规则照 `render/paste.py` 的 `paste_one`（`lib/paste.ts`）：配方组里，镜头单元不同 → `unit_mismatch`；只有 JPG → `needs_raw`（近似模式关着）；这份 DAT 里这个单元没有这张卡 → `recipe_rejected`；否则换成来源的配方和它的档位；来源的 `memory` 不是空的就整份换过来，是空的就留着目标自己的（`render/edits.write` 在请求里没有 `memory` 时沿用 sidecar 里的）。调整组整份换成复制来的，0 也算。配方组要么整个套用，要么不套。来源这张从来不是目标。结果句是桌面的 `pasteMessage`，撤销的结果是 `undoMessage`，这几个函数的字放在 `lib/paste_text.ts`，单元测试从 `web/assets/paste_core.js` 里把桌面的函数原样抠出来跑，对同一批输入比输出。

每张照片有一个编辑版本 `rev`：选配方、旋钮松手、滑块松手（`change`）、全部重置、点两下归零、套用、撤销都加一；重新选同一张照片不变。撤销照 `PasteUndo`：每批记下每张套用之前的配方、档位和调整，以及套用之后的 `rev`；同一批里套到同一张两次，还原到最早的样子；只留最近 20 批，只在内存里，重新打开页面就没有了。撤销时 `rev` 还是套用后那一版才还原，否则算「粘贴之后又改过」。

结果和「撤销」贴在网格底部（04e），和「已复制」占同一格；到下一次复制、下一次套用、清除、清空照片，或离开网格 15 秒（桌面提示的 `timeout: 15000`）才收起。没有按钮的一行（没复制时的提示、撤销的结果）照桌面普通提示，3.2 秒后收起。套用按时间片做，每片最多 4 ms，片与片之间让出主线程：500 张合成照片一共约 4–7 ms。

拍摄时间和桌面同源：手机的拍摄时间是 `photo_meta(...).taken` 加 `SubSecTimeOriginal` 的毫秒（桌面的 `taken` 到秒为止）。RAW+JPG 的相机 JPG 里没有拍摄时间时，和桌面导入一样用 DNG 的（Worker 再读一次 DNG 的前 256 KiB，缩略图仍用 JPG 的）；两个都没有才用文件修改时间。模拟器截图用 `?shot=paste-04a|paste-04b|paste-04c|paste-04d|paste-04e|paste-02a`（`app/paste_shots.ts`，只在开发服务器里）。

## 导出进相册（M3）

选择栏的「导出 N 张」和编辑器的「导出这张」打开同一个导出面板（05a）：尺寸「原始尺寸 / 大 / 中 / 小」（长边不限、3000、2048、1600）、品质「高 / 中 / 低」（95、85、70），默认原始尺寸、高，和桌面 `SIZE_TIERS` / `QUALITY_TIERS` 一样。一张时右边写成品尺寸（`export_output_sizes`，源尺寸在导出 Worker 里从文件头读：相机 JPG 转正后的尺寸，或 DNG 的裁切窗口）；几张时写桌面那句「每张照片按各自比例导出，成品尺寸可能不同。」。HDR 那一行见下面「HDR」。没选 `GXRCE.DAT` 时面板下面是桌面那句说明。

- **请求**（`lib/export_request.ts`，纯函数）：`shot` 照桌面图库的 shot（`name`、`files`、`lens`、`iso`；`width` / `height` 是图库存的 `photo_meta(jpg).width or photo_meta(dng).width`，由 Worker 读文件头补上：`lib/photo_size.ts` 照 `meta.read_jpeg_meta` / `read_dng_meta`，不用解码后的位图）；`side` 照桌面 `render/edits.write` 写出的 sidecar：`recipe` 是 `RecipeSel.to_json`（档位只带用户动过的，和桌面 `cleanLevels` 一样），`memory` 是照片的 `photo.memory`（桌面 `look_cards.js` recipeTarget 记下的那份，空的档位也是一项，见「复制和套用到这一组」），键按桌面 `recipeKey` 改写（内置 `<单元>/<位置>`，其余 `<单元>/<种类>/<id>`）；`adjust` 是 `Adjust.to_json`，全是 0 时不写。没动过的照片是 `{}`。`defaults` 是 `{}`，底图「自动」，`layout` 是 `{}`（M3 没有边框和日期戳）。`binding` 是 `gxr-core <构建号>`，构建号是 `vite build` 时的短提交号（`vite.config.ts` 的 `__GXR_BUILD__`，开发服务器里是 `dev`）。
- **导出 Worker**（`core/export_worker.ts`、`core/export_api.ts`）：不是 M1/M2 那组 Worker。一个 Worker、一份 WASM、一次一张；一批开始时建，结束后闲 30 秒、停止或清空时关掉，WASM 的线性内存跟着还回去。相机配方文件和校准包（`calib_index` + `calib_blob`，文件按名字排好）每批交一次，Worker 重建时再交。每张：读 DNG / JPG（主线程不调 `File.arrayBuffer`）→ `export_plan` → 选了配方时 `color_resolve`（只有 RAW、没选配方时用这个单元的原厂 04）→ 没选配方又有相机 JPG 时解码相机 JPG → `export_prepare` → 墨迹 → `export_complete`。核心报 `raw_unreadable` / `no_jpg_source` / `raw_decode` 时，像桌面 `_raw_rgb` 那样解内嵌预览（`export_meta_preview` 的位置）再试一次。
- **墨迹**：M3 不能设边框、日期戳、裁剪和 LUT，`export_prepare` 只会给 `canvas: "full"`、没有版面，墨迹这一步什么都不画（导出管线的「无墨迹」情况，和桌面逐字节相同）。三次调用之间留着 `ink(prepared, canvas)` 这一处；万一 prepare 给了边框、日期戳或 `canvas: "photo"`，这一张按 `ink_not_supported` 失败，绝不漏掉墨迹导出。M4 在这里补上，字体包也跟着 M4。
- **解码相机 JPG**：照 `finish/compose.load_jpeg`：`createImageBitmap(blob, { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' })`，画到 `OffscreenCanvas`（`srgb`、`willReadFrequently`），`getImageData` 后去掉 alpha，位图马上关掉。无头 Chrome 里和 Pillow 逐像素相同（含 Orientation 6、8 的拷贝）；Safari 用的是 ImageIO，真机检查时比解码像素的哈希。
- **陷阱**：WASM 陷阱、`RangeError` 或 Worker 的 `error` 事件：关掉 Worker，这一张写「内存不够，这张没做成。可以重试，或改用小一点的尺寸。」，下一张用新的 Worker 接着做。
- **批次**（`app/export_batch.ts`，不碰 DOM）：点「存到相册」那一刻把照片、文件句柄、版本和请求定下来，之后改动不影响这一批；按网格当前的顺序做。「停止」马上关掉 Worker，做好的留着。「重试这张 / 这 N 张」只重做失败的，用同一份请求，重新选过的照片用新的 File；失败的照片按网格顺序并进还没做的队列，不会把它冲掉。先把做好的存进相册、点了「好」，失败的照片和重试仍然留在 06f，直到它们做好或者清空。做好的文件（`new File([bytes], file_name, { type: 'image/jpeg' })`）留在内存里，任何时候都不超过 384 MiB：下一份结果会超过时就放掉它，这张排回队首，先停在「做好了」那一栏，交出去、点「好」以后再做它和剩下的；一张就超过上限的按内存不够算。交给 Worker 这一步本身抛错（建 Worker 时的 `RangeError` 之类）也按陷阱算，接着做下一张；这一批的设置（相机配方文件、校准包）也不在 `begin()` 里发，而是在交第一张时、同一个 try 里发给新建或已经建好的 Worker（比如一张时先查过成品尺寸），发不出去同样按陷阱算。下载兜底的对象 URL 由导出这边统一记着，重画、交出、清空、新的一批和批次结束时都撤销。每次提交前核对会话、批次和照片版本，晚到的结果丢掉。读不了的文件走 M1 的 `gone`（拔卡）路径。跑着的时候配方卡小图和烘焙先停（`app/export_pause.ts`），并申请屏幕常亮。
- **进度和结果**：点「存到相册」面板收起，进度贴在最底下（`.pdock`，在网格和编辑器下面，不盖住选择栏），可以接着滚、点开照片。做完不自动打开共享面板：Safari 只认刚发生的那次点击。05c 的「存到相册」在点击里同步调用 `navigator.share({ files })`，前面没有 await；先问 `canShare`，没有共享面板的浏览器写一句「这个浏览器打不开系统的共享面板。」，加每个文件的下载链接。一次最多 20 张、256 MiB（`SHARE_MAX_FILES` / `SHARE_MAX_BYTES`），多了分批，按钮写「存到相册（第 1 批，共 3 批）」。`share()` resolve 只说明用户在面板里做了某个操作，不一定是存储图像，所以 05e 写「好了：N 张」和「在面板里点了「存储 N 张图像」，就已经在相册里。」，不说「已存到相册」（机主决定 2）；关掉面板（`AbortError`）就留在 05c。
- **`?perf=1`**：一批的 WASM、JS、待存文件的高水位和每张的毫秒数。
- **开发服务器里**（生产包里没有）：`window.__devExport` 可以把 `binding` 换成桌面的 `engine_pin()`（逐字节对比用）、把分批上限改小、在第几张模拟一次陷阱，测试也从这里读批次和内存。

- **HDR**（ui-m3-hdr，M5 的导出那一半提前做）：选中的照片里有 RAW 时开关可用，写设计稿那句「在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。」；一张 RAW 都没有时停用，写桌面的「HDR 需要 RAW 文件」（`hdrAvailable`、`HDR_NO_RAW`），也不改记住的选择。开着、又混着只有 JPG 的照片时，下面一行「HDR 需要 RAW 文件：N 张只有 JPG，会存普通版本。」。开关记在 `localStorage`（`gxr.mobile.export_hdr`），第一次默认关，和桌面 `prefs.export_hdr` 一样。开了 HDR，每张的请求都带 `hdr: true`，做不做得成由核心定（只有 JPG 的是 `no_raw`，照常导出普通版本）。Worker 里 `export_prepare` 之后用 `hdr_layer()` 取出数据层（float32 `(h, w, 2)`），原样交给 `export_complete`，不回主线程；日期戳掩码是空的（M3 没有日期戳，M4 的墨迹补上）。进度第二行是「生成 HDR 版本要多等几秒。可以接着看照片。」；05c / 05e 多一行「4 张 HDR · 1 张只有 JPG，存普通版本」，别的原因没做成的按桌面 `hdrSummary` 计数（「N 张未生成 HDR」），逐张写桌面 `HDR_NOTES` 的说明（`capture_mismatch`「RAW 与 JPG 不是同一次拍摄」，别的「这张照片不支持 HDR」）。文件名里的 `_hdr` 只看核心结果的 `hdr`。HDR 的一批待存文件上限是 256 MiB（`STAGE_CAP_HDR_BYTES`），比普通的 384 MiB 低，因为 HDR 的 WASM 峰值和 Worker 里多拿的数据层会让整页的估算超过 1.2 GiB。

测试：`tests/test_mobile_ui_export_hdr_unit.py`（HDR 的开关、文案和上限，`node:test`）、`tests/test_mobile_ui_export_hdr_browser.py`（`browser_serial`：四对 RAW+JPG 加一张只有 JPG 的开 HDR 导出原始尺寸和小，和桌面 `export_finish(hdr=True)` 逐字节相同，`hdr`、`hdr_reason`、文件名也相同）、`tests/test_mobile_ui_export_unit.py`（请求、批次状态机、分批、文件头尺寸，`node:test`）和 `tests/test_mobile_ui_export_browser.py`（`browser_serial`：解码门禁；六张照片在界面里编辑后导出，和桌面 `export_finish` 逐字节相同，ICC 时间戳按导出管线的办法固定；停止、陷阱、清空、读不了再重试、没有共享面板）。这几个文件都在导出管线隐私 lint 的范围里（`test_mobile_ui_export*.py`）。

## LUT（M4）

- **导入与存储（`lib/lut.ts`、`app/lut_book.ts`、`core/worker.ts`）**：用不带 `accept` 滤镜的多选文件框打开选择器，仅收 `.cube` 文件并统计忽略项（「N 个文件不是 .cube，没有导入。」）。在 Worker 里先按 `MAX_CUBE_BYTES` 检查大小再读取字节、按 `decodeCubeUtf8` 严格核对 UTF-8（去 BOM，非 UTF-8 统一映射为桌面 `cube_unreadable`「无法读取 LUT。请改用 .cube 文件。」）、算 SHA-256，调用 `lut_parse` 与 `lut_identify(bytes, filename)` 生成与桌面 `_record` 对齐的记录；字节和记录按版本水位与事务确认规则存进 IndexedDB（`gxr-mobile` 的 `lut_records` / `lut_blobs` / `lut_meta`），库总容量上限 **256 MiB**（`MAX_LUT_LIBRARY_BYTES`），超出报「LUT 太多了。删掉几个再导入。」，重复 `cube_id` 不覆盖已改名/已选空间并提示「已经导入过」；`remove`/`rename`/`setSpace`/`importFiles` 均捕获 `AbortError` 等 IndexedDB 异常，事务失败时不动内存并恢复弹层按钮。
- **输入色彩空间（`app/lut_sheet.ts`）**：`lut_identify` 未写明或存在冲突时卡片显示「· 请选择输入色彩空间」并锁定，选定后解锁；选单按桌面 `lut_choices.js` 的两组（「普通照片与视频显示」「相机 Log 与高级空间」）列出全部 12 个空间，并在高级区提供「输入范围 / 输出色彩空间（`DISPLAY_SPACES`：sRGB / Rec.709 / Gamma 2.4）/ 输出色域 / 输出范围」，未改动「输出色彩空间」时保留原声明值；默认选项与说明行由核心 `undeclared_choice` 给出，03g 行显示当前空间与来源标记（文件已写明 / 推测 / 默认 / 已手选）。空间是库级属性，更改后同步刷新所有使用同一 LUT 的照片。
- **03g 标签页与直接套用（经理决定 M-4）**：首张「不用 LUT」，其后为每份已导入 LUT 生成当前照片的 GPU 小图（纳入 M2 的 32 MiB 全局缩略图上限）；无输入空间或 JPG-only 照片遇到 Log LUT（非 `srgb` / `rec709` / `gamma24`）时锁卡并显示「这个 LUT 需要 RAW 文件」；引用的 LUT 被删除后显示「LUT 已删除」；长按支持「重命名」「删除」；卡条（`lut_view.ts`、`recipe_view.ts`）在卡片键序列不变时原地更新 DOM，避免异步刷新时卸载正在交互的节点；03g 脚注按 M-4 改为「LUT 只存在这台 iPhone 上，直接套在照片上，不写入相机。」。
- **GPU 预览与内存（`workers/lut_table.ts`、`app/lut_gpu.ts`、`frontend/src/engine.ts`）**：`lut_table(bytes, space_json)` 在专用短命 Worker 中逐个运行，完成后立即 `terminate()` 并将表（64 MiB）转移回主线程（不进常驻池，避免 WASM 线性内存滞留）；表未就绪时照片保持上一帧画面；`engine.addLut(cube_id, meta, data)` 后立刻丢弃 JS 持有，引擎显存缓存最多保留 3 份 LUT（`trim(this.luts, 3)`）；清空照片（`clear()` / `remove()`）时调用 `lutGpuQueue.clear()` 同步终止短命 Worker、清零队列并经 `engine.clearLuts()` 释放 GPU 上的 LUT 纹理。无头 Chrome 实测连切 3 份显示 LUT：GPU 峰值 **192.0 MiB**，JS 暂态峰值 **64.0 MiB**，短命 `lut_table` Worker WASM 峰值 **75.88 MiB**。
- **配方复制与导出（`lib/paste.ts`、`lib/export_request.ts`、`core/export_worker.ts`）**：配方组扩展为「配方、LUT」，只要选了配方或 LUT 即视为已设置，套用与配方部分全有或全无，处理 `lut_missing` 与 `needs_raw`（`part: "lut"`）。导出请求带 `side.lut = cube_id`，点击导出时先同步定格内存中的 `TargetSpec` 并在 `LutBook.exportSnapshot()` 同步登记写屏障（`rename` / `setSpace` / `removeChecked` 在 `restore()` 之后经 `writeTail` FIFO 队列按调用顺序串行排队，轮到时再等全部在途快照读完，失败也释放屏障且不阻塞后续写），再经 `lut-snapshot` 在单次 IndexedDB `readonly` 事务里原子读齐全部 `.cube` 字节与记录，避免跨多 Worker 池或重叠快照下的并发修改/删除竞态与写反序；LUT 已删或 JPG-only 配 Log LUT 时按桌面 `cube_not_found` / `lut_needs_raw` 报错（绝不静默去 LUT 导出）。无头 Chrome 实测导出峰值：SDR 全尺寸 WASM **209.6 MiB** / JS **39.3 MiB**，HDR + Log LUT 全尺寸 WASM **438.2 MiB** / JS **132.2 MiB**（低于 640 MiB 上限）。
- **测试**：`tests/test_mobile_ui_lut_unit.py`（`node:test` 记录构建、空间选择与保留、配方+LUT 粘贴、出图令牌、`exportSnapshot` 写屏障、重叠快照 FIFO 写序与失败释放、`AbortError` 恢复、`LutGpu.clear()`、`decodeCubeUtf8` 及与桌面 Python 逐项对比）、`tests/test_mobile_ui_group_parity_browser.py`（配方 + LUT 复制套用桌面一致）以及 `tests/test_mobile_ui_lut_browser.py`（`browser_serial`，受 `test_core_parity_export_pipeline_guard.py` 隐私 lint 约束：导入、选空间与保留 `output_space`、非 UTF-8 报错、3 槽短命 Worker GPU 预览与清空释放、真实 IDB `tx.abort()` 删卡失败恢复、真实多 Worker 池繁忙及两次重叠快照下的写屏障与 FIFO 写序回归、Display/Log/HDR 导出按 Manager post-r1 P2-01 规范与桌面/Gate A/B 对拍、重命名与删卡导出报错）。

## 导出底图（`gxr-export` 的 `source/`，命令前缀 `source-`）

移植的是导出时「从哪张图开始」：`finish/serve.py` 的 `color_image` 和 `_raw_rgb`（只有 `full=True`）、`render/service.py` 的 `plan` 和不精确的 `renderer`、`raw/render.py` 的 `render_full`（任意渲染器）、`lut/photo.py` 的 `color_image`。不移植 `exact_u8`（精确导出）、电脑图库里的本机配方、近似配方（`approx.ENABLED` 是 False）和预览路径。

桌面版从应用数据目录读相机数据和校准包，手机直接拿用户选的文件：DAT（r5 卡另带 GXRFX.BIN）、校准包里的每个文件、相机 JPG 和内嵌预览（浏览器解码好的 uint8，JPG 已按 EXIF 转正），以及解析后的配方条目（编辑解析那一单元的输出）。

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `source-plan` | `--shot`、`--edit`，可选 `--dat`/`--library`、`--entry`、`--calib DIR`、`--binding` | `selection`、`plan`（单元、槽、`model_iso`、`calibrated`、`defaulted`、标签、条目 sha256）和 `renderer`（id、标签、写进 EXIF 270 的 `description`） |
| `source-selection` | `--shot`、`--edit`、`--base`，可选 `--space` | 这个底图模式会规划哪个配方，调用方据此去解析条目。相机 JPG、中性转换，以及场景域 LUT（`--space` 的输入空间不是 srgb、rec709、gamma24）时是 `null`。显示域 LUT 去掉 LUT 后仍按底图模式选，不随校准或相机数据改变 |
| `source-render` | `--dng`、`--entry`、`--stock`、`--unit`、`--calib`、`--iso`、可选 `--adjust` | 全尺寸 uint8 `(H, W, 3)`，已转正 |
| `source-color-image` | 同上，再加 `--base`、`--jpg`/`--preview`（uint8 `.npy`）、`--cube`/`--space` | `path`（走了哪条路）、`from_raw`、`labels`，图写到 `--out` |

- shot 是 `{"files": {"dng": {"relpath"}, "jpg": {...}}, "lens", "iso"}`，只看有哪些文件、DNG 的文件名、镜头和 ISO。edit 是 `RecipeSel.to_json()`、LUT 的 id，加上 `color-apply` 读的 `adjust`、`curves`。
- `binding` 是桌面的 `engine_pin()`。它来自应用数据目录里文件的哈希和修改时间，手机没法算出同一个值，所以由调用方给；描述里的这一段因此和桌面不会逐字节相同，除非调用方给的就是桌面那份。
- 配方渲染按条带走：每次 256 行去马赛克（float32），交给 `chroma_u8`，量化后写进 uint8 输出，没有整幅的浮点数组。LUT 的 Log 路径也按条带：先算这一条带的场景 XYZ（`colour.xyz` 整条带先核对有限），再按 65536 像素一块套 LUT，和 Python 的分块一样。显示域 LUT 对整张 uint8 底图按 65536 像素一块套。
- 本机配方：Python 先查 RAW+JPG 是否成对（`local_recipe_needs_pair`），再去电脑图库解析。手机没有那个图库，成对时报 `local_recipe_unsupported`（`RenderUnavailable`，state 仍是 `local_recipe`），这是有意的差别。
- 中性转换：底图模式 `neutral` 一律画中性转换（不规划配方，有校准也一样），没有全尺寸 CFA 时用内嵌预览。画面是中性转换或内嵌预览时 `from_raw` 为假，成片像 JPG 一样套全部调整；`standard` 退回中性转换时也是这样。
- 错误：`NoRaw` 报 `no_raw`；`RenderUnavailable` 带 `state`；`EditError` 带 `detail`；`RawDecodeError` 按 `export_finish` 改成 `raw_unreadable`（有 JPG）或 `no_jpg_source`，`detail` 是「没有全尺寸传感器数据，也没有更小的预览。」加原因。
- `render_full` 在分配输出之前做和 `gxr_raw::render_full` 一样的裁切窗口、白平衡和马赛克检查（`check_crop_window` 在 `gxr-raw` 里是私有的，这里照抄了同样的判断和文案），所以坏文件在配方渲染和中性渲染上报同一个错。尺寸用带检查的 u64，元素数不超过 2^32−1，输出缓冲区可失败地申请。

WASM：`source_plan(shot, edit, dat, library, entry, calib_index_json, calib_bytes, binding)` 返回和 CLI 一样的 JSON（带换行）；`source_selection(shot, edit, base, space_json)` 同样返回 `source-selection` 的 JSON（带换行），`space_json` 为空就是没给 `--space`；`source_render(dng, name, entry, stock04, unit, calib_index_json, calib_bytes, iso, adjust_json)` 返回 `[h u32][w u32][rgb]`。校准包的格式照 `frame_layout` 的字体：`[[名字, 字节数], …]` 加拼在一起的文件。比较用例在 `mobile/scripts/wasm_cases/source.mjs`。

输入检查（Sol r1 之后），CLI 和 WASM 走同一段代码：

- 0 字节就是没给：空的 `dat`、`library`、`entry` 当作缺省，空的调整当作 `{}`。CLI 的空文件和 WASM 的空数组结果一样。
- 条目和原厂 04 必须正好 552 字节，在求哈希、规划或渲染之前检查，错误是 `value`／`entry must be 552 bytes`。
- 解码好的相机 JPG 和内嵌预览在公共 `color_image` 入口先查：宽高大于 0、3 个通道、`h*w*3` 用带检查的 u64 算且不超过 2^32−1、字节数正好相符，然后才复制或套 LUT。CLI 读 `--jpg`/`--preview` 的 `.npy` 时，解析完头就用同一个 `check_rgb_shape` 查声明的形状和 payload 长度，不通过就报错，不复制像素；通过的像素留在读文件的那块缓冲里。
- 调整解析之后再查有限值：曝光是 NaN 或 ±Infinity 时报 `bad_adjust`／「请求无效。请刷新页面后重试。」，曲线点非有限时报 `bad_curve`，文案和 Python 的 `parse_adjust`、`_parse_points` 一样。
- WASM 的 `iso` 按 f64 接收（CLI 把文本读成同一个数），先查有限、是整数、绝对值不超过 2^53，再变成整数；`NaN`、`Infinity`、`1.5` 报 `bad_request`／`iso 要是整数`，超过 2^53 报 `iso 超出范围`。

对拍在 `tests/test_core_parity_export_source.py`：相机数据用上层项目的 g127 DAT（`GXR_STUDIO_TEST_PARENT`），校准包是合成的（`color_fixtures` 生成，用 `import_calibration` 写），机主的校准来源在本机时另跑 3 张，不打印内容。配方渲染、标签、`from_raw`、错误都要求完全相同；LUT 照 lut 单元的 Gate A / Gate B（顺序 oracle、边界点、机械归因）。

## HDR 追踪与封装（`gxr-export` 的 `hdr_track/`，命令前缀 `hdrtrack-`）

移植桌面版成片导出里的 HDR 增益图层跟踪与 Ultra HDR JPEG 封装：`finish/hdr_export.py`（`HdrTrack` 状态机）、`finish/geometry.py` 的 `warp_float` 与双通道 `crop_valid_f32`、`gxr_finish::resample_f32(..., Filter::Box, ...)` 以及 `gxr_jpeg::encode_luma_jpeg`（单通道增益图 JPEG 与 Pillow `"L"` 逐字节一致），最后交给 `gxr-hdr::assemble` 写入 MPF + XMP Gain Map。

- CLI 子命令（`cmd_hdrtrack.rs`）：`hdrtrack-warp-float`、`hdrtrack-crop-valid`、`hdrtrack-begin`、`hdrtrack-begin-batch`（按行隔离返回 `exit`/结果，不 export 到 WASM）、`hdrtrack-warp`、`hdrtrack-box`、`hdrtrack-crop`、`hdrtrack-stamp`、`hdrtrack-frame`、`hdrtrack-to-grid`、`hdrtrack-finish`。
- WASM 导出（`gxr-wasm/src/hdrtrack.rs`）：`hdrtrack_begin`、`hdrtrack_warp`、`hdrtrack_box`、`hdrtrack_crop`、`hdrtrack_stamp`、`hdrtrack_frame`、`hdrtrack_to_grid`、`hdrtrack_finish`（不含 `hdrtrack-begin-batch`）。
- 源 JPEG 校验复用 `gxr-export::meta` 的 `open_image` / `load_image_exif`，`hdr_track/` 本身不复制 JPEG/TIFF 解析；向 Pillow 12.2.0 对齐时，对 `meta/tiff_ifd.rs` 的 `tiff_tag_length` 按 IFD 分组（Root / EXIF `34665` / GPS `34853` / Interop `40965`）拆表，并移除了 Pillow `TAGS_V2` 已不存在的 `326`、`327`。
- **已接受的偏差 — 超大像素（DecompressionBomb）JPEG**：对超出 Pillow 像素上限（178,956,970 像素）的 JPEG，桌面 Python 的 `HdrTrack.begin`（`hdr/gain.py:check_capture`）仅捕获 `(ValueError, OSError, SyntaxError)`，会直接向外抛出未捕获的 `DecompressionBombError`；手机核心在 `hdr_track/capture.rs` 中将所有 `meta::tiff_exif::open_image` 错误统一归类为 `"metadata_unreadable"`，令 `HdrTrack` 安全失活并按无 HDR 导出（不崩溃）。`meta` 元数据层（`open_image`、`read_jpeg_meta`、`photo_meta`）仍与 Pillow 12.2.0 一致返回 `DecompressionBombError`；差分 sweep（`tests/test_core_parity_jpeg_sweep.py`）仅对明确登记的 5 例超大像素边界（`ord_mp_big_bomb_sof`、`ord_mp_no_b002_bomb_sof`、`ord_bomb_solo`、`ord_zero_then_bomb`、`ord_ok_then_bomb`）归一化 `HdrTrack.begin` 状态。

