# 逐单元校准

电脑预测按配方单元选模型。50mm、28mm、MOUNT 分别校验自己的原厂 04 色表行；一个单元失效不会停用其它有效单元。相机数据本身失效时全部停用。

| 单元 | 输入映射 | C040 和色相环 | 实拍校准 ISO | 曝光锚 |
| --- | --- | --- | --- | --- |
| 50mm / `jlaunch` | 二次多项式残差 | 50mm 实测节点、增益、核参数 | 200、1600 | 50mm |
| 28mm / `qlaunch` | fit-v4-r2 冻结的零残差 | 自身原厂节点，保留 50mm 实测残差与核参数 | 200 | 曝光沿用 50mm |
| MOUNT / `l08firm` | 一次残差 | 自身原厂节点，保留 50mm 实测残差与核参数 | 200 | 曝光沿用 50mm |

模型基准为研究 fit-v4-r2 实际评分使用的 `native-C040-v2.0`。它用原厂 04 色度查表，用配方 C100 色度乘查得的增益。cal28-r1 已采用的 28mm IN6w 尚未进入该冻结评分模型，本次不额外叠加。所有单元都保留 ISO 200 / 1600 两套色相核；低于 ISO 566 用 200 分支，其余用 1600。28mm、MOUNT 的高 ISO 是迁移预测，不能称为实拍校准。

28mm 屏幕场次和 MOUNT 既有屏幕、自然场景资料可用于颜色映射和曝光互转，未提供可直接替代 50mm 中灰锚的独立绝对曝光标定。Log LUT 的场景 XYZ 继续使用照片自己的 DNG 矩阵与 50mm 锚，不随 LUT 调整曝光。

曝光与白平衡增益先作用于 RAW 码值，再做输入映射，最后夹紧并查曲线。预览和导出的色度增益网格间距均为 0.375；回归继续要求浮点 p99 ΔE00 ≤ 0.3、量化最大差 ≤ 3 码，不因模型更新放宽。

## 本地数据格式

外部包清单为 `calib-pack.json`，schema 是 `gxr-studio/calib-pack/2`。`units` 的键是上述单元 ID，每个单元含 `200`、`1600` 两项，各项含 `npz` 和 `ring` 文件名。`files` 保存每个文件的 SHA-256 与大小。文件名只允许普通文件名，拒绝链接、路径穿越、错误摘要和异常大小。

每份来源 NPZ 含 `stock`、`d0`、`d1`、`S_eff`、`input_coefficients`。输入多项式系数为 `(4, 3)` 或 `(10, 3)`，依次对应常数、R/G/B，以及可选的 R²/G²/B²/RG/RB/GB。码值先除以 4095，再加残差。Ring JSON 使用 `gxr-sector-model/2`。

导入先核对所有来源，再原子替换整个 `calib/`。可以导入部分单元，未列出的单元成为“未导入”。安装后的清单 schema 为 `gxr-studio/calib/2`，包含 `units.<id>.models`、实拍 ISO、曝光来源和原厂色表摘要。安装后的 NPZ 只存 `d0`、`d1`、输入系数；`stock` 与原厂查表矩阵运行时来自相机数据。参数和字节只保存在应用数据目录。旧 schema 拒绝使用，要求重新导入。

## 接口字段

- `POST /api/calibration/import` 接收绝对文件夹路径 `path`，支持新版包或开发机研究根目录。校验失败保留之前的包，错误码为 `calibration_pack_invalid` 或 `calibration_stock_mismatch`。
- `GET /api/camdata/status` 和 `GET /api/render/status` 的 `calibration.units.<id>` 含 `state`、`message`、`isos`、`exposure_note`。`state` 为 `ready`、`missing` 或 `invalid`，`isos` 只列当前可用的实拍校准 ISO。至少一个单元可用时总状态 `ready=true`，调用指定单元时仍单独检查。
- `GET /api/recipes` 的单元项含 `calibrated`、`calibrated_isos`、`calibration_note`、`exposure_note`。
- `GET /api/lut/spaces` 的单元项含当前 `fit_supported` 和上述 `calibration` 状态。前端据此启用“生成配方”。
- `POST /api/lut/fit` 的 `unit` 决定校准模型、原厂 04、旋钮菜单和照片抽样。只选同单元的 RAW；相机数据或全部校准不可用时返回 `camera_data_missing`，其后才按所选单元检查 `unit_not_calibrated`。拟合固定使用 ISO 200，搜索策略仍为“底子 + 旋钮”。

## 本机复核

公开回归 `tests/test_unit_calibration.py` 只用合成数据；无研究文件的 CI 不需要标定数值。研究逐位复核入口为 `scripts/verify_unit_calibration.py`，先通过研究 `tools/loadgate.py`，再在共享重命令锁下运行：

```sh
lockf -k /tmp/gxr-heavy.lock nice -n 15 env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 \
  .venv/bin/python -I -B scripts/verify_unit_calibration.py \
  --research /path/to/projectricoh \
  --out /path/to/projectricoh/research/studio_unitcal_r1/local/parity \
  --home /path/to/scratch/unitcal-app-data
```

脚本从研究实际源码单独加载模型构造、输入映射和 v2 前向函数；对三单元、两套 ISO、两组 C100 矩阵和固定输入比较浮点字节。输出另含 NumPy 多种归约的实际差界、旧 Studio 50mm 的改变量与输入摘要。核对用应用数据目录必须在 Git 工作区外；应用隐私守卫不因目录已被忽略而放行。真实数据和输出目录不提交到 Studio。
