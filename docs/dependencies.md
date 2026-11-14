# 依赖

直接依赖写在 `pyproject.toml`，用 `==` 钉死。`uv.lock` 是安装时的唯一依据，用 `uv sync --frozen`。

`sentry-sdk` 是直接依赖，这样设了 `GXR_STUDIO_SENTRY_DSN` 就不用再装一次。没设这个变量时不会初始化，也不会发请求。它带上的 `urllib3` 是传递依赖；`certifi` 本来就在锁里。

`pydantic-core`、`anyio`、`h11`、`click`、`typing-extensions` 由 FastAPI、uvicorn 和 pydantic 加载。源码里不写它们的 import，但版本仍然钉在直接依赖里，避免锁文件被间接依赖悄悄换掉。deptry 的 DEP002 因此放行这五个名字。

`src/gxr_studio/engine/` 和 `src/gxr_studio/lut/spaces.py` 要和来源逐字节或逐段相同（见各自的 `PROVENANCE.json`）。ruff 不格式化它们。

等一个版本发布满 7 天再把它写进锁定文件。`renovate.json` 的 `minimumReleaseAge` 是同一条规则。安全修复如果已经在用的版本有公开的洞，可以不等满 7 天，但要在提交说明里写是哪一个洞。

加开发依赖也要锁版本，并在提交说明里写为什么需要它。

