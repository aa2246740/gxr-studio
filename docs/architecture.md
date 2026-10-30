# 工作室怎么串起来

一个进程。`gxr_studio.cli` 拿图库锁，听 `127.0.0.1`，`create_app` 装上图库、任务表、渲染和安全中间件。浏览器打开的页面来自 `web/`，脚本是普通 ES 模块，没有打包步骤。

请求先过 `SecurityMiddleware`（令牌、Host、Origin），再过 `ObserveMiddleware`（`X-Request-ID` 和进程内计数）。业务路由在 `api/routes.py`、`render_routes.py`、`camdata_routes.py`、`lut_routes.py`。

数据在本机，不调用外部服务：

| 东西 | 放哪 | 谁用 |
| --- | --- | --- |
| 照片和边车 | 图库目录，默认 `~/Pictures/GXR Studio` | `library.store` |
| SQLite 索引 | `<图库>/.studio/index.sqlite`，可以删掉重建 | `library.store`，表结构在 `src/gxr_studio/library/schema.sql` |
| 相机数据和标定 | 应用数据目录 | `camdata` |
| 导出的 JPEG/PNG | 导出目录 | `export` |
| 存储卡 | 只读。测试禁止碰 `/Volumes` 里真实的卡 | `cards` |

渲染链是 `raw` 读 DNG，`engine` 拿拟合好的整数表，`render` 把裁切和配方套上去。`lut` 在已经有相机数据时做路线 1 的旋钮拟合。`devtools` 只给测试和假卡用，运行时代码不许 import 它（import-linter 会查）。

后台导入和拟合走 `jobs`。任务抛错时 `register_alert_service` 调用 `on_job_failed`，由 `StudioNotifier` 记一条打过码的告警。
