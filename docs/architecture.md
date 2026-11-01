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
| 导出的 JPEG/PNG | 导出目录 | `export` |
| 存储卡 | 只读。测试禁止碰 `/Volumes` 里真实的卡 | `cards` |

渲染链是 `raw` 读 DNG，`engine` 拿拟合好的整数表，`render` 把裁切和配方套上去。`lut` 在已经有相机数据时做路线 1 的旋钮拟合。`devtools` 只给测试和假卡用，运行时代码不许 import 它（import-linter 会查）。

HDR 在 `hdr`：`gain` 从同一张 DNG 算增益图（对不齐就不做），`container` 按 ISO 21496-1 + Ultra HDR 封装，主图字节不动。导入结束后另起一个 `hdr` 任务生成图库里的 HDR 版本；导出勾选 HDR 时由 `finish.hdr_export` 让 RAW 亮度跟着成片的几何、日期戳和边框走，在导出的 JPEG 上重新算。

生成前，`hdr.capture` 核对原始文件中双方都有的拍摄时间、秒的小数部分、曝光时间、光圈、ISO、相机型号、镜头和图像唯一 ID。已知字段冲突就记为 `capture_mismatch`，原因 JSON 和导出提示均为“RAW 与 JPG 不是同一次拍摄”。缺失、空白或未知数值不表示相同，仍须通过图像检查；不以文件名、文件修改时间或当前日期补全。相机时钟一致地设在 2000 年也可以正常配对。

图像检查包含全局平移、4×4 局部结构相关和多种窗口大小的亮度残差。局部必须有当前位置的相关证据；小范围搜索没有更优结果不能证明已对齐。残差检查先平均取样，再拟合窗口内的亮度关系；超过误差门槛的连续区域会导致不支持。v1 缓存不再显示为已通过，下一次导入后按 v2 重查。

关闭应用时，先在 `hdr_lock` 内停止接受新 HDR 任务并发出取消信号，再等现有 HDR 线程退出，最后关闭图库。整轮枚举出错会把 HDR 进度设为 `failed`，由任务表调用 `on_job_failed`；逐张失败仍独立计数。

后台导入和拟合走 `jobs`。任务抛错时 `register_alert_service` 调用 `on_job_failed`，由 `StudioNotifier` 记一条打过码的告警。