Windows 的 NumPy 固定为 2.5.4，其他平台保持 2.4.3。Windows 的 2.4.3 轮子自带 OpenBLAS 0.3.31.dev，
两个线程并发执行 65536×3 的矩阵乘法会停住，独立于文件句柄代码和 LUT 表分配。
2.5.4 轮子的 OpenBLAS 0.3.34.106.0 通过同一双线程入口，完整 256³ RGBA 表的 SHA-256 也与 2.4.3 设置
`OPENBLAS_NUM_THREADS=1` 的对照结果相同。[2.5.4](https://github.com/numpy/numpy/releases/tag/v2.5.4)
在 2026-10-10 发布，已超过 7 天。`scripts/windows_probe.py matmul` 和 `lut` 可重跑这两项检查。

## 前端构建（`frontend/`）

显卡预览引擎用 TypeScript + WGSL 写，Vite 打成一个不依赖外部包的 ES 模块 `web/assets/gpu/engine.js`。**产物提交进仓库**，所以双击「GXR Studio.command」、`make dev` 和 `uv run gxr-studio` 都不需要 Node。只有改 `frontend/` 的人需要 Node 22+ 和 pnpm（`packageManager` 钉在 `pnpm@10.33.2`），用 `make web` 重新构建后提交；`make check`（`scripts/check_frontend.py`）会重新构建到临时目录并逐字节核对，CI 里还要求核对必须跑（`GXR_REQUIRE_FRONTEND=1`）。

版本用 `frontend/package.json` 的精确版本和 `frontend/pnpm-lock.yaml` 钉死，`frontend/.npmrc` 的 `minimum-release-age` 同样要求新版本发布满 7 天。都是开发依赖，打进产物的只有我们自己的代码：

| 包 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| `vite` | 8.3.5 | MIT | 构建（库模式），把 `.wgsl` 以文本内联 |
| `typescript` | 7.0.2 | Apache-2.0 | 类型检查（`tsc --noEmit`） |
| `@webgpu/types` | 0.1.74 | BSD-3-Clause | WebGPU 的类型声明 |

传递依赖（`pnpm licenses list`）：rolldown、@rolldown/*、@oxc-project/types、postcss、nanoid、picomatch、tinyglobby、fdir、fsevents 为 MIT；picocolors 为 ISC；source-map-js 为 BSD-3-Clause；detect-libc 为 Apache-2.0；lightningcss 为 MPL-2.0（Vite 自带的 CSS 工具，只在构建机上运行，不进入产物，不改它的源码，所以 MPL 的文件级义务不涉及本仓库）。以上许可都允许在 PolyForm Noncommercial 1.0.0 的项目里使用。

一致性测试（`tests/test_gpu_parity.py`）用本机已装的 Google Chrome 的 SwiftShader 软件适配器，不新增 Python 依赖。

## Rust 核心（`core/`）

`core/Cargo.toml` 把直接依赖写成 `=x.y.z`。下面这几个在 2026-11-13 核对时都已发布超过 7 天。工具链是 `core/rust-toolchain.toml` 里的 1.93.1，高于它们的 MSRV（wasm-bindgen 0.2.130 要 1.81，命令行工具要 1.86）。

| 包 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| `libm` | 0.2.16 | MIT | `gxr-math` 的数学函数。原生和 WASM 走同一份，避免两边的 `sin`/`exp` 差一位 |
| `wasm-bindgen` | 0.2.130 | MIT OR Apache-2.0 | 只在 `gxr-wasm` 里用，把描述和半分辨率码值导出给网页 |
| `wasm-bindgen-cli` | 0.2.130 | MIT OR Apache-2.0 | 构建机上的命令，和库同一个版本：`cargo install --locked wasm-bindgen-cli --version 0.2.130` |
核心 crate 之间只能互相依赖，再加 `libm`（`jpeg-encoder` 外部包已移除，不再出现在 `core/Cargo.lock`）。

### 内部移植的 JPEG 编码器（`gxr-jpeg`，原 `jpeg-encoder 0.6.0`）

`core/crates/gxr-jpeg/src/jpeg_enc/` 将 `jpeg-encoder 0.6.0`（2023-10-09 发布，MIT OR Apache-2.0；其中 `fdct.rs` 派生自 IJG 软件）源码直接收编为仓库内部模块，并移除了外部 `jpeg-encoder` 依赖。MIT、Apache-2.0 许可原文与 IJG 致谢（"this software is based in part on the work of the Independent JPEG Group"）均保存在 `core/crates/gxr-jpeg/NOTICE`。

相对上游 `jpeg-encoder 0.6.0` 的改动：
- 移除 x86/x86_64 `avx2` SIMD 模块及 `no_std`/`alloc` 条件编译，以纯安全 Rust 满足工作区 `#![forbid(unsafe_code)]`；
- 将 `encoder.rs` 拆分为 `encoder.rs` 与 `encoder_scans.rs`，满足 `scripts/large_files.py` 单文件不超过 800 行的规范。
- 将定点算术与 4:2:0 下采样对齐 `libjpeg-turbo` C 参考实现（`optimize=False` 单扫描交错编码）：
  - `image_buffer.rs`：`rgb_to_ycbcr` 对 `Y` 使用 `+ 0x8000`（`ONE_HALF`）、对 `Cb/Cr` 使用 `+ 0x7FFF`（`ONE_HALF - 1`）；
  - `quantization.rs`：使用整数精确除法 `(|x| + (q >> 1)) / q`（保留符号）替代 15-bit 倒数近似；
  - `encoder.rs`：YCbCr 分量 ID 对齐 ITU-T T.81 / JFIF（`1, 2, 3`）；
  - `encoder_scans.rs`：`2x2` 子采样执行 `h2v2_downsample`（列偏置 `bias = 1, 2, 1, 2`、垂直钳位至有效色度末行），并对超出 `ceil(w/8) × ceil(h/8)` 的 MCU 填充块输出 `jccoefct.c` dummy 块（`[prev_dc, 0, ...]`）。

不使用 serde。JSON 由 `gxr-raw` 里手写的写入器生成，命令行和 WASM 共用，所以两边的字节一样。`mobile/` 的开发依赖和 `frontend/` 是同一组精确版本（vite 8.3.5、typescript 7.0.2、`@webgpu/types` 0.1.74），锁在 `mobile/pnpm-lock.yaml`。

### 生成的 Unicode 表（`gxr-hdr`）

`core/crates/gxr-hdr/src/unicode_tables.rs` 不是外部包，是 `core/crates/gxr-hdr/scripts/gen_unicode_tables.py` 用参考 Python（3.14.7，Unicode 16.0.0）逐个码点生成的：`str.isspace()` 的空白、`float(str)` 认的十进制数字、`str.casefold()` 的完整折叠。数据来自 Unicode Character Database，许可是 Unicode License v3，全文在 `core/crates/gxr-hdr/NOTICE`。换 Python 或 Unicode 版本时重跑脚本，再 `rustfmt`；`tests/test_core_parity_hdr_review.py` 会核对表和当前解释器一致。

### 内部移植的图像重采样器（`gxr-finish`，原 `Pillow 12.2.0`）

`core/crates/gxr-finish/src/resample/` 依据 `Pillow 12.2.0`（2026-04-01 发布，MIT-CMU / HPND 许可）的 `src/libImaging/Resample.c`、`src/libImaging/Convert.c`（`RGBA ↔ RGBa`）、`src/libImaging/ImagingUtils.h` 与 `src/PIL/Image.py`（`resize`）用纯安全 Rust 重写，不引入外部 Rust crate。Pillow 版权与 MIT-CMU 许可全文保存在 `core/crates/gxr-finish/NOTICE`。

相对上游 `Pillow 12.2.0` C 实现的约束：
- `#![forbid(unsafe_code)]` 纯安全 Rust 实现；
- Lanczos `sinc` 统一调用 `gxr_math::sin`（`libm`），使原生 CLI 与 WASM 逐位一致；
- `32bpc`（`mode = "F"`）按标准 IEEE 754 `f64` 顺序乘加并禁止 FMA（`mul_add`），遵循核心可移植算术规范。
