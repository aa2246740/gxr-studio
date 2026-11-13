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

1. 选负责的 crate。已经留好的空 crate 见下面的表，不要再改 `core/Cargo.toml` 的成员列表。
2. 一个 Python 函数对一个 Rust 函数，名字用 snake_case。注释写 `/// Port of gxr_studio/<路径>.py:<行号> <函数名>`。运算顺序和每一步的 dtype 都照着 Python 来。
3. 在 `gxr-cli` 加子命令。数组写成 `.npy`（小端、C 顺序、格式 1.0），其余走 JSON。核心错误打印 `{"error":{"kind","message"}}` 并退出 2，用法错误退出 1。
4. 加 `tests/test_core_parity_<范围>.py`，用 `tests/support/core_cli.py`。
5. 需要网页用到的函数，在 `gxr-wasm` 导出，并让 `mobile/scripts/wasm_vs_native.mjs` 也比这一项。
6. 先 `make core-test`，再跑对应的 pytest，最后 `make check`。

`#![forbid(unsafe_code)]`。坏输入返回 `Result`，不要 panic。`CoreError.kind` 用稳定的短字符串。

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
| `gxr-finish` | `gxr_studio/finish/` 和 `crop.py`：裁切、几何、边框、戳记 |
| `gxr-jpeg` | Pillow 那条 JPEG 编码，以及 `gxr_studio/meta.py` |
| `gxr-export` | `export.py`、`raw/render.py`、`raw/demosaic.py` 的全分辨率流程 |

`gxr-raw` 已经有 TIFF/DNG、马赛克、半分辨率码值和显卡布局。`gxr-math` 是 libm 的薄包装。

## 开发壳

`make mobile-dev` 打开的页面可以选一张 DNG。Worker 里的 WASM 读布局，显卡引擎画预览。拖动曝光、对比度或饱和度只会调用 `setLook`。页面上写着「开发用中性预览，不是配方」：没有相机数据时，`mobile/src/dev/neutral_bake.ts` 造一张中性烘焙表（sRGB 编码乘 255，BT.601，增益网格全是 `[1, 0]`）。真正的配方表以后从 `gxr-color` 来。

没有 WebGPU 时（模拟器就是这样）仍会跑 WASM，并显示布局摘要，同时说明这个浏览器现在用不了显卡预览。

`?autotest=1` 只在开发服务器里生效。它会拉 `GXR_MOBILE_SAMPLES` 里的第一张 DNG，跑描述和 5 秒自动拖动，再把结果写到 `mobile/.reports/`。这些路径不会出现在 `vite build` 的产物里。开发壳的页面是 `dev.html`，`/?dev=1` 和 `/?autotest=1` 也会打开它，所以原来的自动测试地址不用改。

## 第一版界面

`mobile/index.html` 是给 iPhone Safari 的界面，不是开发壳。三屏都在内存里切换，不用浏览器的历史记录：起始、按拍摄时间分组的网格、编辑一张。

选文件是一个没有 `accept` 的多选框，否则存储卡上的 `.DAT` 会变灰。`R*.DNG` 和 `R*.JPG` 按主文件名配成一张，`GXRCE.DAT` 先留着，这一版不解析。别的文件不计进网格，导航上会写有几个没放进来。文件字节在 Worker 里读，同时最多两个。缩略图好一张挂一张。拍摄时间优先用 JPG 前 128 KiB 里的 `DateTimeOriginal`（带亚秒）；只有 DNG 时用 TIFF IFD 里的同一组标签，没有就用文件修改时间。描述接口现在不带日期。只有 DNG 时，缩略图来自内嵌预览：GXR 把 640×480 的 JPEG 放在第二个 SubIFD 里，是一条缩小尺寸（NewSubFileType 1）、JPEG 压缩的条带，不在 `JPEGInterchangeFormat` 标签下；两处都找，取最大的那份。

默认从早到晚，可以改成从晚到早，分组间隔是 1、3、5 或 10 分钟。这两项记在 `localStorage`（`gxr.mobile.order`、`gxr.mobile.gap`）。网格只挂看得见的行。点进一张后，DNG 原字节交给显卡引擎，配方键用开发壳那张中性烘焙表，画面上写着「开发用中性预览，不是配方」。拖滑块只调用 `setLook`。没有显卡时（模拟器）改显示相机 JPG 或内嵌预览，并写「这个浏览器现在用不了显卡预览」，滑块停用。`?perf=1` 在滚动和拖动时记下帧时间。

坏掉的 DNG 用设计稿里「读不了」那一屏。读到一半文件句柄失效，用「存储卡被拔出了」那一屏。已经读进来的缩略图还留着。

模拟器截图走开发服务器：`?shot=start|grid|editor|drag` 从样本端点读 DNG，摆好那一屏后 POST `/__dev/report/shot-<名字>`（写进不进仓库的 `mobile/.reports/`），截图脚本等这个文件出现再截。生产包里没有这段。
