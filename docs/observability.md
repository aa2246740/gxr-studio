# 启动之后看哪里

工作室没有远程仪表盘。一次启动好不好，看这几处：

- 终端里的启动行：URL、端口、图库路径。第二个实例会说已经开着并以 75 退出。
- `GET /api/health`：`{"ok": true, "version": ...}`，并带 `X-Request-ID`。
- `GET /api/metrics`：这个进程里的请求数和状态码，Prometheus 文本。
- `GET /api/usage`：按路由记的本地使用次数，不上传。
- `GET /api/alerts`：失败任务的告警。同一条也会以 JSON 打到 stderr。
- `<图库>/.studio/import-log.jsonl`：导入追加日志。

这些接口和页面一样要令牌。请求号从通过访问控制的响应头 `X-Request-ID` 来，日志里的 `request_id` 是同一个值。

## 可选的远程错误追踪

默认不上报、不联网。只有环境变量 `GXR_STUDIO_SENTRY_DSN` 有值时才初始化 Sentry。没设置时不会加载发送逻辑，本地的 `GET /api/alerts` 和 stderr 日志照常。

设置之后，失败的后台任务会多送一条事件，带上：

- `request_id`：任务开始时那次请求的 `X-Request-ID`（没有请求上下文就是空字符串）
- `job_id`、`job_kind`：任务 id 和任务类型
- `app_version`：`gxr_studio.__version__`
- `release`：`GXR_STUDIO_SENTRY_RELEASE`，没设就是 `gxr-studio@<版本>`
- `environment`：`GXR_STUDIO_SENTRY_ENVIRONMENT`，没设就是 `local`

`send_default_pii` 关掉。发送前会把家目录、卷路径、卡路径（`DCIM` / `MISC`）、文件名、令牌和长串内容换成占位符，并且丢掉请求体和用户字段。不会附带图像、DNG 或 DAT 内容。自动采集集成也关掉了，所以框架不会另外抓请求。
